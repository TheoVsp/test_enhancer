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

Rules:
- Build on the existing tests; do NOT remove existing assertions.
- Use only the function's public interface unless the existing tests do otherwise.
- Make assertions STRONG: assert exact values/structure, not just "is not None".
- Keep the tests runnable and self-contained (include needed imports).
- Ground expected values in the runtime trace when available.

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


def enhance_tests(annotated_code: str, variable_table: list[dict],
                  existing_tests: str, plan: TestPlan) -> EnhancementResult:
    """Génère les tests renforcés à partir du plan de test."""
    user_prompt = build_user_prompt(annotated_code, variable_table,
                                     existing_tests, plan)
    parsed, raw = llm_client.call_json(SYSTEM_PROMPT, user_prompt)
    return EnhancementResult(
        analysis=parsed.get("analysis", ""),
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
