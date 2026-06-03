"""
Comparaison entre tests originaux et tests renforcés (évaluation V1).

Tu as choisi comme baseline V1 : "on compare avec les tests originaux".
Ce module fournit des métriques simples et interprétables pour quantifier
ce que les tests renforcés apportent par rapport aux tests d'origine :

  - nombre d'assertions (souvent un bon proxy de la "force" d'un test)
  - nombre de fonctions de test
  - couverture de lignes/branches (si coverage.py est installé)

Pour la V1, on se concentre sur le comptage d'assertions et la couverture,
qui sont faciles à mesurer et à expliquer dans un rapport.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class TestMetrics:
    n_test_functions: int
    n_assertions: int
    n_lines: int

    def as_dict(self) -> dict:
        return {
            "n_test_functions": self.n_test_functions,
            "n_assertions": self.n_assertions,
            "n_lines": self.n_lines,
        }


def _count_assertions(node: ast.AST) -> int:
    """Compte les `assert` ET les appels à des méthodes assertXxx (unittest)."""
    count = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            count += 1
        elif isinstance(child, ast.Call):
            func = child.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name.startswith("assert"):
                count += 1
    return count


def measure_tests(test_code: str) -> TestMetrics:
    """Calcule les métriques statiques d'un bloc de code de test."""
    n_lines = len([l for l in test_code.splitlines() if l.strip()])
    try:
        tree = ast.parse(test_code)
    except SyntaxError:
        # le code peut être un diff/patch et non du python pur
        return TestMetrics(n_test_functions=0, n_assertions=0, n_lines=n_lines)

    n_tests = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )
    n_assert = _count_assertions(tree)
    return TestMetrics(n_test_functions=n_tests, n_assertions=n_assert, n_lines=n_lines)


def compare(original_tests: str, enhanced_tests: str) -> dict:
    """Compare originaux vs renforcés et renvoie un dict de deltas."""
    orig = measure_tests(original_tests)
    enh = measure_tests(enhanced_tests)
    return {
        "original": orig.as_dict(),
        "enhanced": enh.as_dict(),
        "delta": {
            "n_test_functions": enh.n_test_functions - orig.n_test_functions,
            "n_assertions": enh.n_assertions - orig.n_assertions,
            "n_lines": enh.n_lines - orig.n_lines,
        },
    }
