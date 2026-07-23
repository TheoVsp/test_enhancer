"""
Boucle d'enrichissement itérative (demande du prof : "loop end: 1) repeat X
times, 2) check coverage, 3) (good) test fail").

À chaque itération :
  1. MESURE la branch coverage de l'état courant (suite existante + tests
     déjà générés) ;
  2. s'arrête si : plus aucun gap (full_coverage), aucun progrès depuis
     l'itération précédente (plateau), ou max_iterations atteint ;
  3. sinon : re-PLANIFIE en ciblant les gaps RESTANTS, avec le feedback de
     l'itération précédente (tests déjà générés, échecs d'assertion) ;
  4. GÉNÈRE de nouveaux tests, les concatène aux précédents, VALIDE le tout
     (boucle de réparation).

Les "good test fails" (tests qui tournent mais échouent sur assertion) sont
enregistrés à chaque itération : ce sont des détections potentielles de
défauts, pas des déchets.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import enhancer, planner, validate


@dataclass
class LoopResult:
    final_tests: str
    stop_reason: str                    # full_coverage | plateau | max_iterations | empty_plan | empty_generation
    records: list[dict] = field(default_factory=list)
    last_plan: "planner.TestPlan | None" = None
    last_enh: "enhancer.EnhancementResult | None" = None
    last_outcome: object = None         # RepairOutcome de validate


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
    max_iterations: int = 3,
) -> LoopResult:
    import coverage_probe          # module racine (cwd = racine du projet)

    llm_raw_dir = out_dir / "llm_raw"
    llm_raw_dir.mkdir(exist_ok=True)
    dest_rel = Path(base_test_path).parent / "test_te_enhanced.py"

    combined_tests = ""
    prev_missing: dict | None = None
    last_validation_failures: list[str] = []
    generated_goals: list[str] = []
    records: list[dict] = []
    plan = None
    enh = None
    outcome = None
    stop_reason = "max_iterations"

    for it in range(1, max_iterations + 1):
        # --- 1. Mesure de l'état courant --------------------------------
        extra = []
        if combined_tests.strip():
            (repo_dir / dest_rel).write_text(combined_tests, encoding="utf-8")
            extra = [str(dest_rel)]
        cov = coverage_probe.measure_on_prepared_repo(
            repo_dir, node_ids, patched_paths, extra_node=extra, verbose=False)
        (out_dir / f"coverage_iter{it}.json").write_text(
            json.dumps(cov, indent=2), encoding="utf-8")

        missing = {f: e["missing_branches"] for f, e in cov["files"].items()}
        n_missing = sum(len(v) for v in missing.values())
        pct = {f: e["percent_covered"] for f, e in cov["files"].items()}
        print(f"    [it {it}] couverture: {pct} | branches manquantes: {n_missing}")

        # --- 2. Critères d'arrêt ---------------------------------------
        if n_missing == 0:
            stop_reason = "full_coverage"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing, "action": "stop"})
            break
        if prev_missing is not None and missing == prev_missing:
            stop_reason = "plateau"
            records.append({"iteration": it, "coverage": pct,
                            "missing_branches": missing, "action": "stop"})
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

        candidate = (combined_tests + "\n\n\n" + enh.enhanced_tests
                     if combined_tests.strip() else enh.enhanced_tests)
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

        records.append({
            "iteration": it, "coverage": pct, "missing_branches": missing,
            "n_plan_items": len(plan.items),
            "n_passed": v.n_passed,
            "n_assertion_fails": v.n_assertion_fails,
            "n_run_errors": v.n_run_errors,
            "good_test_fail_candidates": last_validation_failures,
            "action": "generated",
        })

    (out_dir / "loop_history.json").write_text(
        json.dumps({"stop_reason": stop_reason, "records": records},
                   indent=2), encoding="utf-8")
    return LoopResult(final_tests=combined_tests, stop_reason=stop_reason,
                      records=records, last_plan=plan, last_enh=enh,
                      last_outcome=outcome)