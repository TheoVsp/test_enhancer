"""
Pipeline orchestrateur : enchaîne toutes les étapes du whiteboard.

  1. charger l'instance (gold patch + tests)
  2. préparer le repo (apply patch)
  3. run test cases sous tracer
  5. construire tableau + code annoté
  6. demander au LLM de renforcer les tests

Produit, dans runs/<instance_id>/ :
  - variable_table.csv         (le tableau d'évolution des variables)
  - variable_table.xlsx        (si openpyxl dispo)
  - annotated_<file>.py        (le code annoté inline)
  - analysis.json              (l'analyse + les tests renforcés du LLM)
  - run_log.txt                (stdout/stderr de l'exécution des tests)
"""
from __future__ import annotations

import json
from pathlib import Path

from . import artifacts, enhancer, evaluate, planner, swe_runner, validate
from .config import WORK_DIR
from .dataset import Instance, get_instance


def _get_patched_files(patch_text: str) -> list[str]:
    """Extrait les noms des fichiers modifiés par un patch."""
    files = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            files.append(Path(path).name)
    return files


def run_pipeline(
    instance_id: str,
    do_enhance: bool = True,
    use_agent_patch: bool | None = None,
) -> Path:
    """Exécute le pipeline complet sur une instance SWE-bench Lite.

    Args:
        instance_id: ex. "sympy__sympy-24909".
        do_enhance: si False, on s'arrête après les artefacts (pas d'appel LLM).
        use_agent_patch: si True, applique le patch de l'agent (soumission
            locale) au lieu du gold patch. None = auto selon la config.
    """
    print(f"\n=== Pipeline pour {instance_id} ===")
    out_dir = WORK_DIR / instance_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Charger l'instance
    print("[1] Chargement de l'instance...")
    instance: Instance = get_instance(instance_id, use_agent_patch=use_agent_patch)
    patch_kind = "AGENT" if instance.agent_patch.strip() else "GOLD"
    print(f"    repo={instance.repo}  #FAIL_TO_PASS={len(instance.fail_to_pass)}  "
          f"patch={patch_kind}")

    # 2. Préparer le repo (clone + checkout + apply gold patch + test patch)
    print("[2] Préparation du repo (clone + patch)...")
    repos_root = WORK_DIR / "_repos"
    repo_dir = swe_runner.prepare_repo(instance, repos_root)
    swe_runner.install_repo(repo_dir)

    # 3 & 5. Exécuter les tests sous tracer
    print("[3] Exécution des tests FAIL_TO_PASS sous tracer...")
    node_ids = swe_runner.resolve_node_ids(instance.fail_to_pass, instance.test_patch)
    print(f"    node ids résolus : {node_ids}")
    result = swe_runner.run_tests_traced(repo_dir, node_ids)
    
    (out_dir / "run_log.txt").write_text(
        f"success={result.success}\n\n--- STDOUT ---\n{result.stdout}\n"
        f"\n--- STDERR ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    print(f"    tests success={result.success}  "
          f"trace_rows={len(result.tracer.rows) if result.tracer else 0}")

    rows = result.tracer.rows if result.tracer else []

    # Construire le tableau de variables
    print("[5] Construction du tableau de variables + code annoté...")
    table = artifacts.build_variable_table(rows)
    artifacts.write_table_csv(table, out_dir / "variable_table.csv")
    artifacts.write_table_xlsx(table, out_dir / "variable_table.xlsx")

    # Annoter les fichiers source touchés par la trace
    traced_files = sorted({r.filename for r in rows})
    patched_names = _get_patched_files(instance.gold_patch)
    
    annotated_main = ""
    main_filename = ""
    
    for fpath in traced_files:
        fp = Path(fpath)
        if not fp.exists():
            continue
        out_annotated = out_dir / f"annotated_{fp.name}"
        artifacts.annotate_source(fp, rows, out_annotated)
        
        # Priorité : on garde le fichier annoté qui correspond au gold patch
        if fp.name in patched_names and not annotated_main:
            annotated_main = out_annotated.read_text(encoding="utf-8")
            main_filename = fp.name

    # Fallback au cas où aucun fichier tracé ne matche le patch
    if not annotated_main and traced_files:
        first_fp = Path(traced_files[0])
        fallback_annotated = out_dir / f"annotated_{first_fp.name}"
        if fallback_annotated.exists():
            annotated_main = fallback_annotated.read_text(encoding="utf-8")
            main_filename = first_fp.name

    if main_filename:
        print(f"    -> Fichier pertinent sélectionné pour le LLM : {main_filename}")

    # On détermine le chemin relatif du fichier de test original (pour y
    # placer les tests renforcés au moment de la validation).
    base_test_path = ""
    test_files = swe_runner.extract_test_files(instance.test_patch)
    if test_files:
        base_test_path = test_files[0]

    # 6. PLAN DE TEST (nouvelle étape) : le LLM identifie les faiblesses de
    #    couverture en raisonnant avec les concepts du test logiciel.
    if do_enhance:
        existing_tests = instance.test_patch
        annotated = annotated_main or "(pas de code tracé)"

        print("[6] Plan de test (concepts du test logiciel)...")
        plan = planner.make_plan(
            annotated_code=annotated,
            variable_table=table,
            existing_tests=existing_tests,
        )
        print(f"    -> {len(plan.items)} objectif(s) de test planifie(s)")

        # Fichier Markdown qui retranscrit le RAISONNEMENT du LLM (demande du
        # prof) : ou il voit une faiblesse, et pourquoi.
        reasoning_md = planner.render_reasoning_markdown(plan, instance_id)
        (out_dir / "test_plan_reasoning.md").write_text(reasoning_md, encoding="utf-8")
        print(f"    -> raisonnement ecrit : test_plan_reasoning.md")

        # 7. GENERATION des tests A PARTIR DU PLAN.
        print("[7] Generation des tests a partir du plan...")
        enh = enhancer.enhance_tests(
            annotated_code=annotated,
            variable_table=table,
            existing_tests=existing_tests,
            plan=plan,
        )
        print(f"    analyse: {enh.analysis[:120]}...")

        # 8. EVALUATION (comparaison avec les tests originaux).
        print("[8] Evaluation : comparaison tests originaux vs renforces...")
        metrics = evaluate.compare(existing_tests, enh.enhanced_tests)
        delta = metrics["delta"]
        print(f"    Delta assertions={delta['n_assertions']:+d}  "
              f"Delta fonctions de test={delta['n_test_functions']:+d}")

        # 9. VALIDATION + BOUCLE DE REPARATION.
        #    - tests qui PLANTENT (errors) -> repares en boucle
        #    - tests qui echouent sur ASSERTION (failures) -> gardes et signales
        print("[9] Validation + reparation (tests qui ne tournent pas)...")
        outcome = validate.validate_with_repair(
            repo_dir=repo_dir,
            enhanced_tests=enh.enhanced_tests,
            annotated_code=annotated,
            base_test_path=base_test_path or None,
            max_iterations=3,
        )
        v = outcome.result
        print(f"    -> {v.n_passed} passent, {v.n_assertion_fails} echouent (assertion), "
              f"{v.n_run_errors} ne tournent pas  |  reparations: {outcome.iterations}")
        if outcome.has_assertion_failures:
            print(f"    [!] {v.n_assertion_fails} test(s) tournent mais echouent sur assertion "
                  f"-> GARDES et SIGNALES (peuvent reveler un vrai bug du patch)")
        if outcome.has_run_errors:
            print(f"    [!] Il reste des tests qui ne tournent pas apres "
                  f"{outcome.iterations} tentative(s) de reparation.")

        # Le code final (apres reparations eventuelles)
        (out_dir / "enhanced_tests.py").write_text(outcome.final_tests, encoding="utf-8")

        # Log complet de la derniere validation (diagnostic).
        (out_dir / "validation_log.txt").write_text(
            f"iterations de reparation: {outcome.iterations}  (repaired={outcome.repaired})\n"
            f"syntax_ok={v.syntax_ok}  collected_ok={v.collected_ok}  passed={v.passed}\n"
            f"n_passed={v.n_passed}  n_failed={v.n_failed}  n_errors={v.n_errors}\n"
            f"has_run_errors={outcome.has_run_errors}  "
            f"has_assertion_failures={outcome.has_assertion_failures}\n"
            f"notes={v.notes}\n"
            f"\n===== PYTEST STDOUT =====\n{v.stdout}\n"
            f"\n===== PYTEST STDERR =====\n{v.stderr}\n",
            encoding="utf-8",
        )
        print(f"    -> log de validation ecrit : validation_log.txt")

        # On sauvegarde tout dans analysis.json (enrichi avec le plan).
        (out_dir / "analysis.json").write_text(
            json.dumps(
                {
                    "plan": plan.as_dict(),
                    "analysis": enh.analysis,
                    "enhanced_tests": outcome.final_tests,
                    "metrics": metrics,
                    "validation": v.as_dict(),
                    "repair": {
                        "iterations": outcome.iterations,
                        "repaired": outcome.repaired,
                        "has_run_errors": outcome.has_run_errors,
                        "has_assertion_failures": outcome.has_assertion_failures,
                    },
                },
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        print("[6] (saute : do_enhance=False)")


    print(f"=== Terminé. Artefacts dans {out_dir} ===")
    return out_dir