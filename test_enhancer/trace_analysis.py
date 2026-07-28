"""
Post-traitement des traces d'exécution pour guider le plan de test.

Deux analyses fournies au planner (planner.py), en plus du tableau brut :

  1. detect_boundary_candidates(table)
     Pour chaque variable numérique observée dans la trace, calcule les
     valeurs vues, le min/max, et signale les "candidats frontière" -
     zéro, changement de signe, valeurs négatives vues. Sert de preuve
     concrète pour la Boundary Value Analysis (le prof demande des tests
     à la frontière, juste en dessous, juste au-dessus - pas des frontières
     inventées, mais ancrées dans ce qui a été observé à l'exécution).

  2. detect_uncovered_lines(source_path, rows)
     Compare les lignes EXÉCUTABLES du fichier source (via ast) aux
     lignes réellement visitées par la trace -> donne au LLM une liste
     concrète de lignes jamais exécutées par les tests existants, à
     cibler pour la couverture de branches / MC/DC (au lieu de deviner).
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from .tracer import TraceRow

_NUMERIC_PREFIXES = tuple("-0123456789")


def _try_parse_number(text: str) -> float | None:
    """Tente de parser une repr() de variable comme un nombre. Renvoie None
    si ce n'est visiblement pas un nombre (évite de mal interpréter des
    chaînes/objets qui commencent par un chiffre, ex. '123abc')."""
    if not text or text[0] not in _NUMERIC_PREFIXES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def detect_boundary_candidates(table: list[dict], max_vars: int = 40) -> dict[str, dict]:
    """Repère, pour chaque variable numérique de la trace, des candidats
    frontière : min, max, zéro si traversé, changement de signe.

    Returns: {var_name: {"values": [...], "min":..., "max":...,
                          "crosses_zero": bool, "has_negative": bool,
                          "has_zero_value_observed": bool}}
    """
    seen: dict[str, list[float]] = defaultdict(list)
    for row in table:
        name = row.get("variable", "")
        val = row.get("value", "")
        if not name:
            continue
        num = _try_parse_number(str(val))
        if num is not None:
            seen[name].append(num)

    result: dict[str, dict] = {}
    for name, values in seen.items():
        if len(result) >= max_vars:
            break
        if len(values) < 2:
            continue  # une seule valeur observée -> rien à dire sur la frontière
        vmin, vmax = min(values), max(values)
        has_negative = any(v < 0 for v in values)
        has_positive = any(v > 0 for v in values)
        has_zero = any(v == 0 for v in values)
        result[name] = {
            "values": sorted(set(values))[:10],
            "min": vmin,
            "max": vmax,
            "crosses_zero": has_negative and has_positive,
            "has_negative": has_negative,
            "has_zero_value_observed": has_zero,
        }
    return result


def detect_uncovered_lines(source_path: Path, rows: list[TraceRow]) -> list[int]:
    """Renvoie les numéros de ligne EXÉCUTABLES du fichier source qui
    n'apparaissent JAMAIS dans la trace (candidats manque de couverture
    de branche / MC-DC)."""
    source_path = Path(source_path)
    if not source_path.exists():
        return []

    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    executable_lines: set[int] = set()
    for node in ast.walk(tree):
        if hasattr(node, "lineno") and not isinstance(
            node, (ast.Module, ast.arguments, ast.arg)
        ):
            executable_lines.add(node.lineno)

    target_name = str(source_path.resolve())
    traced_lines = {
        r.lineno for r in rows
        if str(Path(r.filename).resolve()) == target_name
    }

    return sorted(executable_lines - traced_lines)


def detect_compound_conditions(source_path: Path) -> list[dict]:
    """Repère les décisions COMPOSÉES (if/while avec 'and'/'or') dans le
    fichier source : ce sont les points où la couverture de branche seule
    NE SUFFIT PAS - il faut MC/DC (montrer que chaque sous-condition
    affecte indépendamment le résultat de la décision).

    Returns: [{"lineno": int, "condition": "<code source>", "n_subconditions": int}]
    """
    source_path = Path(source_path)
    if not source_path.exists():
        return []
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    results = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            test = node.test
            if isinstance(test, ast.BoolOp):
                n_sub = len(test.values)
                lineno = node.lineno
                snippet = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
                results.append({
                    "lineno": lineno,
                    "condition": snippet,
                    "n_subconditions": n_sub,
                })
    return results


def render_trace_analysis_summary(
    boundary_candidates: dict[str, dict],
    uncovered_lines: list[int],
    compound_conditions: list[dict],
) -> str:
    """Rend un résumé texte compact à injecter dans le prompt du planner."""
    lines = []

    lines.append("### Boundary candidates (min/max/values_seen/crosses_zero)")
    if boundary_candidates:
        for name, info in boundary_candidates.items():
            flag = " [traverse zéro -> tester juste en dessous / à zéro / juste au-dessus]" if info["crosses_zero"] else ""
            lines.append(
                f"- {name}: min={info['min']}, max={info['max']}, "
                f"values_seen={info['values']}{flag}"
            )
    else:
        lines.append("(aucune variable numérique tracée)")

    lines.append("")
    lines.append("### Lignes JAMAIS exécutées par les tests existants (candidats branch coverage)")
    if uncovered_lines:
        preview = uncovered_lines[:30]
        suffix = " ..." if len(uncovered_lines) > 30 else ""
        lines.append(f"{preview}{suffix}")
    else:
        lines.append("(aucune détectée - ou fichier non résolu)")

    lines.append("")
    lines.append("### Décisions composées (and/or) - candidats MC/DC")
    if compound_conditions:
        for c in compound_conditions:
            lines.append(f"- L{c['lineno']}: `{c['condition']}` ({c['n_subconditions']} sous-conditions)")
    else:
        lines.append("(aucune décision composée détectée dans le fichier ciblé)")

    return "\n".join(lines)