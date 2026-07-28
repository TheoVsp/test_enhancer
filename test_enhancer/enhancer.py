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

TESTING PRINCIPLES THE SUITE MUST FOLLOW (not just "increase coverage"):

1. Boundary Value Analysis (BVA) — for every boundary a plan item targets, \
prefer writing the AT-boundary, JUST-BELOW-boundary, and JUST-ABOVE-boundary \
cases as separate assertions or separate tests, not a single arbitrary value.

2. Equivalence Partitioning — when a plan item targets an input class, pick \
an input that is clearly REPRESENTATIVE of that class (valid / invalid / \
edge-case-exceptional), not an arbitrary or unusually special value that \
happens to work.

3. MC/DC coverage — for decisions made of multiple conditions, write tests \
so each condition can be shown to independently affect the decision's \
outcome: vary one condition while holding the others fixed, across enough \
tests that every condition's effect is isolated at least once, and every \
decision takes both its True and False outcomes.

TRACE AND VERIFICATION DIRECTIVE — for EVERY test function you write, you \
must also produce a structured trace (see the "test_traces" field below) \
that: maps the test's input to the code path it drives, names the specific \
branch(es)/condition(s)/boundary it exercises, states the expected behavior, \
and justifies why the test is necessary (what it would catch that a weaker \
test would miss). Do not write a test you cannot produce a real trace for.

CRITICAL RULES TO AVOID WRONG EXPECTED VALUES (this is the main failure mode):
- ONLY import names that actually EXIST. Do NOT invent classes, functions, or symbols. Every import must come from the existing tests, the annotated source code, or the standard public API you are certain of. A single hallucinated import (e.g. `from x import NonExistentClass`) makes the WHOLE test file fail to collect, destroying all other tests. When unsure whether a name exists, do NOT import it — reuse only what the existing tests already import.
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
  "enhanced_tests": "<the full test code as a single string>",
  "test_traces": [
    {
      "test_name": "<must exactly match a def test_... name in enhanced_tests>",
      "input": "<the concrete input(s) this test uses>",
      "code_path": "<which lines/branches of the target function this input drives execution through>",
      "conditions_exercised": "<which decision(s)/condition(s) this test isolates and their True/False outcome; say 'n/a' if the test targets BVA/equivalence-partitioning rather than a compound decision>",
      "expected_behavior": "<what the code is expected to do/return for this input, and why that is the correct expectation (cite the trace or an existing assertion)>",
      "justification": "<why this test is necessary: what gap it closes, what class of fault it could catch that existing/weaker tests would miss>"
    }
  ],
  "compliance_check": "<markdown BODY TEXT ONLY (do not include the '## Theoretical Compliance Check' heading itself). For each test or tight group of tests, state which of Boundary Value Analysis, Equivalence Partitioning, and MC/DC it contributes to (a test may contribute to more than one), and note where it strengthens fault detection beyond simple line/branch coverage. End with a short paragraph explaining why this suite was designed to avoid a coverage plateau and target real fault-detection gaps rather than mechanically inflating line/branch counts.>"
}
No markdown, no backticks, just the JSON object."""


@dataclass
class TestTrace:
    """Trace d'exécution justificative pour UN test généré (directive
    'Trace and Verification'). Ce n'est PAS une trace runtime mesurée : c'est
    l'explication du LLM, à vérifier manuellement en cas de doute."""
    test_name: str
    input: str
    code_path: str
    conditions_exercised: str
    expected_behavior: str
    justification: str

    @classmethod
    def from_dict(cls, d: dict) -> "TestTrace":
        return cls(
            test_name=str(d.get("test_name", "")),
            input=str(d.get("input", "")),
            code_path=str(d.get("code_path", "")),
            conditions_exercised=str(d.get("conditions_exercised", "")),
            expected_behavior=str(d.get("expected_behavior", "")),
            justification=str(d.get("justification", "")),
        )

    def as_dict(self) -> dict:
        return {
            "test_name": self.test_name,
            "input": self.input,
            "code_path": self.code_path,
            "conditions_exercised": self.conditions_exercised,
            "expected_behavior": self.expected_behavior,
            "justification": self.justification,
        }


@dataclass
class EnhancementResult:
    analysis: str
    enhanced_tests: str
    raw_response: str
    traces: list[TestTrace] = None
    compliance_check: str = ""

    def __post_init__(self):
        if self.traces is None:
            self.traces = []


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
    traces = [TestTrace.from_dict(d) for d in parsed.get("test_traces", [])]
    return EnhancementResult(
        analysis=parsed.get("analysis", ""),
        enhanced_tests=parsed.get("enhanced_tests", ""),
        raw_response=raw,
        traces=traces,
        compliance_check=parsed.get("compliance_check", ""),
    )


def render_compliance_markdown(
    traces: list[TestTrace],
    compliance_sections: list[tuple[int, str]],
    instance_id: str,
) -> str:
    """Produit le Markdown final : traces d'exécution par test + section
    '## Theoretical Compliance Check' (agrégée sur toutes les itérations de
    la boucle d'enrichissement, dans l'ordre de génération)."""
    lines = [f"# Execution traces & theoretical compliance — {instance_id}", ""]

    lines.append("## Execution traces")
    lines.append("")
    if not traces:
        lines.append("_(no traces returned by the LLM)_")
    for i, t in enumerate(traces, 1):
        lines.append(f"### {i}. `{t.test_name}`")
        lines.append(f"- **Input:** {t.input}")
        lines.append(f"- **Code path:** {t.code_path}")
        lines.append(f"- **Conditions exercised:** {t.conditions_exercised}")
        lines.append(f"- **Expected behavior:** {t.expected_behavior}")
        lines.append(f"- **Why this test is necessary:** {t.justification}")
        lines.append("")

    lines.append("## Theoretical Compliance Check")
    lines.append("")
    if not compliance_sections:
        lines.append("_(no compliance check returned by the LLM)_")
    for it, text in compliance_sections:
        if len(compliance_sections) > 1:
            lines.append(f"### Iteration {it}")
            lines.append("")
        lines.append(text.strip() or "_(empty)_")
        lines.append("")

    return "\n".join(lines)


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