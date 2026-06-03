"""
Point d'entrée en ligne de commande.

Exemples :
    # Lister les premières instances disponibles
    python -m test_enhancer.main --list 5

    # Lancer le pipeline complet sur une instance (avec LLM)
    python -m test_enhancer.main --instance sympy__sympy-20590

    # Lancer SANS appel LLM (juste trace + tableau + code annoté)
    python -m test_enhancer.main --instance sympy__sympy-20590 --no-enhance

    # Tester le pipeline sur l'exemple jouet local (sans Docker, sans SWE-bench)
    python -m test_enhancer.main --demo

    # Lister les instances d'une soumission d'agent locale
    python -m test_enhancer.main --list-local 10

    # Lancer le pipeline en utilisant le patch de l'agent (pas le gold patch)
    #   (nécessite TE_LOCAL_SUBMISSION_DIR défini, voir config.py)
    python -m test_enhancer.main --instance sympy__sympy-15345 --use-agent-patch
"""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Enhancer")
    parser.add_argument("--instance", help="instance_id SWE-bench Lite à traiter")
    parser.add_argument("--no-enhance", action="store_true",
                        help="ne pas appeler le LLM (trace + artefacts seulement)")
    parser.add_argument("--list", type=int, metavar="N",
                        help="lister les N premières instances HuggingFace et quitter")
    parser.add_argument("--list-local", type=int, metavar="N",
                        help="lister N instances de la soumission locale (resolved) et quitter")
    parser.add_argument("--use-agent-patch", action="store_true",
                        help="utiliser le patch de l'agent (soumission locale) au lieu du gold patch")
    parser.add_argument("--demo", action="store_true",
                        help="lancer l'exemple jouet local (voir demo_local.py)")
    args = parser.parse_args()

    if args.list:
        from .dataset import load_instances
        instances = load_instances(limit=args.list)
        print(f"{len(instances)} instances :")
        for inst in instances:
            print(f"  - {inst.instance_id:35s} repo={inst.repo:25s} "
                  f"#F2P={len(inst.fail_to_pass)}")
        return

    if args.list_local:
        from .config import LOCAL_SUBMISSION_DIR
        if not LOCAL_SUBMISSION_DIR:
            parser.error("TE_LOCAL_SUBMISSION_DIR n'est pas défini "
                         "(voir config.py ou variable d'environnement).")
        from .local_dataset import LocalSubmission
        sub = LocalSubmission(LOCAL_SUBMISSION_DIR)
        resolved = sub.list_resolved_ids()[:args.list_local]
        print(f"{len(resolved)} instances resolved (sur {len(sub.list_resolved_ids())} au total) :")
        for iid in resolved:
            print(f"  - {iid}")
        return

    if args.demo:
        from .demo_local import run_demo
        run_demo()
        return

    if not args.instance:
        parser.error("précise --instance, --list N, --list-local N, ou --demo")

    from .pipeline import run_pipeline
    # use_agent_patch : si le flag est passé, on force ; sinon None = auto
    use_agent = True if args.use_agent_patch else None
    run_pipeline(args.instance, do_enhance=not args.no_enhance,
                 use_agent_patch=use_agent)


if __name__ == "__main__":
    main()
