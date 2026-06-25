"""
Script d'évaluation kill rate.

Usage :
    python run_kill_eval.py sympy__sympy-15345
    python run_kill_eval.py sympy__sympy-15345 sympy__sympy-20639 ...

Pour chaque instance, suppose que les tests renforcés ont DÉJÀ été générés
(fichier runs/<instance_id>/enhanced_tests.py). Lance ces tests sur le patch
GOLD puis sur le patch AGENT, compare, et reporte combien de patches sont
"tués" par les tests renforcés.

Prérequis : le repo de l'instance doit déjà être cloné/installé dans
runs/_repos/<instance_id> (ce qui est le cas après un run du pipeline).
"""
from __future__ import annotations

import sys
from pathlib import Path

from test_enhancer.dataset import get_instance
from test_enhancer import validate
import evaluate_kill


def eval_one(instance_id: str) -> evaluate_kill.InstanceKillResult | None:
    enh_path = Path(f"runs/{instance_id}/enhanced_tests.py")
    if not enh_path.exists():
        print(f"[SKIP] {instance_id} : pas de enhanced_tests.py "
              f"(lance d'abord le pipeline sur cette instance)")
        return None

    repo_dir = Path(f"runs/_repos/{instance_id}").resolve()
    if not repo_dir.exists():
        print(f"[SKIP] {instance_id} : repo non préparé dans runs/_repos/")
        return None

    inst_gold = get_instance(instance_id, use_agent_patch=False)
    inst_agent = get_instance(instance_id, use_agent_patch=True)

    print(f"[EVAL] {instance_id} : exécution sur GOLD puis AGENT...")
    result = evaluate_kill.evaluate_kill_for_instance(
        instance_id=instance_id,
        enhanced_tests_path=enh_path,
        repo_dir=repo_dir,
        base_commit=inst_gold.base_commit,
        gold_patch=inst_gold.patch_to_apply,
        agent_patch=inst_agent.patch_to_apply,
        test_patch=inst_gold.test_patch,
        validate_module=validate,
    )
    return result


def main():
    instance_ids = sys.argv[1:]
    if not instance_ids:
        print("Usage: python run_kill_eval.py <instance_id> [instance_id ...]")
        sys.exit(1)

    results = []
    for iid in instance_ids:
        r = eval_one(iid)
        if r:
            results.append(r)
            print("   ", r.summary())
            # Détail des kills (le livrable 3 du prof)
            for v in r.verdicts:
                if v.category == "KILL":
                    print(f"      KILL: {v.name} (passe sur gold, échoue sur agent)")
                elif v.category == "ANOMALY":
                    print(f"      ANOMALY: {v.name} (échoue gold mais passe agent — à investiguer)")

    # Bilan agrégé
    if results:
        n_total = len(results)
        n_killed = sum(1 for r in results if r.patch_killed)
        total_kills = sum(r.n_kills for r in results)
        total_hallu = sum(r.n_hallucinations for r in results)
        print("\n===== BILAN =====")
        print(f"Instances évaluées        : {n_total}")
        print(f"Patches tués (>=1 kill)   : {n_killed}")
        print(f"Total de tests 'kill'     : {total_kills}")
        print(f"Total d'hallucinations    : {total_hallu}")


if __name__ == "__main__":
    main()
