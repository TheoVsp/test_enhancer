# Test Enhancer Pipeline - V1 (Local Execution) (A mettre à jour)

Ce projet implémente un pipeline d'analyse dynamique et de renforcement de tests pour l'évaluation de LLMs sur le dataset **SWE-bench Lite**. 

Plutôt que d'utiliser une analyse statique ou d'injecter manuellement des `print()`, ce pipeline utilise le tracing natif de Python (`sys.settrace`) couplé à des hooks Pytest pour capturer l'état réel de la mémoire à l'exécution. Ces données d'exécution sont ensuite fournies à un agent LLM pour générer des assertions de test plus robustes.

## 🚀 Fonctionnalités clés de la V1
* **Clonage et patching automatisés** : Récupère une instance SWE-bench, applique le *gold patch* et les tests originaux.
* **Tracing chirurgical** : Capture l'évolution des variables locales ligne par ligne pendant l'exécution des tests.
* **Génération d'artefacts d'analyse** :
  * Un tableau d'exécution global (`.csv` / `.xlsx`).
  * Du code source annoté *inline* (`# [TE] var=valeur`).
* **Sélection dynamique** : Isole automatiquement le fichier source modifié par le développeur pour l'injecter dans le prompt.
* **Renforcement par LLM** : Utilise l'API Gemini (via le SDK OpenAI) pour analyser les artefacts et produire de nouvelles assertions.

---

## 🛠️ Prérequis et Installation

1. **Environnement virtuel** : Il est recommandé d'utiliser un environnement Conda (ex: `mitacs-env`).
2. **Dépendances** : 
   ```bash
   pip install datasets pytest openai openpyxl
   ```
3. **Clé API** : Le pipeline utilise le modèle `gemini-2.5-flash` via le client OpenAI.
   ```cmd
   # Sous Windows (cmd)
   set GEMINI_API_KEY=votre_cle_api_ici
   ```

---

## 💻 Utilisation (CLI)

Le point d'entrée principal est `main.py`.

* **Lister les instances disponibles** :
  ```bash
  python -m test_enhancer.main --list 5
  ```
* **Lancer une démo locale (sans SWE-bench, pour tester le tracer)** :
  ```bash
  python -m test_enhancer.main --demo
  ```
* **Lancer le pipeline complet sur une instance (Ex: SymPy)** :
  ```bash
  python -m test_enhancer.main --instance sympy__sympy-24909
  ```
* **Lancer l'extraction sans appeler le LLM (mode debug/économique)** :
  ```bash
  python -m test_enhancer.main --instance sympy__sympy-24909 --no-enhance
  ```

---

## 🏗️ Architecture du Pipeline

L'exécution suit le cycle de vie suivant :

1. **Chargement (`dataset.py`)** : Récupération des métadonnées de l'instance depuis HuggingFace (commits, patches, node ids).
2. **Préparation (`swe_runner.py`)** : Clone du dépôt, checkout du `base_commit`, application des patchs, et installation en mode éditable (avec forçage UTF-8 pour Windows).
3. **Exécution sous Tracer (`swe_runner.py` & `tracer.py`)** : 
   * Résolution des *node ids* incomplets de SWE-bench vers un format lisible par Pytest.
   * Injection d'un micro-plugin Pytest (`pytest_runtest_call`) pour n'activer `sys.settrace` **que** pendant l'exécution du test métier, éliminant ainsi le bruit d'initialisation de la librairie.
4. **Création des artefacts (`artifacts.py`)** : Consolidation des traces brutes en CSV et génération des fichiers Python annotés.
5. **Filtrage contextuel (`pipeline.py`)** : Analyse du diff original pour garantir que le LLM recevra le code du fichier précisément ciblé par la correction.
6. **Inférence LLM (`enhancer.py`)** : Construction du prompt systémique et génération d'un objet JSON contenant l'analyse et le code de test renforcé.
7. **Évaluation (`evaluate.py`)** : Comparaison statique des tests originaux vs renforcés (nombre d'assertions, de fonctions de test).
8. **Validation (`validate.py`)** : Réexécution des tests renforcés dans le repo patché pour vérifier qu'ils sont syntaxiquement valides, collectables, et qu'ils PASSENT. Un test renforcé qui échoue (LLM ayant halluciné une valeur) est rejeté.

---

## 🗂️ Utiliser un dataset / une soumission d'agent en local

Le pipeline peut fonctionner de deux manières :

**Mode par défaut (gold patch)** : on applique le patch du développeur (gold patch) issu de HuggingFace. C'est ce qui se passe si aucune soumission locale n'est configurée.

**Mode patch d'agent** : on applique le patch *généré par un agent* (ex. une soumission mini-swe-agent). C'est l'usage intéressant pour étudier si les tests attrapent les patches d'agents qui passent mais peuvent être sémantiquement incomplets.

Une "soumission locale" est un dossier de résultats d'agent ayant cette structure :
```
<submission>/
├── results/results.json          (listes resolved / no_logs)
├── trajs/<instance_id>/...        (trajectoires)
└── logs/<instance_id>/
    ├── patch.diff                 (le patch généré par l'agent)
    └── report.json                (verdict + tests_status)
```

Pour l'activer, définis la variable d'environnement avant de lancer :
```cmd
set TE_LOCAL_SUBMISSION_DIR=C:\chemin\vers\20260217_mini-v2.0.0_minimax-2-5-high
```

Puis :
```cmd
# Lister les instances resolved de la soumission
python -m test_enhancer.main --list-local 10

# Lancer le pipeline en utilisant le patch de l'agent
python -m test_enhancer.main --instance sympy__sympy-15345 --use-agent-patch
```

> Note importante : même en mode "patch d'agent", les métadonnées
> (`base_commit`, `test_patch`, `FAIL_TO_PASS`) viennent toujours de
> HuggingFace, car la soumission locale ne les fournit pas de façon
> exploitable pour préparer l'environnement. On combine donc les deux sources.

---

## 📂 Rôle des fichiers

| Fichier | Rôle principal |
| :--- | :--- |
| `main.py` | Interface CLI (Argparse). Gère les arguments de lancement. |
| `config.py` | Configuration globale (chemins, nom du dataset, paramètres du LLM). |
| `pipeline.py` | L'orchestrateur. Il appelle les modules dans l'ordre, gère la logique métier (ex: sélection du bon fichier annoté) et sauvegarde les résultats dans `/runs/`. |
| `dataset.py` | Interface avec `datasets` de HuggingFace. Structure les données dans une `dataclass` `Instance`. |
| `swe_runner.py` | Manipule le système (Git, Subprocess, pip). Contient la logique d'exécution de Pytest et le hook de tracing. |
| `tracer.py` | Le cœur de l'analyse dynamique. Utilise `sys.settrace`, filtre les chemins pour ignorer la `stdlib` (gère les faux positifs Windows), et formatte les objets complexes en toute sécurité. |
| `artifacts.py` | Mise en forme des données. Crée le `variable_table` et injecte les valeurs réelles à la fin des lignes de code via la fonction `annotate_source`. |
| `enhancer.py` | Logique LLM. Contient le `SYSTEM_PROMPT`, configure le client OpenAI pour router vers Gemini, et valide la sortie JSON. |
| `evaluate.py` | Comparaison statique tests originaux vs renforcés (compte assertions / fonctions de test). |
| `validate.py` | Réexécute les tests renforcés dans le repo patché et rend un verdict (syntaxe / collecte / passage). Rejette les tests hallucinés par le LLM. |
| `local_dataset.py` | Lecture d'une soumission SWE-bench locale (patches d'agent + reports), pour utiliser les patches d'agent au lieu du gold patch. |
| `demo_local.py` | Script de test autonome utilisant un faux module pour valider le comportement du `tracer.py` sans dépendre du réseau. |

---

## 🚧 Limites actuelles & Passage à la V2

La V1 a été conçue pour valider l'approche algorithmique de l'analyse dynamique locale. Cependant, elle présente des limites inhérentes à l'exécution locale :
* **Dépendances système** : Les environnements SWE-bench sont complexes et varient selon les commits. L'installation locale (`pip install -e .`) peut échouer sur des bibliothèques plus lourdes (ex: requérant du code C compilé).
* **Isolation** : L'exécution locale risque d'altérer l'environnement Conda hôte ou d'être perturbée par lui.

**Prochaine étape (V2) :** Intégration du **harness Docker officiel de SWE-bench**. L'objectif sera de déporter l'étape 3 (Exécution sous Tracer) à l'intérieur d'un conteneur éphémère pré-buildé, garantissant une reproductibilité stricte sur les 300 instances du dataset.
