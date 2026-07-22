"""
Lance le pipeline complet (test_enhancer.pipeline.run_pipeline) sur PLUSIEURS
instances SWE-bench Lite d'affilée, avec reprise (resume), filtrage, et
tolérance aux erreurs (une instance qui plante n'arrête pas les autres).

Placer ce fichier à la racine du projet, au même niveau que run_pipeline.py /
run_kill_eval.py (à côté du package test_enhancer/).

Usage :
    # Tourner sur TOUTES les instances du dataset (300 pour SWE-bench Lite)
    python run_all_instances.py

    # Se limiter aux 20 premières (utile pour un premier essai)
    python run_all_instances.py --limit 20

    # Ne garder qu'un repo précis
    python run_all_instances.py --repo sympy/sympy

    # Reprendre après un arrêt (saute les instances déjà terminées)
    python run_all_instances.py --resume

    # Utiliser Docker (voir docker_runner.py) au lieu de l'exécution locale
    python run_all_instances.py --docker

    # Utiliser le patch d'agent (soumission locale) plutôt que le gold patch
    python run_all_instances.py --use-agent-patch

    # Traiter une liste d'instances précises (un id par ligne dans un fichier)
    python run_all_instances.py --instances-file my_ids.txt

    # Ne PAS appeler le LLM (juste trace + artefacts, pour tester vite)
    python run_all_instances.py --no-enhance --limit 5

Produit :
    runs/_all_runs_summary.json   -> statut de chaque instance (ok/skip/fail)
    runs/_all_runs_summary.csv    -> même chose en CSV (plus facile à trier)

Une instance est considérée "déjà terminée" (et sautée en --resume) si
runs/<instance_id>/analysis.json existe (V1 : do_enhance=True) ou
runs/<instance_id>/variable_table.csv existe (si --no-enhance).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path

from test_enhancer.config import WORK_DIR
from test_enhancer.dataset import load_instances, Instance
from test_enhancer.pipeline import run_pipeline


@dataclass
class RunOutcome:
    instance_id: str
    repo: str
    status: str          # "ok" | "skipped" | "failed"
    seconds: float
    error: str = ""


def _already_done(instance_id: str, do_enhance: bool) -> bool:
    out_dir = WORK_DIR / instance_id
    if do_enhance:
        return (out_dir / "analysis.json").exists()
    return (out_dir / "variable_table.csv").exists()


def _load_ids_from_file(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def _select_instances(args) -> list[Instance]:
    """Résout la liste des instances à traiter selon les options CLI."""
    if args.instances_file:
        ids = _load_ids_from_file(Path(args.instances_file))
        print(f"[SELECT] {len(ids)} instance(s) lues depuis {args.instances_file}")
        # On charge le dataset complet une fois puis on filtre par id
        # (plus robuste que get_instance() répété, qui recharge le dataset
        # à chaque appel).
        all_instances = load_instances(limit=None)
        by_id = {inst.instance_id: inst for inst in all_instances}
        missing = [i for i in ids if i not in by_id]
        if missing:
            print(f"[WARN] {len(missing)} id(s) introuvables dans le dataset : "
                  f"{missing[:10]}{' ...' if len(missing) > 10 else ''}")
        instances = [by_id[i] for i in ids if i in by_id]
    else:
        instances = load_instances(limit=args.limit)

    if args.repo:
        instances = [i for i in instances if i.repo == args.repo]

    if args.start_index or args.end_index is not None:
        end = args.end_index if args.end_index is not None else len(instances)
        instances = instances[args.start_index:end]

    return instances


def _write_summary(outcomes: list[RunOutcome]) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    json_path = WORK_DIR / "_all_runs_summary.json"
    csv_path = WORK_DIR / "_all_runs_summary.csv"

    json_path.write_text(
        json.dumps([asdict(o) for o in outcomes], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["instance_id", "repo", "status", "seconds", "error"])
        writer.writeheader()
        for o in outcomes:
            writer.writerow(asdict(o))

    print(f"\n[SUMMARY] écrit : {json_path}  et  {csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lance le pipeline sur plusieurs instances SWE-bench Lite")
    parser.add_argument("--limit", type=int, default=None,
                        help="ne traiter que les N premières instances du dataset")
    parser.add_argument("--repo", type=str, default=None,
                        help="ne garder qu'un repo précis, ex. 'sympy/sympy'")
    parser.add_argument("--start-index", type=int, default=0,
                        help="indice de départ dans la liste sélectionnée (après --repo/--limit)")
    parser.add_argument("--end-index", type=int, default=None,
                        help="indice de fin (exclusif) dans la liste sélectionnée")
    parser.add_argument("--instances-file", type=str, default=None,
                        help="fichier texte avec un instance_id par ligne (remplace --limit/--repo)")
    parser.add_argument("--resume", action="store_true",
                        help="sauter les instances déjà terminées (analysis.json ou variable_table.csv présent)")
    parser.add_argument("--no-enhance", action="store_true",
                        help="ne pas appeler le LLM (trace + artefacts seulement)")
    parser.add_argument("--docker", action="store_true",
                        help="exécuter dans Docker (docker_runner.py) au lieu de l'exécution locale")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="forcer le rebuild des images Docker (ignoré si --docker absent)")
    parser.add_argument("--use-agent-patch", action="store_true",
                        help="utiliser le patch d'agent (soumission locale) au lieu du gold patch")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="arrêter tout le run à la première erreur (par défaut : on continue)")
    parser.add_argument("--dry-run", action="store_true",
                        help="afficher la liste des instances qui seraient traitées, sans rien lancer")
    args = parser.parse_args()

    instances = _select_instances(args)
    print(f"[PLAN] {len(instances)} instance(s) sélectionnée(s) au total")

    if args.dry_run:
        for inst in instances:
            done = _already_done(inst.instance_id, do_enhance=not args.no_enhance)
            tag = "(déjà fait)" if done else ""
            print(f"  - {inst.instance_id:35s} repo={inst.repo:25s} {tag}")
        return

    use_agent = True if args.use_agent_patch else None
    outcomes: list[RunOutcome] = []

    for idx, inst in enumerate(instances, 1):
        iid = inst.instance_id
        print(f"\n{'='*70}")
        print(f"[{idx}/{len(instances)}] {iid}  (repo={inst.repo})")
        print(f"{'='*70}")

        if args.resume and _already_done(iid, do_enhance=not args.no_enhance):
            print(f"  [SKIP] déjà terminé, --resume actif")
            outcomes.append(RunOutcome(iid, inst.repo, "skipped", 0.0))
            continue

        t0 = time.time()
        try:
            run_pipeline(
                iid,
                do_enhance=not args.no_enhance,
                use_docker=args.docker,
                force_rebuild=args.force_rebuild,
                use_agent_patch=use_agent,
            )
            elapsed = time.time() - t0
            outcomes.append(RunOutcome(iid, inst.repo, "ok", elapsed))
            print(f"  [OK] {iid} terminé en {elapsed:.1f}s")

        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - t0
            err_text = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL] {iid} a échoué après {elapsed:.1f}s : {err_text}", file=sys.stderr)
            traceback.print_exc()
            outcomes.append(RunOutcome(iid, inst.repo, "failed", elapsed, err_text))

            # On sauvegarde le résumé partiel à chaque échec, au cas où le
            # script est interrompu plus tard (Ctrl+C, crash Docker, etc.)
            _write_summary(outcomes)

            if args.stop_on_error:
                print("[STOP] --stop-on-error actif, arrêt du run.")
                break

    # ------------------------------------------------------------------
    # Bilan final
    # ------------------------------------------------------------------
    n_ok = sum(1 for o in outcomes if o.status == "ok")
    n_skip = sum(1 for o in outcomes if o.status == "skipped")
    n_fail = sum(1 for o in outcomes if o.status == "failed")
    total_time = sum(o.seconds for o in outcomes)

    print(f"\n{'='*70}")
    print("===== BILAN FINAL =====")
    print(f"Instances traitées : {len(outcomes)}")
    print(f"  OK      : {n_ok}")
    print(f"  Sautées : {n_skip}")
    print(f"  Échouées: {n_fail}")
    print(f"Temps total (hors sautées) : {total_time/60:.1f} min")

    if n_fail:
        print("\nInstances en échec :")
        for o in outcomes:
            if o.status == "failed":
                print(f"  - {o.instance_id} : {o.error}")

    _write_summary(outcomes)


if __name__ == "__main__":
    main()