"""
Boucle d'enrichissement itérative (demande du prof : "loop end: 1) repeat X
times, 2) check coverage, 3) (good) test fail").

À chaque itération :
  1. MESURE la branch coverage de l'état courant (suite existante + tests
     déjà générés) ;
  2. s'arrête si : plus aucun gap (full_coverage), aucun progrès depuis
     l'itération précédente (plateau), ou max_iterations atteint ;
  3. sinon : re-PLANIFIE en ciblant les gaps RESTANTS, avec le feedback de
     l'itération précédente (tests déjà générés, échecs d'assertion, claims
     falsifiés) ;
  4. GÉNÈRE de nouveaux tests, les concatène aux précédents, VALIDE le tout
     (boucle de réparation) ;
  5. VÉRIFIE les claims (claim-vs-trace) : chaque test est exécuté
     individuellement sous coverage et ses branches revendiquées sont
     confrontées aux arcs réellement visités.

Les "good test fails" (tests qui tournent mais échouent sur assertion) sont
enregistrés à chaque itération : ce sont des détections potentielles de
défauts, pas des déchets.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import claims, enhancer, planner, validate

import ast
import re


def _rename_duplicate_tests(existing_code: str, new_code: str, it: int) -> str:
    """Renomme (suffixe _itN) les fonctions de test du nouveau code dont le
    nom existe déjà dans le code accumulé — sinon la redéfinition Python
    écraserait silencieusement les tests des itérations précédentes.
    Renomme aussi les clés correspondantes dans CLAIMED_BRANCHES."""
    try:
        existing_names = {n.name for n in ast.parse(existing_code).body
                          if isinstance(n, ast.FunctionDef)}
    except SyntaxError:
        existing_names = set()
    try:
        new_names = [n.name for n in ast.parse(new_code).body
                     if isinstance(n, ast.FunctionDef)]
    except SyntaxError:
        return new_code
    for name in new_names:
        if name in existing_names:
            renamed = f"{name}_it{it}"
            new_code = re.sub(rf"\bdef {name}\b", f"def {renamed}", new_code)
            new_code = new_code.replace(f'"{name}"', f'"{renamed}"')
            new_code = new_code.replace(f"'{name}'", f"'{renamed}'")
    return new_code


@dataclass
class LoopResult:
    final_tests: str
    stop_reason: str                    # full_coverage | plateau | max_iterations | empty_plan | empty_generation
    records: list[dict] = field(default_factory=list)
    last_plan: "planner.TestPlan | None" = None
    last_enh: "enhancer.EnhancementResult | None" = None
    last_outcome: object = None         # RepairOutcome de validate
    last_claims: dict | None = None     # rapport claim-vs-trace final


def _failed_tests_from_stdout(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines()
            if line.strip().startswith("FAILED ")]


def _call_with_retry(fn, is_empty, raw_of, save_raw, label, attempts=2):
    """Appelle fn() jusqu'à `attempts` fois tant que is_empty(result)."""
    result = None
    for att in range(1, attempts + 1):
        result = fn()
        save_raw(att, raw_of(result))
        if not is_empty(result):
            return result
        print(f"    [!] {label} tentative {att}: réponse vide/inparsable — retry")
    return result


def run_enhancement_loop(
    repo_dir: Path,
    out_dir: Path,
    node_ids: list[str],
    patched_paths: list[str],
    annotated: str,
    agg_table: list[dict],
    existing_tests: str,
    base_test_path: str,
    regions: dict | None = None,
    max_iterations: int = 3,
) -> LoopResult:
    import coverage_probe          # module racine (cwd = racine du projet)

    llm_raw_dir = out_dir / "llm_raw"
    llm_raw_dir.mkdir(exist_ok=True)
    dest_rel = Path(base_test_path).parent / "test_te_enhanced.py"

    combined_tests = ""
    prev_missing: dict | None = None
    last_validation_failures: list[str] = []
    last_claims_report: dict | None = None
    generated_goals: list[str] = []
    records: list[dict] = []
    plan = None
    enh = None
    outcome = None
    stop_reason = "max_iterations"

    suspect_counts: dict[str, int] = {}      # "a->b" -> nb d'échecs à fermer
    unreachable: set[str] = set()            # branches classées inatteignables

    for it in range(1, max_iterations + 1):
        # --- 1. Mesure de l'état courant --------------------------------
        extra = []
        if combined_tests.strip():
            (repo_dir / dest_rel).write_text(combined_tests, encoding="utf-8")
            extra = [str(dest_rel)]
        cov = coverage_probe.measure_on_prepared_repo(
            repo_dir, node_ids, patched_paths, regions=regions, extra_node=extra, verbose=False)
        (out_dir / f"coverage_iter{it}.json").write_text(
            json.dumps(cov, indent=2), encoding="utf-8")

        # Les critères d'arrêt portent sur les gaps ACTIONNABLES (région
        # patchée) quand le scoping est disponible — sinon la boucle
        # poursuivrait des branches hors périmètre qu'on ne cible pas.
        missing = {f: e.get("region_missing_branches", e["missing_branches"])
                   for f, e in cov["files"].items()}
        n_missing = sum(len(v) for v in missing.values())
        pct = {f: e["percent_covered"] for f, e in cov["files"].items()}
        n_file = sum(len(e["missing_branches"]) for e in cov["files"].values())
        print(f"    [it {it}] couverture: {pct} | branches manquantes "
              f"(région patchée): {n_missing}  [fichier entier: {n_file}]")
        # --- 2. Critères d'arrêt ---------------------------------------
        # Les branches classées inatteignables ne comptent plus comme cibles.
        actionable = {f: [b for b in v if f"{b[0]}->{b[1]}" not in unreachable]
                      for f, v in missing.items()}
        n_actionable = sum(len(v) for v in actionable.values())
        if unreachable:
            print(f"    [it {it}] dont {len(unreachable)} branche(s) classée(s) "
                  f"inatteignable(s) -> {n_actionable} cible(s) restante(s)")

        if n_missing == 0:
            stop_reason = "full_coverage"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing, "action": "stop"})
            break
        if n_actionable == 0:
            stop_reason = "unreachable_gaps"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing,
                            "unreachable": sorted(unreachable),
                            "action": "stop"})
            break
        if prev_missing is not None and missing == prev_missing:
            stop_reason = "plateau"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing,
                            "unreachable": sorted(unreachable),
                            "action": "stop"})
            break
        prev_missing = missing

        # --- 3. Plan ciblé sur les gaps restants + feedback -------------
        weakness = coverage_probe.render_weakness_map(cov, repo_dir)
        feedback = None
        if it > 1:
            fb = [f"This is iteration {it} of the enhancement loop.",
                  "Tests already generated in previous iterations (do NOT "
                  "duplicate their goals):"]
            fb += [f"  - {g}" for g in generated_goals] or ["  (none)"]
            if last_validation_failures:
                fb.append("Assertion failures from the previous iteration "
                          "(these tests RUN but their expectation failed — "
                          "either the expectation is wrong, or a real defect "
                          "was found; do not simply re-propose the same "
                          "strategy):")
                fb += [f"  - {f}" for f in last_validation_failures]
            if last_claims_report:
                falsified = {n: r for n, r in
                             last_claims_report["tests"].items()
                             if r["verdict"] == "falsified"}
                if falsified:
                    fb.append("CLAIM-VS-TRACE verification results from the "
                              "previous iteration (MEASURED execution): the "
                              "following tests claimed branch outcomes they "
                              "did NOT actually exercise. The strategy did "
                              "not reach its target — analyse WHY using the "
                              "code and propose a STRUCTURALLY different "
                              "approach:")
                    for n, r in falsified.items():
                        fb.append(f"  - {n}: claimed {r['claimed']}, "
                                  f"never taken {r['missing']}")
            fb.append("Only propose NEW test items for the REMAINING measured "
                      "gaps above. If a previous strategy failed to reach its "
                      "target branch, propose a DIFFERENT strategy.")
            feedback = "\n".join(fb)

        plan = _call_with_retry(
            fn=lambda: planner.make_plan(
                annotated_code=annotated, variable_table=agg_table,
                existing_tests=existing_tests + "\n\n" + combined_tests,
                coverage_summary=weakness, feedback=feedback),
            is_empty=lambda p: not p.items,
            raw_of=lambda p: p.raw_response,
            save_raw=lambda att, raw: (llm_raw_dir /
                f"planner_raw_it{it}_attempt{att}.txt").write_text(
                    raw, encoding="utf-8"),
            label="planner")
        if not plan.items:
            stop_reason = "empty_plan"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing, "action": "abort"})
            break
        print(f"    [it {it}] plan: {len(plan.items)} objectif(s)")
        generated_goals += [it_.goal for it_ in plan.items]

        # --- 4. Génération + concaténation + validation -----------------
        enh = _call_with_retry(
            fn=lambda: enhancer.enhance_tests(
                annotated_code=annotated, variable_table=agg_table,
                existing_tests=existing_tests + "\n\n" + combined_tests,
                plan=plan),
            is_empty=lambda e: not e.enhanced_tests.strip(),
            raw_of=lambda e: e.raw_response,
            save_raw=lambda att, raw: (llm_raw_dir /
                f"enhancer_raw_it{it}_attempt{att}.txt").write_text(
                    raw, encoding="utf-8"),
            label="enhancer")
        if not enh.enhanced_tests.strip():
            stop_reason = "empty_generation"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing, "action": "abort"})
            break

        new_tests = _rename_duplicate_tests(combined_tests,
                                            enh.enhanced_tests, it)
        candidate = (combined_tests + "\n\n\n" + new_tests
                     if combined_tests.strip() else new_tests)
        outcome = validate.validate_with_repair(
            repo_dir=repo_dir, enhanced_tests=candidate,
            annotated_code=annotated, base_test_path=base_test_path or None,
            max_iterations=3)
        combined_tests = outcome.final_tests
        v = outcome.result
        last_validation_failures = _failed_tests_from_stdout(v.stdout)
        print(f"    [it {it}] validation: {v.n_passed} passent, "
              f"{v.n_assertion_fails} échouent (assertion), "
              f"{v.n_run_errors} ne tournent pas")

        # --- 5. Vérification claim-vs-trace -----------------------------
        (repo_dir / dest_rel).write_text(combined_tests, encoding="utf-8")
        print(f"    [it {it}] vérification claim-vs-trace...")
        last_claims_report = claims.verify_claims(
            repo_dir=repo_dir, test_rel_path=str(dest_rel),
            test_code=combined_tests, patched_paths=patched_paths)
        (out_dir / f"claims_iter{it}.json").write_text(
            json.dumps(last_claims_report, indent=2), encoding="utf-8")
        print(f"    [it {it}] claims: "
              f"{last_claims_report['n_verified']} vérifiés, "
              f"{last_claims_report['n_falsified']} falsifiés, "
              f"{last_claims_report['n_no_claims']} sans claim")

        # --- 5bis. Détection des gaps inatteignables --------------------
        # Une branche ciblée par un test qui a RÉELLEMENT tourné mais qui
        # reste ouverte est suspecte ; suspecte 2 fois -> inatteignable
        # (typiquement du code mort : cf. sympy-20154, garde en amont qui
        # rend un bloc `if n == 0:` inaccessible).
        targeted = {f"{b[0]}->{b[1]}"
                    for item in plan.items for b in item.claimed_branches}
        cov_after = coverage_probe.measure_on_prepared_repo(
            repo_dir, node_ids, patched_paths, regions=regions,
            extra_node=[str(dest_rel)], verbose=False)
        still_missing = set()
        for f, e in cov_after["files"].items():
            for b in e.get("region_missing_branches", e["missing_branches"]):
                still_missing.add(f"{b[0]}->{b[1]}")
        # Une branche ciblée par un test qui a RÉELLEMENT tourné ET PASSÉ
        # mais qui reste ouverte est très probablement inatteignable
        # (code mort). Si le test a échoué, sa stratégie peut simplement
        # être mauvaise : on laisse une seconde chance.
        passed_tests = {n for n, r in last_claims_report["tests"].items()
                        if r.get("passed")}
        targeted_by_passing = set()
        for n in passed_tests:
            for b in last_claims_report["tests"][n].get("claimed", []):
                targeted_by_passing.add(f"{b[0]}->{b[1]}")

        for key in targeted & still_missing:
            threshold = 1 if key in targeted_by_passing else 2
            suspect_counts[key] = suspect_counts.get(key, 0) + 1
            if suspect_counts[key] >= threshold:
                unreachable.add(key)
        newly = {k for k in targeted & still_missing
                 if suspect_counts.get(k, 0) >= 2} - (unreachable - unreachable)
        if newly:
            print(f"    [it {it}] branches suspectées inatteignables: "
                  f"{sorted(newly)}")

        records.append({
            "iteration": it, "coverage": pct, "missing_branches": missing,
            "n_plan_items": len(plan.items),
            "n_passed": v.n_passed,
            "n_assertion_fails": v.n_assertion_fails,
            "n_run_errors": v.n_run_errors,
            "good_test_fail_candidates": last_validation_failures,
            "claims": {n: r["verdict"]
                       for n, r in last_claims_report["tests"].items()},
            "action": "generated",
        })

    (out_dir / "loop_history.json").write_text(
        json.dumps({"stop_reason": stop_reason, "records": records},
                   indent=2), encoding="utf-8")
    return LoopResult(final_tests=combined_tests, stop_reason=stop_reason,
                      records=records, last_plan=plan, last_enh=enh,
                      last_outcome=outcome, last_claims=last_claims_report)