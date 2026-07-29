"""
Étape « plan de test » (nouvelle approche, demande du Pr. Chen).

Au lieu de demander directement au LLM « renforce les tests », on lui demande
D'ABORD de produire un *plan de test* : où sont les faiblesses de couverture,
et comment les combler, en raisonnant avec les concepts établis du test
logiciel (equivalence partitioning, boundary value analysis, branch coverage,
MC/DC, edge cases).

Le planner reçoit une WEAKNESS MAP MESURÉE par coverage.py (lignes/branches
jamais exécutées par la suite existante), traitée comme VÉRITÉ TERRAIN.

Conformité à la théorie du test (demande Slack du prof) :
  - MC/DC fait partie des techniques exigées ;
  - chaque item de plan porte une JUSTIFICATION PAR LA TRACE, étape par étape
    (input -> chemin de code -> branches/conditions/frontières exercées ->
    comportement attendu -> pourquoi ce test est nécessaire) ;
  - chaque item déclare les BRANCHES REVENDIQUÉES au format mesurable
    [ligne_source, ligne_dest] — vérifiables ensuite contre l'exécution
    réelle (claim-vs-trace, phase suivante) ;
  - le plan inclut un THEORETICAL COMPLIANCE CHECK, rendu dans le Markdown
    sous le titre exact `## Theoretical Compliance Check`.

Ce module produit deux choses :
  1. Un plan structuré (liste d'objectifs de test) -> consommé par enhancer.py.
  2. Un fichier Markdown lisible qui retranscrit le RAISONNEMENT du LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import llm_client


# Concepts de test logiciel injectés dans le prompt (« keywords from software
# testing literature » demandés par le prof).
TESTING_CONCEPTS = """\
- Equivalence partitioning: split the input space into classes that should \
behave the same, and include representative tests from valid, invalid, and \
edge-case/exceptional input classes.
- Boundary value analysis (BVA): test important boundaries — values exactly \
at the boundary, just below it, and just above it (zero, negative values, \
empty collections, very large values, None, etc.).
- Branch / path coverage: identify the branches in the code (if/else, loops, \
exception paths) and target the ones the existing tests do NOT exercise.
- MC/DC (Modified Condition/Decision Coverage): for conditional logic, \
ensure each decision takes both true and false outcomes where possible, and \
that each important condition can be shown to INDEPENDENTLY affect the \
decision outcome.
- Edge cases: unusual situations revealed by the runtime trace (a variable \
taking an unexpected value, a branch never visited, etc.)."""


SYSTEM_PROMPT = f"""You are a software testing expert. You are given a Python \
function, the runtime values its variables took during the EXISTING tests, and \
the existing tests themselves. You may ALSO be given a MEASURED coverage \
report (produced by coverage.py in branch mode).

Your job is NOT to write tests yet. Your job is to produce a TEST PLAN that \
identifies where the existing test suite is WEAK and how to strengthen it. A \
test passing does not mean it is a good test: it may have weak assertions, \
cover only one branch, or be redundant.

Reason explicitly using these software-testing principles:
{TESTING_CONCEPTS}

RULES ABOUT THE MEASURED COVERAGE REPORT (when present):
1. The measured report is GROUND TRUTH. Every line or branch outcome it lists \
as never executed is a REAL gap: your plan MUST contain at least one test \
item targeting each of them, and each such item must cite the exact line \
numbers it targets in its rationale.
2. Do NOT claim a coverage gap that the measurement contradicts: if a line or \
branch is not listed as missing, it is already executed by the existing \
suite. Proposing a test for it is only justified on OTHER grounds (weak \
assertions, equivalence classes, boundary values, MC/DC) — never as a \
coverage gap.
3. You may and should go BEYOND the measured gaps with equivalence \
partitioning, boundary value analysis and MC/DC, since 100% branch coverage \
does not mean the assertions are strong.

TRACE AND VERIFICATION DIRECTIVE (mandatory for EVERY plan item):
For each planned test, provide a step-by-step execution trace justification \
that explains how the test will interact with the target code. It must:
- map the concrete test input to the relevant code path (cite functions and \
line numbers);
- identify the branches, conditions, or boundary cases exercised;
- explain the expected behavior at each step, grounded in the runtime trace \
or the existing tests (never guessed);
- justify why this test is necessary (what fault it could expose that the \
existing suite cannot).
Also list the branch outcomes the test CLAIMS to exercise, as measurable \
[source_line, dest_line] pairs matching the measured report's format. These \
claims will be VERIFIED against actual execution: only claim what the test \
genuinely exercises.

When a "Feedback from previous iterations" section is present, this is a \
LATER iteration of an enhancement loop: only propose NEW items for the \
remaining measured gaps, never duplicate a goal already generated, and when \
a previous strategy failed to reach its target branch, analyse WHY (using \
the code) and propose a structurally different strategy.

THEORETICAL COMPLIANCE CHECK (mandatory):
After the plan, provide a compliance check that explains how the planned \
tests satisfy the required testing principles. For each test or group of \
tests, specify whether it contributes to: Boundary Value Analysis, \
Equivalence Partitioning, MC/DC coverage, or stronger fault detection beyond \
simple line/branch coverage. Explain why the tests are not merely increasing \
coverage mechanically, but are designed to avoid coverage plateaus and \
expose faults that weak or redundant tests may miss.

Respond ONLY with a JSON object of the form:
{{
  "reasoning": "<your step-by-step thinking: where the suite is weak and WHY, \
referencing the principles above, the measured gaps, and the trace>",
  "test_plan": [
    {{
      "goal": "<what this test should verify>",
      "technique": "<one of: equivalence partitioning | boundary value \
analysis | branch coverage | MC/DC | edge case>",
      "rationale": "<why this matters / what gap it fills; cite measured \
line numbers when targeting a measured gap>",
      "inputs": "<concrete inputs to use>",
      "trace_justification": "<step-by-step: input -> code path (functions, \
line numbers) -> branches/conditions/boundaries exercised -> expected \
behavior -> why this test is necessary>",
      "claimed_branches": [[source_line, dest_line], ...]
    }}
  ],
  "theoretical_compliance_check": "<the compliance check described above>"
}}
No markdown, no backticks, just the JSON object."""


@dataclass
class TestPlanItem:
    goal: str
    technique: str
    rationale: str
    inputs: str
    trace_justification: str = ""
    claimed_branches: list[list[int]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "TestPlanItem":
        branches = []
        for b in d.get("claimed_branches", []) or []:
            try:
                branches.append([int(b[0]), int(b[1])])
            except (TypeError, ValueError, IndexError):
                continue
        return cls(
            goal=str(d.get("goal", "")),
            technique=str(d.get("technique", "")),
            rationale=str(d.get("rationale", "")),
            inputs=str(d.get("inputs", "")),
            trace_justification=str(d.get("trace_justification", "")),
            claimed_branches=branches,
        )

    def as_dict(self) -> dict:
        return {"goal": self.goal, "technique": self.technique,
                "rationale": self.rationale, "inputs": self.inputs,
                "trace_justification": self.trace_justification,
                "claimed_branches": self.claimed_branches}


@dataclass
class TestPlan:
    reasoning: str
    items: list[TestPlanItem] = field(default_factory=list)
    raw_response: str = ""
    coverage_summary: str = ""          # la weakness map mesurée (si fournie)
    compliance_check: str = ""          # Theoretical Compliance Check

    def as_dict(self) -> dict:
        return {"reasoning": self.reasoning,
                "test_plan": [it.as_dict() for it in self.items],
                "theoretical_compliance_check": self.compliance_check}


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
        compliance_check=parsed.get("theoretical_compliance_check", ""),
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
        if it.claimed_branches:
            claimed = ", ".join(f"{a}->{b}" for a, b in it.claimed_branches)
            lines.append(f"- **Claimed branch outcomes:** {claimed}")
        if it.trace_justification:
            lines.append(f"- **Execution-trace justification:** "
                         f"{it.trace_justification}")
        lines.append("")
    lines += [
        "## Theoretical Compliance Check",
        "",
        plan.compliance_check.strip()
        or "_(no compliance check returned)_",
        "",
    ]
    return "\n".join(lines)