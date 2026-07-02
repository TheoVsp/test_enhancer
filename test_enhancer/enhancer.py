"""
Étape de génération des tests À PARTIR DU PLAN (approche plan-guided).

Avant : un seul appel LLM générique « renforce les tests ».
Maintenant : le LLM reçoit le PLAN DE TEST produit par planner.py et génère
les tests qui réalisent ce plan. La génération est donc ciblée sur les
faiblesses de couverture identifiées, pas au hasard.

L'objectif reste d'AMÉLIORER la suite (on ne supprime pas les tests existants ;
on ajoute des tests qui comblent les trous du plan).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import llm_client
from .planner import TestPlan, _truncate_table


SYSTEM_PROMPT = """You are a software testing expert. You are given a Python \
function (annotated with runtime values), the existing tests, and a TEST PLAN \
describing coverage gaps to fill.

Your job: write Python test code that REALISES the test plan. For each plan \
item, write one or more tests that achieve its goal using the stated technique \
and inputs.

CRITICAL RULES TO AVOID WRONG EXPECTED VALUES (this is the main failure mode):
- Do NOT guess expected output formats. Many libraries have non-obvious output \
conventions (ordering of terms, bracket styles, spacing). If you are not CERTAIN \
of the exact expected value, derive it from the runtime trace, or from the \
existing tests' assertions, which show the TRUE output format.
- Reuse the exact formatting conventions visible in the existing tests. If an \
existing assertion shows mcode(x) == "f[x, y, z]", follow that EXACT bracket and \
separator style for similar cases.
- Prefer asserting properties you are sure of (type, length, membership, \
substring) over guessing an exact string you are unsure about. A correct weaker \
assertion is better than a wrong strong one.
- For ordering-sensitive output (series, sums, polynomials), do NOT assume an \
order unless the trace or existing tests confirm it.
- Only assert an exact equality when the value is directly supported by the \
trace or by an existing test.

TEST STRUCTURE (very important for evaluation):
- Create ONE separate, atomic test function PER plan item. Do NOT put many unrelated assertions into a single giant test function.
- Give each function a descriptive name reflecting what it checks (e.g. test_prefix_multiplication_by_unit, test_prefix_zero_division).
- Each test function should focus on ONE behaviour, with a few closely related assertions at most. This way, if one assertion is wrong, only that small test fails instead of hiding all the others.
- Aim for roughly as many test functions as there are plan items.

Other rules:
- Build on the existing tests; do NOT remove existing assertions.
- Use only the function's public interface unless the existing tests do otherwise.
- Keep the tests runnable and self-contained (include needed imports).

Respond ONLY with a JSON object of the form:
{
  "analysis": "<short summary of what the new tests add, per plan item>",
  "enhanced_tests": "<the full test code as a single string>"
}
No markdown, no backticks, just the JSON object."""


@dataclass
class EnhancementResult:
    analysis: str
    enhanced_tests: str
    raw_response: str


def _render_plan(plan: TestPlan) -> str:
    lines = []
    for i, it in enumerate(plan.items, 1):
        lines.append(f"{i}. goal: {it.goal}")
        lines.append(f"   technique: {it.technique}")
        lines.append(f"   rationale: {it.rationale}")
        lines.append(f"   inputs: {it.inputs}")
    return "\n".join(lines) if lines else "(empty plan)"


def build_user_prompt(annotated_code: str, variable_table: list[dict],
                      existing_tests: str, plan: TestPlan) -> str:
    return f"""## Annotated source code (runtime values inline)
```python
{annotated_code}
```

## Variable evolution table (excerpt)
{_truncate_table(variable_table)}

## Existing tests
```python
{existing_tests}
```

## Test plan to realise
{_render_plan(plan)}

Write the tests that realise this plan now."""

def _as_text(value) -> str:
    """Force une valeur en chaîne (le LLM renvoie parfois analysis en dict/list
    au lieu d'une string, ce qui casse les usages type analysis[:120])."""
    if isinstance(value, str):
        return value
    import json as _json
    try:
        return _json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def enhance_tests(annotated_code: str, variable_table: list[dict],
                  existing_tests: str, plan: TestPlan) -> EnhancementResult:
    """Génère les tests renforcés à partir du plan de test."""
    user_prompt = build_user_prompt(annotated_code, variable_table,
                                     existing_tests, plan)
    parsed, raw = llm_client.call_json(SYSTEM_PROMPT, user_prompt)
    return EnhancementResult(
        analysis=_as_text(parsed.get("analysis", "")),
        enhanced_tests=parsed.get("enhanced_tests", ""),
        raw_response=raw,
    )


def repair_tests(failing_code: str, error_output: str,
                 annotated_code: str) -> str:
    """Demande au LLM de CORRIGER des tests qui NE TOURNENT PAS (étape 4).

    On ne corrige QUE les tests qui plantent (erreur de syntaxe, d'import,
    d'API) — pas ceux qui échouent sur une assertion. Le message d'erreur
    pytest est fourni pour guider la correction.

    Returns le code de test corrigé (string).
    """
    system = """You are fixing Python test code that FAILS TO RUN (syntax \
error, import error, wrong API usage). Do NOT change the testing intent; only \
fix what prevents the tests from running. Keep all assertions and their \
expected values intact.

Respond ONLY with a JSON object: {"fixed_tests": "<corrected full test code>"}.
No markdown, no backticks."""
    user = f"""## Test code that fails to run
```python
{failing_code}
```

## Error output from pytest
```
{error_output}
```

## Source code for reference (annotated with runtime values)
```python
{annotated_code}
```

Fix ONLY what prevents the tests from running. Return the full corrected code."""
    parsed, raw = llm_client.call_json(system, user)
    return parsed.get("fixed_tests", failing_code)
