"""
Chargement des instances SWE-bench Lite.

Une "instance" SWE-bench contient (champs utiles pour nous) :
  - instance_id        : identifiant unique, ex. "django__django-12345"
  - repo               : "django/django"
  - base_commit        : commit AVANT le fix
  - patch              : le GOLD PATCH (le fix du développeur) -> notre "patch qui passe"
  - test_patch         : le patch qui ajoute/modifie les tests
  - FAIL_TO_PASS       : tests qui échouent avant le patch et passent après (JSON list)
  - PASS_TO_PASS       : tests qui passent avant et après (JSON list)
  - environment_setup_commit : commit pour installer les dépendances

Pour la V1, "le patch qui passe" = gold patch, et "les tests" = FAIL_TO_PASS.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from datasets import load_dataset

from .config import DATASET_NAME, DATASET_SPLIT, LOCAL_SUBMISSION_DIR


@dataclass
class Instance:
    """Représentation simplifiée d'une instance SWE-bench."""
    instance_id: str
    repo: str
    base_commit: str
    gold_patch: str
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    version: str
    environment_setup_commit: str
    problem_statement: str
    raw: dict[str, Any]  # on garde l'instance brute au cas où
    # Patch généré par un agent (chargé depuis une soumission locale).
    # Vaut "" si on n'utilise que le dataset HuggingFace.
    agent_patch: str = ""

    @property
    def patch_to_apply(self) -> str:
        """Le patch effectivement appliqué : celui de l'agent si présent,
        sinon le gold patch. C'est ce qui caractérise 'le patch qui passe'."""
        return self.agent_patch if self.agent_patch.strip() else self.gold_patch

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Instance":
        def _parse_list(value: Any) -> list[str]:
            # Les champs FAIL_TO_PASS / PASS_TO_PASS sont stockés en JSON string.
            if isinstance(value, str):
                return json.loads(value)
            return list(value) if value else []

        return cls(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            gold_patch=row["patch"],
            test_patch=row["test_patch"],
            fail_to_pass=_parse_list(row.get("FAIL_TO_PASS", "[]")),
            pass_to_pass=_parse_list(row.get("PASS_TO_PASS", "[]")),
            environment_setup_commit=row.get("environment_setup_commit", row["base_commit"]),
            problem_statement=row.get("problem_statement", ""),
            version=row.get("version",""),
            raw=row,
        )


def load_instances(limit: int | None = None) -> list[Instance]:
    """Charge les instances SWE-bench Lite depuis HuggingFace.

    Args:
        limit: si fourni, ne renvoie que les `limit` premières instances.
               Très utile pour débugger sur 1-2 instances avant un run complet.
    """
    ds = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    rows = ds if limit is None else ds.select(range(min(limit, len(ds))))
    return [Instance.from_row(row) for row in rows]


def get_instance(instance_id: str, use_agent_patch: bool | None = None) -> Instance:
    """Récupère une instance précise par son id.

    Args:
        instance_id: ex. "sympy__sympy-24909".
        use_agent_patch: si True, on charge en plus le patch généré par
            l'agent depuis la soumission locale (config.LOCAL_SUBMISSION_DIR)
            et on l'attache à l'instance. Si None, on l'active automatiquement
            dès qu'une soumission locale est configurée.

    Les métadonnées (base_commit, test_patch, FAIL_TO_PASS) viennent TOUJOURS
    de HuggingFace, car la soumission locale ne les fournit pas de façon
    exploitable pour préparer l'environnement.
    """
    ds = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    instance: Instance | None = None
    for row in ds:
        if row["instance_id"] == instance_id:
            instance = Instance.from_row(row)
            break
    if instance is None:
        raise ValueError(f"Instance introuvable dans HuggingFace : {instance_id}")

    # Décider si on enrichit avec le patch d'agent local
    if use_agent_patch is None:
        use_agent_patch = bool(LOCAL_SUBMISSION_DIR)

    if use_agent_patch and LOCAL_SUBMISSION_DIR:
        # Import local pour ne pas exiger le module si on ne s'en sert pas
        from .local_dataset import LocalSubmission
        sub = LocalSubmission(LOCAL_SUBMISSION_DIR)
        try:
            entry = sub.get_entry(instance_id)
            instance.agent_patch = entry.agent_patch
        except ValueError:
            # l'instance n'est pas dans la soumission locale -> on garde le gold
            pass

    return instance
