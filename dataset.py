from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# On utilise load_from_disk pour lire le format Arrow/Hugging Face local
from datasets import load_from_disk

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
            # Les champs FAIL_TO_PASS / PASS_TO_PASS peuvent être stockés en JSON string binaire ou liste
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return [value] if value else []
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
    """Charge les instances SWE-bench depuis le dossier de cache local.

    Args:
        limit: si fourni, ne renvoie que les `limit` premières instances.
    """
    # load_from_disk va chercher automatiquement le fichier .arrow et state.json
    ds = load_from_disk(DATASET_DIR)
    
    rows = ds if limit is None else ds.select(range(min(limit, len(ds))))
    return [Instance.from_row(row) for row in rows]


def get_instance(instance_id: str) -> Instance:
    """Récupère une instance précise par son id depuis le dossier local."""
    ds = load_from_disk(DATASET_DIR)
    
    for row in ds:
        if row["instance_id"] == instance_id:
            return Instance.from_row(row)
            
    raise ValueError(f"Instance introuvable dans le dataset local : {instance_id}")