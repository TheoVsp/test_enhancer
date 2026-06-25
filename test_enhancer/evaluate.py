"""
Comparaison entre tests originaux et tests renforcés (évaluation V1).

Baseline V1 : "on compare avec les tests originaux".

Métriques simples et interprétables pour quantifier ce que les tests renforcés
apportent :
  - nombre d'assertions (proxy de la "force" d'un test)
  - nombre de fonctions de test
  - nombre de lignes de code de test

IMPORTANT : les tests ORIGINAUX arrivent au format DIFF (test_patch), pas en
Python pur. On extrait donc d'abord le code ajouté du diff avant de mesurer,
sinon ast.parse échoue et on compte 0 (bug constaté). On a aussi un comptage
de secours par regex quand le fragment extrait n'est pas parsable tel quel
(indentation partielle d'un hunk de diff).
"""
from __future__ import annotations

import ast
import re
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


def extract_added_code_from_diff(text: str) -> str:
    """Extrait le code Python d'un diff unifié.

    Conserve les lignes ajoutées ('+') et de contexte (' '), retire les
    préfixes et les en-têtes de diff. Si `text` n'est pas un diff, le renvoie
    tel quel. Résultat = le code tel qu'il existera après application du patch.
    """
    if "diff --git" not in text and "@@" not in text and not text.lstrip().startswith("---"):
        return text  # déjà du Python pur
    out = []
    for line in text.splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ")):
            continue
        if line.startswith("+"):
            out.append(line[1:])
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            out.append(line[1:])
        else:
            out.append(line)
    return "\n".join(out)


def _count_assertions_ast(node: ast.AST) -> int:
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


def _count_regex(code: str) -> tuple[int, int]:
    """Comptage de secours par regex (quand l'AST ne parse pas).

    Returns (n_test_functions, n_assertions).
    """
    n_tests = len(re.findall(r"^\s*def\s+test\w*\s*\(", code, re.MULTILINE))
    # 'assert ' (statement) + appels assertXxx( (unittest)
    n_assert_stmt = len(re.findall(r"^\s*assert\b", code, re.MULTILINE))
    n_assert_call = len(re.findall(r"\bassert[A-Za-z]\w*\s*\(", code))
    return n_tests, n_assert_stmt + n_assert_call


def measure_tests(test_code: str) -> TestMetrics:
    """Calcule les métriques statiques d'un bloc de code de test.

    Gère à la fois le Python pur ET les diffs (test_patch).
    """
    code = extract_added_code_from_diff(test_code)
    n_lines = len([l for l in code.splitlines() if l.strip()])

    try:
        tree = ast.parse(code)
        n_tests = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        )
        n_assert = _count_assertions_ast(tree)
        return TestMetrics(n_test_functions=n_tests, n_assertions=n_assert, n_lines=n_lines)
    except SyntaxError:
        # Fragment de diff non parsable (indentation partielle) -> regex
        n_tests, n_assert = _count_regex(code)
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
