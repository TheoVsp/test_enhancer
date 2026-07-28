"""
Étape « plan de test » (nouvelle approche, demande du Pr. Chen).

Au lieu de demander directement au LLM « renforce les tests », on lui demande
D'ABORD de produire un *plan de test* : où sont les faiblesses de couverture,
et comment les combler, en raisonnant avec les concepts établis du test
logiciel (equivalence partitioning, boundary value analysis, branch coverage,
edge cases).

Nouveauté (phase "measured coverage") : le planner peut recevoir en plus une
WEAKNESS MAP MESURÉE par coverage.py (lignes/branches jamais exécutées par la
suite existante). Cette mesure est traitée comme la VÉRITÉ TERRAIN sur la
couverture : le LLM doit prioritairement combler ces trous-là, et n'a pas le
droit d'inventer des trous de couverture contredits par la mesure.

Ce module produit deux choses :
  1. Un plan structuré (liste d'objectifs de test) -> consommé par enhancer.py.
  2. Un fichier Markdown lisible qui retranscrit le RAISONNEMENT du LLM :
     où il voit une faiblesse, et pourquoi. (demande explicite du prof)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import llm_client


# Concepts de test logiciel injectés dans le prompt (« keywords from software
# testing literature » demandés par le prof).
TESTING_CONCEPTS = """\
- Boundary value analysis (BVA): for every important boundary in the code \
(a comparison against 0, a length check, an off-by-one-prone loop bound, a \
None/empty check, a numeric limit), plan tests AT the boundary, just BELOW \
it, and just ABOVE it. A boundary is not properly covered by a single value.
- Equivalence partitioning: split the input space into classes that should \
behave the same, and plan representative tests from the VALID classes, the \
INVALID classes, and any edge-case/exceptional classes (wrong type, empty \
collection, None where an object is expected, etc.).
- MC/DC (Modified Condition/Decision Coverage): for every decision made of \
multiple conditions (e.g. `if a and b`, `while x or y`), plan tests that let \
EACH condition be shown, independently of the others, to affect the \
decision's outcome — not just tests that make the whole decision True once \
and False once. State explicitly, in the rationale, which condition is being \
isolated and what the other condition(s) are held at while doing so.
- Edge cases: unusual situations revealed by the runtime trace (a variable \
taking an unexpected value, a branch never visited, etc.)."""


SYSTEM_PROMPT = f"""You are a software testing expert. You are given a Python \
function, the runtime values its variables took during the EXISTING tests, and \
the existing tests themselves. You may ALSO be given a MEASURED coverage \
report (produced by coverage.py in branch mode).

Your job is NOT to write tests yet. Your job is to produce a TEST PLAN that \
identifies where the existing test suite is WEAK and how to strengthen its \
COVERAGE. A test passing does not mean it is a good test: it may have weak \
assertions, cover only one branch, or be redundant.

Reason explicitly using these software-testing concepts:
{TESTING_CONCEPTS}

RULES ABOUT THE MEASURED COVERAGE REPORT (when present):
1. The measured report is GROUND TRUTH. Every line or branch outcome it lists \
as never executed is a REAL gap: your plan MUST contain at least one test \
item targeting each of them, and each such item must cite the exact line \
numbers it targets in its rationale.
2. Do NOT claim a coverage gap that the measurement contradicts: if a line or \
branch is not listed as missing, it is already executed by the existing \
suite. Proposing a test for it is only justified on OTHER grounds (weak \
assertions, equivalence classes, boundary values) — never as a coverage gap.
3. You may and should go BEYOND the measured gaps with equivalence \
partitioning and boundary value analysis, since 100% branch coverage does \
not mean the assertions are strong.
When a "Feedback from previous iterations" section is present, this is a \
LATER iteration of an enhancement loop: only propose NEW items for the \
remaining measured gaps, never duplicate a goal already generated, and when \
a previous strategy failed to reach its target branch, analyse WHY (using \
the code) and propose a structurally different strategy.

Use the runtime trace to your advantage: it shows which VALUES the variables \
actually took. The measured report tells you WHICH code was never run; the \
trace tells you HOW the code that did run behaved.

Respond ONLY with a JSON object of the form:
{{
  "reasoning": "<your step-by-step thinking: where the suite is weak and WHY, \
referencing the concepts above, the measured gaps, and the trace>",
  "test_plan": [
    {{
      "goal": "<what this test should verify>",
      "technique": "<one of: boundary value analysis | equivalence \
partitioning | MC/DC | edge case>",
      "rationale": "<why this matters / what gap it fills; cite measured \
line numbers when targeting a measured gap. If technique is 'MC/DC', name \
the specific condition being isolated and how the other condition(s) in the \
same decision are held fixed.>",
      "inputs": "<concrete inputs to use>"
    }}
  ]
}}
No markdown, no backticks, just the JSON object."""


@dataclass
class TestPlanItem:
    goal: str
    technique: str
    rationale: str
    inputs: str

    @classmethod
    def from_dict(cls, d: dict) -> "TestPlanItem":
        return cls(
            goal=str(d.get("goal", "")),
            technique=str(d.get("technique", "")),
            rationale=str(d.get("rationale", "")),
            inputs=str(d.get("inputs", "")),
        )

    def as_dict(self) -> dict:
        return {"goal": self.goal, "technique": self.technique,
                "rationale": self.rationale, "inputs": self.inputs}


@dataclass
class TestPlan:
    reasoning: str
    items: list[TestPlanItem] = field(default_factory=list)
    raw_response: str = ""
    coverage_summary: str = ""          # la weakness map mesurée (si fournie)

    def as_dict(self) -> dict:
        return {"reasoning": self.reasoning,
                "test_plan": [it.as_dict() for it in self.items]}


def _truncate_table(table: list[dict], max_rows: int = 300) -> str:
    head = table[:max_rows]
    lines = ["step | function | lineno | event | variable | value"]
    for r in head:
        lines.append(
            f"{r['step']} | {r['function']} | {r['lineno']} | "
            f"{r['event']} | {r['variable']} | {r['value']}"
        )
    if len(table) > max_rows:
        lines.append(f"... ({len(table) - max_rows} more rows omitted)")
    return "\n".join(lines)


def build_user_prompt(annotated_code: str, variable_table: list[dict],
                      existing_tests: str,
                      coverage_summary: str | None = None,
                      feedback: str | None = None) -> str:
    coverage_section = ""
    if coverage_summary:
        coverage_section = f"""## MEASURED coverage gaps (ground truth, from coverage.py --branch)
{coverage_summary}

"""
    feedback_section = ""
    if feedback:
        feedback_section = f"""## Feedback from previous iterations
{feedback}

"""
    return f"""{coverage_section}{feedback_section}## Annotated source code (runtime values inline)
```python
{annotated_code}
```

## Variable evolution table (excerpt)
{_truncate_table(variable_table)}

## Existing tests
```python
{existing_tests}
```

Produce the test plan now."""


def make_plan(annotated_code: str, variable_table: list[dict],
              existing_tests: str,
              coverage_summary: str | None = None,
              feedback: str | None = None) -> TestPlan:
    """Demande au LLM un plan de test guidé par les concepts du test logiciel,
    la weakness map mesurée, et le feedback des itérations précédentes."""
    user_prompt = build_user_prompt(annotated_code, variable_table,
                                    existing_tests, coverage_summary, feedback)
    parsed, raw = llm_client.call_json(SYSTEM_PROMPT, user_prompt)
    items = [TestPlanItem.from_dict(d) for d in parsed.get("test_plan", [])]
    return TestPlan(
        reasoning=parsed.get("reasoning", ""),
        items=items,
        raw_response=raw,
        coverage_summary=coverage_summary or "",
    )


def render_reasoning_markdown(plan: TestPlan, instance_id: str) -> str:
    """Produit le fichier Markdown qui retranscrit le raisonnement du LLM."""
    lines = [
        f"# Test plan reasoning — {instance_id}",
        "",
    ]
    if plan.coverage_summary:
        lines += [
            "## Measured coverage gaps (input to the LLM)",
            "",
            "```",
            plan.coverage_summary,
            "```",
            "",
        ]
    lines += [
        "## Where the LLM sees weaknesses (and why)",
        "",
        plan.reasoning.strip() or "_(no reasoning returned)_",
        "",
        "## Planned tests",
        "",
    ]
    if not plan.items:
        lines.append("_(no test plan items returned)_")
    for i, it in enumerate(plan.items, 1):
        lines.append(f"### {i}. {it.goal}")
        lines.append(f"- **Technique:** {it.technique}")
        lines.append(f"- **Why this matters:** {it.rationale}")
        lines.append(f"- **Inputs:** `{it.inputs}`")
        lines.append("")
    return "\n".join(lines)