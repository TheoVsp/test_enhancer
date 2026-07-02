"""
Pipeline orchestrateur : enchaîne toutes les étapes du whiteboard.

  1. charger l'instance (gold patch + tests)
  2. préparer le repo (apply patch)
  3. run CHAQUE test (FAIL_TO_PASS + PASS_TO_PASS) séparément sous tracer
     -> artefacts par test dans traces/<safe_test_name>/
  4. agréger toutes les traces pour le LLM
  5. construire tableau + code annoté (agrégés)
  6. plan de test + génération + validation

Produit, dans runs/<instance_id>/ :
  - traces/<safe_name>/variable_table.csv   (trace d'un test individuel)
  - traces/<safe_name>/run_log.txt
  - traces/<safe_name>/annotated_<file>.py
  - traces/summary.json                     (résumé pass/fail de chaque test)
  - variable_table.csv                      (trace agrégée, tous tests confondus)
  - variable_table.xlsx                     (si openpyxl dispo)
  - annotated_<file>.py                     (code annoté agrégé)
  - test_plan_reasoning.md
  - enhanced_tests.py
  - validation_log.txt
  - analysis.json
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import artifacts, enhancer, evaluate, planner, swe_runner, validate, docker_runner
from .config import WORK_DIR
from .dataset import Instance, get_instance
from .tracer import TraceRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_patched_files(patch_text: str) -> tuple[list[str], list[str]]:
    """Extrait les noms (et chemins relatifs) des fichiers modifiés par un patch."""
    files: list[str] = []
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            files.append(Path(path).name)
            paths.append(path)
    return files, paths


def _safe_dir_name(test_id: str) -> str:
    """Convertit un node id pytest en nom de dossier valide (max 120 chars).

    Ex. 'sympy/test_foo.py::TestClass::test_bar' -> 'sympy__test_foo.py__TestClass__test_bar'
    """
    safe = re.sub(r"[^\w.\-]", "__", test_id)
    return safe[:120]


def _save_test_artifacts(
    test_dir: Path,
    result,               # RunResult
    patched_names: list[str],
    log_prefix: str = "",
) -> tuple[list[dict], str, str]:
    """Sauvegarde les artefacts d'un run individuel dans test_dir.

    Returns:
        (variable_table, annotated_main_text, main_filename)
    """
    test_dir.mkdir(parents=True, exist_ok=True)

    # run_log
    (test_dir / "run_log.txt").write_text(
        f"{log_prefix}success={result.success}\n\n--- STDOUT ---\n{result.stdout}\n"
        f"\n--- STDERR ---\n{result.stderr}\n",
        encoding="utf-8",
    )

    rows: list[TraceRow] = result.tracer.rows if result.tracer else []

    # variable_table
    table = artifacts.build_variable_table(rows)
    artifacts.write_table_csv(table, test_dir / "variable_table.csv")

    # annotated source files
    traced_files = sorted({r.filename for r in rows})
    annotated_main = ""
    main_filename = ""

    for fpath in traced_files:
        fp = Path(fpath)
        if not fp.exists():
            continue
        out_annotated = test_dir / f"annotated_{fp.name}"
        artifacts.annotate_source(fp, rows, out_annotated)
        if fp.name in patched_names and not annotated_main:
            annotated_main = out_annotated.read_text(encoding="utf-8")
            main_filename = fp.name

    # fallback: first traced file
    if not annotated_main and traced_files:
        first_fp = Path(traced_files[0])
        fallback = test_dir / f"annotated_{first_fp.name}"
        if fallback.exists():
            annotated_main = fallback.read_text(encoding="utf-8")
            main_filename = first_fp.name

    return table, annotated_main, main_filename


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    instance_id: str,
    do_enhance: bool = True,
    use_docker: bool = False,
    force_rebuild: bool = False,
    use_agent_patch: bool | None = None,
) -> Path:
    """Exécute le pipeline complet sur une instance SWE-bench Lite.

    Args:
        instance_id:      ex. "sympy__sympy-24909".
        do_enhance:       si False, on s'arrête après les artefacts (pas LLM).
        use_docker:       exécuter dans Docker plutôt qu'en local.
        force_rebuild:    forcer le rebuild de l'image Docker.
        use_agent_patch:  utiliser le patch de l'agent au lieu du gold patch.
    """
    print(f"\n=== Pipeline pour {instance_id} ===")
    out_dir = WORK_DIR / instance_id
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = out_dir / "traces"
    traces_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Charger l'instance
    # ------------------------------------------------------------------
    print("[1] Chargement de l'instance...")
    instance: Instance = get_instance(instance_id, use_agent_patch=use_agent_patch)
    patch_kind = "AGENT" if instance.agent_patch.strip() else "GOLD"
    print(f"    repo={instance.repo}  "
          f"#FAIL_TO_PASS={len(instance.fail_to_pass)}  "
          f"#PASS_TO_PASS={len(instance.pass_to_pass)}  "
          f"patch={patch_kind}")

    # ------------------------------------------------------------------
    # 2. Préparer le repo
    # ------------------------------------------------------------------
    print("[2] Préparation du repo (clone + patch)...")
    repos_root = WORK_DIR / "_repos"
    repo_dir = swe_runner.prepare_repo(instance, repos_root)
    swe_runner.install_repo(repo_dir)

    patched_names, patched_paths = _get_patched_files(instance.gold_patch)

    # Pre-build Docker image once (if needed) so per-test runs reuse it.
    if use_docker:
        print("    [DOCKER] pré-construction de l'image (si nécessaire)...")
        docker_runner.build_image(instance, force_rebuild=force_rebuild)

    # ------------------------------------------------------------------
    # 3. Résoudre les node ids pour les deux suites
    # ------------------------------------------------------------------
    f2p_ids = swe_runner.resolve_node_ids(instance.fail_to_pass, instance.test_patch)
    p2p_ids = swe_runner.resolve_node_ids(instance.pass_to_pass,  instance.test_patch)

    all_tests: list[tuple[str, str]] = (
        [("FAIL_TO_PASS", tid) for tid in f2p_ids]
        + [("PASS_TO_PASS", tid) for tid in p2p_ids]
    )
    print(f"[3] {len(f2p_ids)} FAIL_TO_PASS + {len(p2p_ids)} PASS_TO_PASS "
          f"= {len(all_tests)} test(s) à tracer individuellement")

    # ------------------------------------------------------------------
    # 4. Boucle par test — trace + artefacts individuels
    # ------------------------------------------------------------------
    all_rows: list[TraceRow] = []
    summary: list[dict] = []
    aggregated_annotated_main = ""
    aggregated_main_filename = ""
    # Path of the original test file (for validation step)
    base_test_path = ""
    test_files = swe_runner.extract_test_files(instance.test_patch)
    if test_files:
        base_test_path = test_files[0]
    for idx, (suite, test_id) in enumerate(all_tests, 1):
        safe_name = _safe_dir_name(test_id)
        test_dir = traces_dir / safe_name
        print(f"    [{idx}/{len(all_tests)}] {suite}  {test_id}")

        if use_docker:
            result = docker_runner.run_single_test_traced_docker(
                instance, test_id,
                force_rebuild=False,          # image déjà construite
                target_files=patched_paths,
            )
        else:
            result = swe_runner.run_single_test_traced(
                repo_dir, test_id,
                target_files=patched_paths,
            )

        rows = result.tracer.rows if result.tracer else []
        print(f"        success={result.success}  trace_rows={len(rows)}")

        table, annotated_code, ann_fname = _save_test_artifacts(
            test_dir, result, patched_names,
            log_prefix=f"suite={suite}  test={test_id}\n",
        )

        
        all_rows.extend(rows)
        if do_enhance:

            plan = planner.make_plan(
                annotated_code=annotated_code,
                variable_table=table,
                existing_tests=instance.test_patch,
            )

            reasoning = planner.render_reasoning_markdown(plan, test_id)
            (test_dir / "test_plan_reasoning.md").write_text(
                reasoning,
                encoding="utf-8",
            )

            enhancement = enhancer.enhance_tests(
                annotated_code=annotated_code,
                variable_table=table,
                existing_tests=instance.test_patch,
                plan=plan,
            )

            metrics = evaluate.compare(
                instance.test_patch,
                enhancement.enhanced_tests,
            )

            outcome = validate.validate_with_repair(
                repo_dir=repo_dir,
                enhanced_tests=enhancement.enhanced_tests,
                annotated_code=annotated_code,
                base_test_path=base_test_path or None,
                max_iterations=3,
            )

            (test_dir / "enhanced_tests.py").write_text(
                outcome.final_tests,
                encoding="utf-8",
            )

            v = outcome.result

            (test_dir / "validation_log.txt").write_text(
                f"iterations de réparation: {outcome.iterations}  (repaired={outcome.repaired})\n"
                f"syntax_ok={v.syntax_ok}  collected_ok={v.collected_ok}  passed={v.passed}\n"
                f"n_passed={v.n_passed}  n_failed={v.n_failed}  n_errors={v.n_errors}\n"
                f"has_run_errors={outcome.has_run_errors}  "
                f"has_assertion_failures={outcome.has_assertion_failures}\n"
                f"notes={v.notes}\n"
                f"\n===== PYTEST STDOUT =====\n{v.stdout}\n"
                f"\n===== PYTEST STDERR =====\n{v.stderr}\n",
                encoding="utf-8",
            )

            (test_dir / "analysis.json").write_text(
                json.dumps(
                    {
                        "suite": suite,
                        "test_id": test_id,
                        "analysis": enhancement.analysis,
                        "plan": plan.as_dict(),
                        "metrics": metrics,
                        "validation": outcome.result.as_dict(),
                        "repair": {
                            "iterations": outcome.iterations,
                            "repaired": outcome.repaired,
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        else:
            print("(sauté : do_enhance=False)")

        summary.append(
            {
                "suite": suite,
                "test_id": test_id,
                "trace_dir": safe_name,
                "success": result.success,
                "n_trace_rows": len(rows),
            }
        )
    (traces_dir / "summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

    traced_files_all = sorted({r.filename for r in all_rows})
    for fpath in traced_files_all:
        fp = Path(fpath)
        if not fp.exists():
            continue
        out_annotated = out_dir / f"annotated_{fp.name}"
        artifacts.annotate_source(fp, all_rows, out_annotated)
       
    print(f"=== Terminé. Artefacts dans {out_dir} ===")
    return out_dir