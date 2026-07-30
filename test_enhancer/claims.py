"""
Vérification claim-vs-trace (phase 3.3).

Principe : chaque test généré DÉCLARE les branches qu'il prétend exercer
(dict module-level CLAIMED_BRANCHES, recopié du plan par le générateur).
On exécute ensuite chaque test individuellement sous coverage --branch et on
compare les branches revendiquées aux arcs RÉELLEMENT visités.

Verdicts par test :
  - verified   : toutes les branches revendiquées ont été visitées ;
  - falsified  : au moins une branche revendiquée n'a PAS été visitée
                 (la justification du LLM est une prédiction fausse —
                 signal automatique de test faible/halluciné, AVANT tout
                 protocole gold/agent) ;
  - no_claims  : le test ne déclare rien (le générateur a omis le dict).

NB : la couverture compte l'EXÉCUTION, pas le verdict pytest — un test qui
échoue sur assertion peut avoir des claims vérifiés (et inversement).
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def test_function_names(test_code: str) -> list[str]:
    """Noms des fonctions de test top-level du module."""
    try:
        tree = ast.parse(test_code)
    except SyntaxError:
        return []
    names = [n.name for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    seen: set[str] = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def parse_claimed_branches(test_code: str) -> dict[str, list[tuple[int, int]]]:
    """Extrait le(s) dict(s) CLAIMED_BRANCHES du module (fusionnés si le
    fichier concatène plusieurs itérations)."""
    claims: dict[str, list[tuple[int, int]]] = {}
    try:
        tree = ast.parse(test_code)
    except SyntaxError:
        return claims
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if "CLAIMED_BRANCHES" not in targets or node.value is None:
            continue
        try:
            raw = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(raw, dict):
            continue
        for name, pairs in raw.items():
            clean = []
            for p in pairs or []:
                try:
                    clean.append((int(p[0]), int(p[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            claims[str(name)] = clean
    return claims


def _observed_arcs(cov_file: Path, patched_paths: list[str]) -> set[tuple[int, int]]:
    """Arcs de branches réellement visités dans les fichiers patchés,
    lus via l'API coverage (fichier .coverage produit par le run)."""
    from coverage import CoverageData
    data = CoverageData(basename=str(cov_file))
    data.read()
    suffixes = [Path(p).as_posix() for p in patched_paths]
    arcs: set[tuple[int, int]] = set()
    for mf in data.measured_files():
        norm = Path(mf).as_posix()
        if any(norm.endswith(s) for s in suffixes):
            for a, b in (data.arcs(mf) or []):
                arcs.add((a, b))
    return arcs


def verify_claims(repo_dir: Path, test_rel_path: str, test_code: str,
                  patched_paths: list[str], verbose: bool = True) -> dict:
    """Exécute chaque fonction de test individuellement sous coverage et
    confronte ses branches revendiquées aux arcs observés."""
    claims = parse_claimed_branches(test_code)
    names = test_function_names(test_code)
    rel_posix = Path(test_rel_path).as_posix()

    report = {"tests": {}, "n_verified": 0, "n_falsified": 0, "n_no_claims": 0}
    for name in names:
        claimed = claims.get(name, [])
        cov_file = (repo_dir / f".coverage_claim_{name}").resolve()
        if cov_file.exists():
            cov_file.unlink()
        r = _run([sys.executable, "-m", "coverage", "run", "--branch",
                  "-m", "pytest", "-q", f"{rel_posix}::{name}"],
                 repo_dir, {"COVERAGE_FILE": str(cov_file)})
        passed = (r.returncode == 0)

        observed: set[tuple[int, int]] = set()
        if cov_file.exists():
            try:
                observed = _observed_arcs(cov_file, patched_paths)
            finally:
                cov_file.unlink(missing_ok=True)

        missing = [list(p) for p in claimed if tuple(p) not in observed]
        if not claimed:
            verdict = "no_claims"
            report["n_no_claims"] += 1
        elif missing:
            verdict = "falsified"
            report["n_falsified"] += 1
        else:
            verdict = "verified"
            report["n_verified"] += 1

        report["tests"][name] = {
            "verdict": verdict,
            "passed": passed,
            "claimed": [list(p) for p in claimed],
            "missing": missing,
        }
        if verbose:
            tag = {"verified": "OK ", "falsified": "FALSIFIED",
                   "no_claims": "no claims"}[verdict]
            extra = f"  manquantes: {missing}" if missing else ""
            print(f"        claim-check {name}: {tag}"
                  f" (pytest {'pass' if passed else 'FAIL'}){extra}")
    return report