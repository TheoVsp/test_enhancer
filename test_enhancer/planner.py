"""
Étape « plan de test » (nouvelle approche, demande du Pr. Chen).

Au lieu de demander directement au LLM « renforce les tests », on lui demande
D'ABORD de produire un *plan de test* : où sont les faiblesses de couverture,
et comment les combler, en raisonnant avec les concepts établis du test
logiciel (equivalence partitioning, boundary value analysis, branch coverage,
edge cases).

Ce module produit deux choses :
  1. Un plan structuré (liste d'objectifs de test) -> consommé par enhancer.py.
  2. Un fichier Markdown lisible qui retranscrit le RAISONNEMENT du LLM :
     où il voit une faiblesse, et pourquoi. (demande explicite du prof)

L'entrée correspond au « Input: test, vars, source code » du prof :
  - les tests existants
  - le tableau de variables + le code annoté (le « vars » + « source code »)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import llm_client


# Concepts de test logiciel injectés dans le prompt (« keywords from software
# testing literature » demandés par le prof). On les nomme explicitement pour
# forcer le LLM à raisonner avec, plutôt qu'au hasard.
TESTING_CONCEPTS = """\
- Equivalence partitioning: split the input space into classes that should \
behave the same, and make sure each class is covered by at least one test.
- Boundary value analysis: test the edges of each partition (zero, negative \
values, empty collections, very large values, None, etc.).
- Branch / path coverage: identify the branches in the code (if/else, loops, \
exception paths) and target the ones the existing tests do NOT exercise.
- Edge cases: unusual situations revealed by the runtime trace (a variable \
taking an unexpected value, a branch never visited, etc.)."""


SYSTEM_PROMPT = f"""You are a software testing expert. You are given a Python \
function, the runtime values its variables took during the EXISTING tests, and \
the existing tests themselves.

Your job is NOT to write tests yet. Your job is to produce a TEST PLAN that \
identifies where the existing test suite is WEAK and how to strengthen its \
COVERAGE. A test passing does not mean it is a good test: it may have weak \
assertions, cover only one branch, or be redundant.

Reason explicitly using these software-testing concepts:
{TESTING_CONCEPTS}

Use the runtime trace to your advantage: it shows which values and branches \
were ACTUALLY visited by the existing tests. Anything not visited is a \
candidate gap.

Respond ONLY with a JSON object of the form:
{{
  "reasoning": "<your step-by-step thinking: where the suite is weak and WHY, \
referencing the concepts above and the trace>",
  "test_plan": [
    {{
      "goal": "<what this test should verify>",
      "technique": "<one of: equivalence partitioning | boundary value \
analysis | branch coverage | edge case>",
      "rationale": "<why this matters / what gap it fills, based on the trace>",
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
                      existing_tests: str) -> str:
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

Produce the test plan now."""


def make_plan(annotated_code: str, variable_table: list[dict],
              existing_tests: str) -> TestPlan:
    """Demande au LLM un plan de test guidé par les concepts du test logiciel."""
    user_prompt = build_user_prompt(annotated_code, variable_table, existing_tests)
    parsed, raw = llm_client.call_json(SYSTEM_PROMPT, user_prompt)
    items = [TestPlanItem.from_dict(d) for d in parsed.get("test_plan", [])]
    return TestPlan(
        reasoning=parsed.get("reasoning", ""),
        items=items,
        raw_response=raw,
    )


def render_reasoning_markdown(plan: TestPlan, instance_id: str) -> str:
    """Produit le fichier Markdown qui retranscrit le raisonnement du LLM.

    C'est le « fichier qui retranscrit le schéma de pensée du LLM » demandé
    par le prof : où il voit une faiblesse, et pourquoi.
    """
    lines = [
        f"# Test plan reasoning — {instance_id}",
        "",
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
