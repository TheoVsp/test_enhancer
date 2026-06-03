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

from . import artifacts, enhancer, evaluate, swe_runner, validate
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

    # 6. Renforcer les tests via LLM
    if do_enhance:
        print("[6] Analyse LLM + renforcement des tests...")
        existing_tests = instance.test_patch
        enh = enhancer.enhance_tests(
            annotated_code=annotated_main or "(pas de code tracé)",
            variable_table=table,
            existing_tests=existing_tests,
        )
        print(f"    analyse: {enh.analysis[:120]}...")

        # --- Point 1 : ÉVALUATION (comparaison avec les tests originaux) ------
        print("[7] Évaluation : comparaison tests originaux vs renforcés...")
        metrics = evaluate.compare(existing_tests, enh.enhanced_tests)
        delta = metrics["delta"]
        print(f"    Δ assertions={delta['n_assertions']:+d}  "
              f"Δ fonctions de test={delta['n_test_functions']:+d}")

        # --- Point 2 : VALIDATION (les tests renforcés passent-ils ?) ---------
        print("[8] Validation : exécution des tests renforcés sur le patch...")
        validation = validate.validate_enhanced_tests(
            repo_dir=repo_dir,
            enhanced_tests=enh.enhanced_tests,
            base_test_path=base_test_path or None,
        )
        verdict = "VALIDES" if validation.is_valid else "REJETÉS"
        print(f"    -> tests renforcés {verdict} "
              f"(syntax={validation.syntax_ok}, collecte={validation.collected_ok}, "
              f"passed={validation.passed}, {validation.n_passed}p/{validation.n_failed}f)")
        if validation.notes:
            for note in validation.notes:
                print(f"       note: {note}")

        # On écrit les tests renforcés en .py lisible (plus pratique que le JSON
        # échappé pour les relire / déboguer).
        (out_dir / "enhanced_tests.py").write_text(
            enh.enhanced_tests, encoding="utf-8"
        )

        # On écrit le log complet de la validation (sortie pytest) pour pouvoir
        # diagnostiquer POURQUOI les tests échouent (hallucination de valeur,
        # mauvaise API, import manquant...). C'est essentiel pour l'analyse.
        (out_dir / "validation_log.txt").write_text(
            f"verdict: {verdict}\n"
            f"syntax_ok={validation.syntax_ok}  collected_ok={validation.collected_ok}  "
            f"passed={validation.passed}\n"
            f"n_passed={validation.n_passed}  n_failed={validation.n_failed}  "
            f"n_errors={validation.n_errors}\n"
            f"notes={validation.notes}\n"
            f"\n===== PYTEST STDOUT =====\n{validation.stdout}\n"
            f"\n===== PYTEST STDERR =====\n{validation.stderr}\n",
            encoding="utf-8",
        )
        print(f"    -> log de validation écrit : validation_log.txt")

        # On sauvegarde tout dans analysis.json (enrichi)
        (out_dir / "analysis.json").write_text(
            json.dumps(
                {
                    "analysis": enh.analysis,
                    "enhanced_tests": enh.enhanced_tests,
                    "metrics": metrics,
                    "validation": validation.as_dict(),
                },
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        print("[6] (sauté : do_enhance=False)")

    print(f"=== Terminé. Artefacts dans {out_dir} ===")
    return out_dir