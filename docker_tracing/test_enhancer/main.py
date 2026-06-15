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

    # Lancer le pipeline complet dans Docker (trace + LLM)
    python -m test_enhancer.main --instance sympy__sympy-20590 --docker

    # Lancer le pipeline dans Docker SANS appel LLM (juste trace + artefacts)
    python -m test_enhancer.main --instance sympy__sympy-20590 --no-enhance --docker
"""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Enhancer V1")
    parser.add_argument("--instance", help="instance_id SWE-bench Lite à traiter")
    parser.add_argument("--no-enhance", action="store_true",
                        help="ne pas appeler le LLM (trace + artefacts seulement)")
    parser.add_argument("--list", type=int, metavar="N",
                        help="lister les N premières instances et quitter")
    parser.add_argument("--demo", action="store_true",
                        help="lancer l'exemple jouet local (voir demo_local.py)")
    parser.add_argument("--docker", action="store_true",
                    help="exécuter les tests dans un conteneur Docker SWE-bench")

    args = parser.parse_args()

    if args.list:
        from ._hub import load_instances
        instances = load_instances(limit=args.list)
        print(f"{len(instances)} instances :")
        for inst in instances:
            print(f"  - {inst.instance_id:35s} repo={inst.repo:25s} "
                  f"#F2P={len(inst.fail_to_pass)}")
        return

    if args.demo:
        from .demo_local import run_demo
        run_demo()
        return

    if not args.instance:
        parser.error("précise --instance, --list N, ou --demo")

    from .pipeline import run_pipeline
    run_pipeline(args.instance, do_enhance=not args.no_enhance, use_docker=args.docker)


if __name__ == "__main__":
    main()
