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

from .config import DATASET_DIR


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
    environment_setup_commit: str
    problem_statement: str
    raw: dict[str, Any]  # on garde l'instance brute au cas où

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
            raw=row,
        )


def load_instances(limit: int | None = None) -> list[Instance]:
    """Charge les instances SWE-bench Lite depuis HuggingFace.

    Args:
        limit: si fourni, ne renvoie que les `limit` premières instances.
               Très utile pour débugger sur 1-2 instances avant un run complet.
    """
    ds = load_dataset(DATASET_DIR)
    rows = ds if limit is None else ds.select(range(min(limit, len(ds))))
    return [Instance.from_row(row) for row in rows]


def get_instance(instance_id: str) -> Instance:
    """Récupère une instance précise par son id."""
    ds = load_dataset(DATASET_DIR)
    for row in ds:
        if row["instance_id"] == instance_id:
            return Instance.from_row(row)
    raise ValueError(f"Instance introuvable: {instance_id}")
