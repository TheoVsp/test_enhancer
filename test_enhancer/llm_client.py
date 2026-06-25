"""
Client LLM partagé.

Centralise la configuration de l'appel au LLM pour que `planner.py`,
`enhancer.py` et la boucle de réparation utilisent tous le même client.
On utilise le SDK `openai` pointé vers des endpoints OpenAI-compatibles.

Le fournisseur (URL) et le modèle sont configurables sans toucher au code :
  - TE_LLM_BASE_URL : l'URL de l'API (défaut : Gemini)
  - TE_LLM_MODEL    : le modèle (via config.py)
  - GEMINI_API_KEY  : la clé d'API

Inclut un RETRY automatique sur les erreurs temporaires (503 surcharge,
429 rate limit, erreurs réseau), indispensable pour les runs à grande échelle.
"""
from __future__ import annotations

import json
import os
import time

from .config import GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE

# URL configurable via variable d'environnement (défaut : Gemini).
# Pour MiniMax : set TE_LLM_BASE_URL=https://api.minimaxi.com/v1
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
LLM_BASE_URL = os.environ.get("TE_LLM_BASE_URL", DEFAULT_BASE_URL)

# Paramètres de retry
MAX_RETRIES = 4           # nombre de tentatives sur erreur temporaire
RETRY_BASE_DELAY = 3.0    # délai de base en secondes (backoff exponentiel)


def _get_client():
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY non défini. Configure ta clé avant de lancer : "
            "set GEMINI_API_KEY=..."
        )
    from openai import OpenAI
    return OpenAI(api_key=GEMINI_API_KEY, base_url=LLM_BASE_URL)


def _is_retryable(exc: Exception) -> bool:
    """Vrai si l'erreur est temporaire et mérite une nouvelle tentative.

    On réessaie sur : surcharge serveur (503), rate limit (429), et erreurs
    réseau/timeout. On NE réessaie PAS sur les erreurs définitives comme
    l'authentification (401) ou une requête invalide (400).
    """
    # Import local pour ne pas imposer la dépendance au moment de l'import
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

    # Filet de sécurité : certains codes arrivent sous APIStatusError générique
    status = getattr(exc, "status_code", None)
    if status in (429, 500, 502, 503, 504):
        return True
    return False


def call_json(system_prompt: str, user_prompt: str) -> tuple[dict, str]:
    """Appelle le LLM en exigeant une réponse JSON, avec retry automatique.

    Returns:
        (parsed_dict, raw_text). Si le JSON est invalide, parsed_dict = {}.

    Raises:
        La dernière exception si toutes les tentatives échouent, ou
        immédiatement si l'erreur n'est pas temporaire (ex. 401).
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
            try:
                return json.loads(raw), raw
            except json.JSONDecodeError:
                return {}, raw

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable(exc) or attempt == MAX_RETRIES:
                # Erreur définitive, ou plus de tentatives -> on propage
                raise
            # Backoff exponentiel : 3s, 6s, 12s...
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(
                f"    [LLM] erreur temporaire ({type(exc).__name__}), "
                f"nouvelle tentative {attempt}/{MAX_RETRIES - 1} dans {delay:.0f}s...",
                flush=True,
            )
            time.sleep(delay)

    # On ne devrait jamais arriver ici, mais par sécurité :
    if last_exc:
        raise last_exc
    return {}, "{}"
