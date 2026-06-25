"""
Évaluation "kill rate" — le cœur de la tâche d'évaluation (demande du prof).

Question : les tests renforcés attrapent-ils des patches incorrects que les
tests originaux laissaient passer ?

Protocole pour une instance :
  1. On a des tests renforcés (générés au préalable par le pipeline).
  2. On les exécute contre le patch AGENT et contre le patch GOLD.
  3. On classe chaque test :
       - passe sur GOLD, échoue sur AGENT  -> KILL (le patch agent est pris en défaut)
       - échoue sur GOLD (et sur AGENT)     -> HALLUCINATION (test faux, à filtrer)
       - passe sur les deux                 -> OK (test valide mais ne discrimine pas)
       - échoue sur GOLD, passe sur AGENT   -> ANOMALIE (à investiguer)

Un patch est "tué" s'il existe au moins un test KILL.

Ce module fournit la logique réutilisable. Le script run_kill_eval.py
l'utilise pour une ou plusieurs instances.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestVerdict:
    name: str
    passed_on_gold: bool
    passed_on_agent: bool

    @property
    def category(self) -> str:
        if self.passed_on_gold and not self.passed_on_agent:
            return "KILL"          # passe gold, échoue agent -> vrai kill
        if not self.passed_on_gold and self.passed_on_agent:
            return "ANOMALY"       # échoue gold mais passe agent -> bizarre
        if not self.passed_on_gold and not self.passed_on_agent:
            return "HALLUCINATION" # échoue les deux -> test faux
        return "OK"                # passe les deux -> valide mais ne discrimine pas


@dataclass
class InstanceKillResult:
    instance_id: str
    verdicts: list[TestVerdict] = field(default_factory=list)

    @property
    def n_kills(self) -> int:
        return sum(1 for v in self.verdicts if v.category == "KILL")

    @property
    def n_hallucinations(self) -> int:
        return sum(1 for v in self.verdicts if v.category == "HALLUCINATION")

    @property
    def patch_killed(self) -> bool:
        return self.n_kills > 0

    def summary(self) -> str:
        cats = {}
        for v in self.verdicts:
            cats[v.category] = cats.get(v.category, 0) + 1
        return (f"{self.instance_id}: {'KILLED' if self.patch_killed else 'survived'} "
                f"| kills={self.n_kills} hallucinations={self.n_hallucinations} "
                f"| {cats}")


def _per_test_results(stdout: str) -> dict[str, bool]:
    """Parse la sortie pytest -rA pour savoir quels tests PASSENT.

    Returns {nom_court_du_test: True si passed}.
    """
    results = {}
    for line in stdout.splitlines():
        line = line.strip()
        for prefix, ok in (("PASSED", True), ("FAILED", False), ("ERROR", False)):
            if line.startswith(prefix + " "):
                rest = line[len(prefix) + 1:]
                # ex: "test_te_enhanced.py::test_xxx - ..." -> garder test_xxx
                node = rest.split(" - ")[0]
                name = node.split("::")[-1]
                results[name] = ok
    return results


def evaluate_kill_for_instance(
    instance_id: str,
    enhanced_tests_path: Path,
    repo_dir: Path,
    base_commit: str,
    gold_patch: str,
    agent_patch: str,
    test_patch: str,
    validate_module,
) -> InstanceKillResult:
    """Lance les tests renforcés sur GOLD puis AGENT et compare.

    `validate_module` est le module validate (injecté pour réutiliser
    validate_enhanced_tests). `repo_dir` doit être un repo déjà clonable
    avec le paquet installé.
    """
    enhanced = enhanced_tests_path.read_text(encoding="utf-8")

    def run(cmd):
        return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)

    def prepare(code_patch: str):
        run(["git", "checkout", "-f", base_commit])
        run(["git", "clean", "-fdx"])
        for label, txt in [("code", code_patch), ("test", test_patch)]:
            if not txt.strip():
                continue
            pf = (repo_dir / f"_kill_{label}.patch").resolve()
            pf.write_text(txt, encoding="utf-8")
            run(["git", "apply", "--verbose", str(pf)])

    # 1. GOLD
    prepare(gold_patch)
    res_gold = validate_module.validate_enhanced_tests(repo_dir, enhanced)
    gold_results = _per_test_results(res_gold.stdout)

    # 2. AGENT
    prepare(agent_patch)
    res_agent = validate_module.validate_enhanced_tests(repo_dir, enhanced)
    agent_results = _per_test_results(res_agent.stdout)

    # 3. Comparer test par test
    all_names = set(gold_results) | set(agent_results)
    verdicts = []
    for name in sorted(all_names):
        verdicts.append(TestVerdict(
            name=name,
            passed_on_gold=gold_results.get(name, False),
            passed_on_agent=agent_results.get(name, False),
        ))
    return InstanceKillResult(instance_id=instance_id, verdicts=verdicts)
