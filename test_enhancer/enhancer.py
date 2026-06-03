"""
Étape d'analyse et de renforcement des tests par le LLM.

Sur le whiteboard : "ask LLM to analyze -> enhance test cases".

On donne au LLM :
  - le code source annoté (avec les valeurs de variables inline)
  - le tableau d'évolution des variables (en extrait)
  - les tests existants
On lui demande de produire des tests RENFORCÉS : assertions supplémentaires,
cas limites observés dans la trace mais non couverts, vérifications de valeurs
intermédiaires, etc.

IMPORTANT (V1) : l'objectif est d'AMÉLIORER les tests existants, pas d'en
générer de nouveaux from scratch. Le prompt insiste donc sur l'enrichissement
des assertions et la couverture des comportements observés.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import LLM_MODEL, LLM_TEMPERATURE, GEMINI_API_KEY


SYSTEM_PROMPT = """You are a software testing expert. Your task is to STRENGTHEN \
an existing test suite for a Python function, using runtime evidence.

You are given:
1. The source code, annotated inline with observed runtime variable values \
(marked with `# [TE] ...`).
2. A table summarizing how variables evolved during a passing test execution.
3. The existing test code.

Your goal is to IMPROVE the existing tests (not write brand-new unrelated \
tests). Concretely, you should:
- Add assertions that check intermediate or final variable values that the \
runtime trace revealed but the current tests do not verify.
- Cover boundary values and alternative branches visible in the trace.
- Make weak assertions stronger (e.g. assert exact values/structure instead \
of just "not None").

Constraints:
- Only use the function's public interface; do not assert on private internals \
unless the existing tests already do.
- Keep the tests runnable and self-contained.
- Do NOT remove existing assertions; only add or strengthen.

Respond ONLY with a JSON object of the form:
{
  "analysis": "<short explanation of the gaps you found>",
  "enhanced_tests": "<the full enhanced test code as a string>"
}
No markdown, no backticks, just the JSON object."""


@dataclass
class EnhancementResult:
    analysis: str
    enhanced_tests: str
    raw_response: str


def _truncate_table(table: list[dict], max_rows: int = 300) -> str:
    """Sérialise un extrait du tableau pour tenir dans le contexte."""
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


def build_user_prompt(
    annotated_code: str,
    variable_table: list[dict],
    existing_tests: str,
) -> str:
    return f"""## Annotated source code (with runtime values inline)
```python
{annotated_code}
```

## Variable evolution table (excerpt)
{_truncate_table(variable_table)}

## Existing tests
```python
{existing_tests}
```

Now strengthen the existing tests using the runtime evidence above."""


def enhance_tests(
    annotated_code: str,
    variable_table: list[dict],
    existing_tests: str,
) -> EnhancementResult:
    """Appelle le LLM pour produire des tests renforcés.

    Nécessite la variable d'environnement GEMINI_API_KEY.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY non défini. Configure ta clé avant de lancer : "
            "set GEMINI_API_KEY=AIza..."
        )

    # Import local pour ne pas exiger openai si on ne fait que tracer.
    from openai import OpenAI

    # L'astuce est ici : on utilise le client OpenAI, mais on l'envoie chez Google !
    client = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    
    user_prompt = build_user_prompt(annotated_code, variable_table, existing_tests)

    response = client.chat.completions.create(
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # fallback : on renvoie le texte brut dans analysis pour debug
        return EnhancementResult(analysis="<JSON parse failed>", enhanced_tests="", raw_response=raw)

    return EnhancementResult(
        analysis=parsed.get("analysis", ""),
        enhanced_tests=parsed.get("enhanced_tests", ""),
        raw_response=raw,
    )