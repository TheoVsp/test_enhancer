"""
Démo locale autonome : teste le pipeline trace -> tableau -> code annoté
sur un petit exemple, SANS SWE-bench, SANS Docker, SANS clé API.

C'est le moyen le plus rapide de vérifier que la mécanique fonctionne.
Lance :  python -m test_enhancer.main --demo
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import artifacts
from .config import WORK_DIR
from .tracer import VariableTracer


# --- Un petit "code source" buggé-mais-patché qu'on va tracer ---------------
EXAMPLE_SOURCE = '''\
def classify_number(n):
    label = "zero"
    if n > 0:
        label = "positive"
        if n % 2 == 0:
            label = "positive_even"
    elif n < 0:
        label = "negative"
    return label


def sum_until(n):
    total = 0
    for i in range(1, n + 1):
        total = total + i
    return total
'''

# Un test "faible" : il ne vérifie qu'un seul cas, manque les branches.
EXAMPLE_TESTS = '''\
def test_classify():
    assert classify_number(0) == "zero"
'''


def run_demo() -> None:
    print("\n=== DÉMO LOCALE (sans SWE-bench/Docker/LLM) ===")
    tmp = Path(tempfile.mkdtemp(prefix="te_demo_"))
    src_path = tmp / "example_module.py"
    src_path.write_text(EXAMPLE_SOURCE, encoding="utf-8")

    # On importe dynamiquement le module et on exécute quelques appels
    # sous le tracer, pour simuler "run test cases + get debugging info".
    import importlib.util
    spec = importlib.util.spec_from_file_location("example_module", src_path)
    module = importlib.util.module_from_spec(spec)

    tracer = VariableTracer(watch_dir=tmp)
    with tracer:
        spec.loader.exec_module(module)
        # quelques appels couvrant plusieurs branches
        module.classify_number(0)
        module.classify_number(4)
        module.classify_number(-3)
        module.sum_until(5)

    print(f"  Trace capturée : {len(tracer.rows)} événements")

    out_dir = WORK_DIR / "_demo"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Tableau de variables
    table = artifacts.build_variable_table(tracer.rows)
    csv_path = artifacts.write_table_csv(table, out_dir / "variable_table.csv")
    print(f"  Tableau CSV écrit : {csv_path}  ({len(table)} lignes)")

    # Code annoté
    annotated = artifacts.annotate_source(
        src_path, tracer.rows, out_dir / "annotated_example_module.py"
    )
    print(f"  Code annoté écrit : {annotated}")
    print("\n--- Aperçu du code annoté ---")
    print(annotated.read_text(encoding="utf-8"))

    print("\n--- Aperçu du tableau (10 premières lignes) ---")
    for entry in table[:10]:
        print(f"  step={entry['step']:3d} {entry['function']:18s} "
              f"L{entry['lineno']:<3d} {entry['variable']:8s} = {entry['value']}")

    print("\n=== Démo terminée. Tout fonctionne si tu vois le code annoté ci-dessus. ===")
    print(f"Artefacts dans : {out_dir}")
