"""
Client LLM partagé.

Centralise la configuration de l'appel au LLM pour que `planner.py`,
`enhancer.py` et la boucle de réparation utilisent tous le même client.
On utilise le SDK `openai` pointé vers des endpoints OpenAI-compatibles.

Le fournisseur (URL) et le modèle sont configurables sans toucher au code :
  - TE_LLM_BASE_URL : l'URL de l'API (défaut : Gemini)
  - TE_LLM_MODEL    : le modèle (via config.py)
  - GEMINI_API_KEY  : la clé d'API

Inclut :
  - un RETRY automatique sur les erreurs temporaires (503/429/réseau)
  - un parsing JSON TOLÉRANT (certains modèles comme MiniMax ne respectent
    pas la consigne "réponds uniquement en JSON" et entourent le JSON de
    texte ou de Markdown -> on extrait quand même l'objet JSON).
"""
from __future__ import annotations

import json
import os
import re
import time

from .config import GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
LLM_BASE_URL = os.environ.get("TE_LLM_BASE_URL", DEFAULT_BASE_URL)

MAX_RETRIES = 4
RETRY_BASE_DELAY = 3.0


def _get_client():
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY non défini. Configure ta clé avant de lancer."
        )
    from openai import OpenAI
    return OpenAI(api_key=GEMINI_API_KEY, base_url=LLM_BASE_URL)


def extract_json(raw: str) -> dict:
    """Extrait un objet JSON d'une réponse LLM, même entourée de texte/Markdown.

    Gère les modèles qui ne respectent pas 'réponds uniquement en JSON' :
      - JSON pur
      - blocs ```json ... ``` ou ``` ... ```
      - texte avant/après le JSON
    Renvoie {} si aucun JSON exploitable n'est trouvé.
    """
    if not raw or not raw.strip():
        return {}
    # 0. Retirer les blocs de raisonnement <think>...</think> émis par les
    #    modèles de raisonnement (ex. MiniMax-M2.x). Ces blocs contiennent
    #    souvent des accolades qui trompent l'extraction du JSON.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Filet : si une balise <think> ouvrante traîne sans fermeture, on coupe
    raw = re.sub(r"<think>.*$", "", raw, flags=re.DOTALL)
    if not raw.strip():
        return {}
    # 1. Parse direct (cas idéal)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 2. Bloc Markdown ```json ... ``` ou ``` ... ```
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # 3. Du premier { au dernier } (gère le texte avant/après)
    first = raw.find("{")
    last = raw.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(raw[first:last + 1])
        except json.JSONDecodeError:
            pass
    # 4. Échec
    return {}


def _is_retryable(exc: Exception) -> bool:
    """Vrai si l'erreur est temporaire (503/429/réseau). Pas de retry sur 401/400."""
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except Exception:  # noqa: BLE001
        return False

    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError,
                        InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    if status in (429, 500, 502, 503, 504):
        return True
    return False


def call_json(system_prompt: str, user_prompt: str) -> tuple[dict, str]:
    """Appelle le LLM en demandant du JSON, avec retry ET parsing tolérant.

    Returns:
        (parsed_dict, raw_text). parsed_dict = {} si aucun JSON exploitable.
    """
    client = _get_client()
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
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
            # Parsing TOLÉRANT (gère MiniMax & co qui ajoutent du texte/Markdown)
            return extract_json(raw), raw

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable(exc) or attempt == MAX_RETRIES:
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(
                f"    [LLM] erreur temporaire ({type(exc).__name__}), "
                f"nouvelle tentative {attempt}/{MAX_RETRIES - 1} dans {delay:.0f}s...",
                flush=True,
            )
            time.sleep(delay)

    if last_exc:
        raise last_exc
    return {}, "{}"
