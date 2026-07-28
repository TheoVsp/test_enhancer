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

Deux entrées publiques, qui partagent tout le corps de la boucle (`_run_loop`)
et ne diffèrent que par OÙ/COMMENT la couverture est mesurée et les tests
validés :

  - run_enhancement_loop        : exécution LOCALE (repo_dir sur l'hôte).
  - run_enhancement_loop_docker : exécution dans un conteneur Docker
                                   persistant déjà démarré et patché (voir
                                   docker_runner.start_persistent_container).
                                   La mesure de couverture tourne alors DANS
                                   le conteneur (coverage_probe.
                                   measure_on_prepared_repo_docker), et la
                                   validation/réparation utilise
                                   validate.validate_with_repair_docker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import enhancer, planner, validate


@dataclass
class LoopResult:
    final_tests: str
    stop_reason: str                    # full_coverage | plateau | max_iterations | empty_plan | empty_generation
    records: list[dict] = field(default_factory=list)
    last_plan: "planner.TestPlan | None" = None
    last_enh: "enhancer.EnhancementResult | None" = None
    last_outcome: object = None         # RepairOutcome de validate
    all_traces: list = field(default_factory=list)
    all_compliance: list = field(default_factory=list)  # list[(iteration, text)]


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


def _run_loop(
    out_dir: Path,
    annotated: str,
    agg_table: list[dict],
    existing_tests: str,
    base_test_path: str,
    measure_coverage: Callable[[str], dict],
    render_weakness: Callable[[dict], str],
    validate_candidate: Callable[[str], "validate.RepairOutcome"],
    max_iterations: int = 3,
) -> LoopResult:
    """Corps commun de la boucle d'enrichissement, indépendant du mode
    d'exécution (local vs Docker).

    Args:
        measure_coverage: combined_tests (str, "" si aucun test généré encore)
            -> dict au format de coverage_probe.measure_on_prepared_repo*.
            Responsable d'installer combined_tests là où les tests tournent
            (fichier hôte OU fichier dans le conteneur) AVANT de mesurer.
        render_weakness: cov dict -> texte lisible (coverage_probe.render_weakness_map).
        validate_candidate: candidate test code -> validate.RepairOutcome
            (validate_with_repair OU validate_with_repair_docker).
    """
    llm_raw_dir = out_dir / "llm_raw"
    llm_raw_dir.mkdir(exist_ok=True)

    combined_tests = ""
    prev_missing: dict | None = None
    last_validation_failures: list[str] = []
    generated_goals: list[str] = []
    records: list[dict] = []
    plan = None
    enh = None
    outcome = None
    stop_reason = "max_iterations"
    all_traces: list = []
    all_compliance: list = []  # list[(iteration, text)]

    for it in range(1, max_iterations + 1):
        cov = measure_coverage(combined_tests)
        (out_dir / f"coverage_iter{it}.json").write_text(
            json.dumps(cov, indent=2), encoding="utf-8")

        missing = {f: e["missing_branches"] for f, e in cov["files"].items()}
        n_missing = sum(len(v) for v in missing.values())
        pct = {f: e["percent_covered"] for f, e in cov["files"].items()}
        print(f"    [it {it}] couverture: {pct} | branches manquantes: {n_missing}")

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

        weakness = render_weakness(cov)
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

        all_traces.extend(enh.traces)
        if enh.compliance_check.strip():
            all_compliance.append((it, enh.compliance_check))

        candidate = (combined_tests + "\n\n\n" + enh.enhanced_tests
                     if combined_tests.strip() else enh.enhanced_tests)
        outcome = validate_candidate(candidate)
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
                      last_outcome=outcome, all_traces=all_traces,
                      all_compliance=all_compliance)


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
    """Boucle d'enrichissement — exécution LOCALE (repo_dir sur l'hôte)."""
    import coverage_probe          # module racine (cwd = racine du projet)

    dest_rel = Path(base_test_path).parent / "test_te_enhanced.py"

    def measure_coverage(combined_tests: str) -> dict:
        extra = []
        if combined_tests.strip():
            (repo_dir / dest_rel).write_text(combined_tests, encoding="utf-8")
            extra = [str(dest_rel)]
        return coverage_probe.measure_on_prepared_repo(
            repo_dir, node_ids, patched_paths, extra_node=extra, verbose=False)

    def render_weakness(cov: dict) -> str:
        return coverage_probe.render_weakness_map(cov, repo_dir)

    def validate_candidate(candidate: str):
        return validate.validate_with_repair(
            repo_dir=repo_dir, enhanced_tests=candidate,
            annotated_code=annotated, base_test_path=base_test_path or None,
            max_iterations=3)

    return _run_loop(
        out_dir=out_dir, annotated=annotated, agg_table=agg_table,
        existing_tests=existing_tests, base_test_path=base_test_path,
        measure_coverage=measure_coverage, render_weakness=render_weakness,
        validate_candidate=validate_candidate, max_iterations=max_iterations)


def run_enhancement_loop_docker(
    container_name: str,
    docker_runner_module,
    out_dir: Path,
    node_ids: list[str],
    patched_paths: list[str],
    local_sources_dir: Path,
    annotated: str,
    agg_table: list[dict],
    existing_tests: str,
    base_test_path: str,
    max_iterations: int = 3,
) -> LoopResult:
    """Boucle d'enrichissement — exécution DANS un conteneur Docker persistant
    déjà démarré et patché (voir docker_runner.start_persistent_container).

    La mesure de couverture ET la validation/réparation tournent toutes les
    deux DANS le conteneur (pas sur un repo_dir hôte) :
      - coverage_probe.measure_on_prepared_repo_docker
      - validate.validate_with_repair_docker

    `local_sources_dir` est un dossier HÔTE contenant une copie (via
    docker cp, faite une fois par pipeline.py) des fichiers sources patchés,
    utilisé uniquement pour afficher le code source dans la weakness map
    (render_weakness_map a besoin de lire les lignes source correspondant
    aux numéros de ligne manquants).
    """
    import coverage_probe          # module racine (cwd = racine du projet)

    dest_rel = Path(base_test_path).parent / "test_te_enhanced.py"
    tmp_dir = out_dir / "_loop_tmp"
    tmp_dir.mkdir(exist_ok=True)

    def measure_coverage(combined_tests: str) -> dict:
        extra_local_path = None
        if combined_tests.strip():
            extra_local_path = tmp_dir / "test_te_enhanced.py"
            extra_local_path.write_text(combined_tests, encoding="utf-8")
        return coverage_probe.measure_on_prepared_repo_docker(
            container_name, docker_runner_module, node_ids, patched_paths,
            extra_test_local_path=extra_local_path,
            extra_node_rel=str(dest_rel) if extra_local_path is not None else None,
            verbose=False)

    def render_weakness(cov: dict) -> str:
        return coverage_probe.render_weakness_map(cov, local_sources_dir)

    def validate_candidate(candidate: str):
        return validate.validate_with_repair_docker(
            container_name=container_name,
            docker_runner_module=docker_runner_module,
            enhanced_tests=candidate, annotated_code=annotated,
            base_test_path=base_test_path or None, max_iterations=3)

    return _run_loop(
        out_dir=out_dir, annotated=annotated, agg_table=agg_table,
        existing_tests=existing_tests, base_test_path=base_test_path,
        measure_coverage=measure_coverage, render_weakness=render_weakness,
        validate_candidate=validate_candidate, max_iterations=max_iterations)
