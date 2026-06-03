"""
Lecture d'une soumission SWE-bench locale (dossier de résultats d'un agent).

Le dossier qu'on nous a fourni n'est PAS le dataset SWE-bench lui-même : c'est
le résultat d'un run de mini-swe-agent. Sa structure est :

  <submission>/
  ├── metadata.yaml          (infos sur le run : modèle, coût, % resolved)
  ├── results/
  │   └── results.json       (listes 'resolved' / 'no_logs' / 'no_generation')
  ├── trajs/<instance_id>/<instance_id>.traj.json   (trajectoire complète)
  └── logs/<instance_id>/
      ├── patch.diff         (LE PATCH GÉNÉRÉ PAR L'AGENT)
      ├── report.json        (verdict : resolved + tests_status FAIL_TO_PASS...)
      └── job-output.json    (log d'exécution détaillé)

Différence cruciale avec le dataset HuggingFace :
  - HuggingFace fournit le GOLD PATCH (fix du développeur) + base_commit +
    test_patch + FAIL_TO_PASS. C'est ce qu'il faut pour préparer l'environnement.
  - La soumission locale fournit le PATCH DE L'AGENT (potentiellement différent
    du gold, et c'est tout l'intérêt : un patch qui "passe les tests" mais qui
    pourrait être sémantiquement incomplet).

Ce module lit la soumission locale. Pour obtenir un environnement exécutable,
on le COMBINE avec les métadonnées HuggingFace (voir dataset.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LocalSubmissionEntry:
    """Une entrée de la soumission locale pour une instance donnée."""
    instance_id: str
    agent_patch: str            # le patch généré par l'agent (patch.diff)
    resolved: bool              # l'agent a-t-il résolu l'instance selon le report ?
    fail_to_pass_success: list[str] = field(default_factory=list)
    fail_to_pass_failure: list[str] = field(default_factory=list)
    pass_to_pass_success: list[str] = field(default_factory=list)
    pass_to_pass_failure: list[str] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)


class LocalSubmission:
    """Accès aux données d'une soumission SWE-bench locale."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"Dossier de soumission introuvable : {self.root}")
        self.logs_dir = self.root / "logs"
        self.trajs_dir = self.root / "trajs"
        self.results_file = self.root / "results" / "results.json"

    def list_instance_ids(self) -> list[str]:
        """Liste les instances présentes (celles qui ont un dossier de logs)."""
        if not self.logs_dir.exists():
            return []
        ids = [
            p.name for p in sorted(self.logs_dir.iterdir())
            if p.is_dir() and not p.name.startswith(".")
        ]
        return ids

    def list_resolved_ids(self) -> list[str]:
        """Liste uniquement les instances marquées 'resolved' par l'agent."""
        if not self.results_file.exists():
            return []
        data = json.loads(self.results_file.read_text(encoding="utf-8"))
        return list(data.get("resolved", []))

    def get_entry(self, instance_id: str) -> LocalSubmissionEntry:
        """Charge le patch d'agent et le report d'une instance."""
        log_dir = self.logs_dir / instance_id
        if not log_dir.exists():
            raise ValueError(
                f"Instance '{instance_id}' absente de la soumission locale "
                f"(dossier {log_dir} introuvable)."
            )

        # Le patch généré par l'agent
        patch_file = log_dir / "patch.diff"
        agent_patch = patch_file.read_text(encoding="utf-8") if patch_file.exists() else ""

        # Le report (verdict + détail des tests)
        report: dict[str, Any] = {}
        report_file = log_dir / "report.json"
        if report_file.exists():
            report = json.loads(report_file.read_text(encoding="utf-8"))

        tests_status = report.get("tests_status", {})
        f2p = tests_status.get("FAIL_TO_PASS", {})
        p2p = tests_status.get("PASS_TO_PASS", {})

        return LocalSubmissionEntry(
            instance_id=instance_id,
            agent_patch=agent_patch,
            resolved=bool(report.get("resolved", False)),
            fail_to_pass_success=f2p.get("success", []),
            fail_to_pass_failure=f2p.get("failure", []),
            pass_to_pass_success=p2p.get("success", []),
            pass_to_pass_failure=p2p.get("failure", []),
            report=report,
        )
