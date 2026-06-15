"""
Configuration centrale du pipeline Test Enhancer.

Toutes les constantes ajustables sont ici pour éviter de les éparpiller
dans le code. Modifie ce fichier plutôt que de toucher à la logique.
"""
from __future__ import annotations

import os

from pathlib import Path

# --- Chemins ---------------------------------------------------------------
# Racine du projet (dossier qui contient ce package)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Dossier où l'on stocke tous les artefacts produits (traces, tableaux, etc.)
WORK_DIR = PROJECT_ROOT / "runs"
WORK_DIR.mkdir(exist_ok=True)

# --- Dataset ---------------------------------------------------------------
# import the local dataset
DATASET_DIR = Path("test_enhancer/dataset")
#DATASET_SPLIT = "test"

# --- LLM -------------------------------------------------------------------
# On lit la clé API depuis l'environnement. NE JAMAIS hardcoder une clé ici.
# On lit la clé OPENAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# On change le modèle par défaut
LLM_MODEL = os.environ.get("TE_LLM_MODEL", "MiniMax-M2.7")
LLM_TEMPERATURE = 0.0  # déterministe pour la reproductibilité

# --- Tracing ---------------------------------------------------------------
# Nombre maximum de lignes de trace qu'on garde par exécution de test
# (évite d'exploser le contexte du LLM sur des boucles énormes).
MAX_TRACE_ROWS = 2000
# Longueur maximale de la représentation d'une valeur de variable.
MAX_VALUE_REPR_LEN = 200
