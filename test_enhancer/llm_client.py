"""
Client LLM partagé.

Centralise la configuration de l'appel au LLM pour que `planner.py`,
`enhancer.py` et la boucle de réparation utilisent tous le même client.
On utilise le SDK `openai` pointé vers les endpoints OpenAI-compatibles de
Google Gemini (choix actuel car la clé OpenAI fournie ne fonctionnait pas ;
changer `base_url` + la clé suffit pour revenir à OpenAI).
"""
from __future__ import annotations

import json

from .config import GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _get_client():
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY non défini. Configure ta clé avant de lancer : "
            "set GEMINI_API_KEY=AIza..."
        )
    from openai import OpenAI
    return OpenAI(api_key=GEMINI_API_KEY, base_url=GEMINI_BASE_URL)


def call_json(system_prompt: str, user_prompt: str) -> tuple[dict, str]:
    """Appelle le LLM en exigeant une réponse JSON.

    Returns:
        (parsed_dict, raw_text). Si le JSON est invalide, parsed_dict = {}.
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return {}, raw
