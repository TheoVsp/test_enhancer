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
# Nom HuggingFace du dataset. SWE-bench Lite = 300 instances Python.
DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_SPLIT = "test"

# Chemin OPTIONNEL vers une soumission SWE-bench locale (dossier de résultats
# d'un agent : trajs/, logs/, results/). Si défini, on peut utiliser le patch
# généré par l'agent au lieu du gold patch. Laisser vide pour ne pas l'utiliser.
# Exemple : r"C:\MITACS\Workspace\datasets\20260217_mini-v2.0.0_minimax-2-5-high"
LOCAL_SUBMISSION_DIR = os.environ.get("TE_LOCAL_SUBMISSION_DIR", "")

# --- LLM -------------------------------------------------------------------
# On lit la clé API depuis l'environnement. NE JAMAIS hardcoder une clé ici.
# On lit la clé Gemini au lieu de OpenAI
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# On change le modèle par défaut
LLM_MODEL = os.environ.get("TE_LLM_MODEL", "gemini-2.5-flash")
LLM_TEMPERATURE = 0.0  # déterministe pour la reproductibilité

# --- Tracing ---------------------------------------------------------------
# Nombre maximum de lignes de trace qu'on garde par exécution de test
# (évite d'exploser le contexte du LLM sur des boucles énormes).
MAX_TRACE_ROWS = 2000
# Longueur maximale de la représentation d'une valeur de variable.
MAX_VALUE_REPR_LEN = 200
