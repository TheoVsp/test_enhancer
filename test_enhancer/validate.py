"""
Validation des tests renforcés générés par le LLM (point critique de la V2).

Problème que ce module résout :
le LLM produit des `enhanced_tests`, mais RIEN ne garantit qu'ils sont
corrects. Le LLM peut :
  - générer du code avec des erreurs de syntaxe,
  - appeler des fonctions qui n'existent pas,
  - écrire des assertions FAUSSES (il hallucine une valeur attendue).

Sans vérification, on ne peut pas affirmer que les tests renforcés valent
mieux que les originaux. Ce module réexécute donc les tests renforcés dans
le repo déjà préparé (patch appliqué) et renvoie un verdict.

Critères de validité (inspirés de STING) :
  1. SYNTAXE   : le code des tests parse correctement (ast.parse).
  2. EXÉCUTION : pytest peut collecter et lancer les tests sans erreur d'import.
  3. PASSAGE   : les tests PASSENT sur le patch appliqué (c'est l'oracle :
                 un bon test renforcé doit passer sur le code corrigé).

Un test renforcé qui ne passe pas cette validation est rejeté.
"""
from __future__ import annotations

import ast
import contextlib
import io
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ValidationResult:
    """Verdict de validation des tests renforcés."""
    syntax_ok: bool
    collected_ok: bool
    passed: bool
    n_passed: int = 0
    n_failed: int = 0
    n_errors: int = 0
    stdout: str = ""
    stderr: str = ""
    test_file: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Les tests renforcés sont valides s'ils parsent, se collectent ET passent."""
        return self.syntax_ok and self.collected_ok and self.passed

    def as_dict(self) -> dict:
        return {
            "syntax_ok": self.syntax_ok,
            "collected_ok": self.collected_ok,
            "passed": self.passed,
            "is_valid": self.is_valid,
            "n_passed": self.n_passed,
            "n_failed": self.n_failed,
            "n_errors": self.n_errors,
            "test_file": self.test_file,
            "notes": self.notes,
        }


def check_syntax(test_code: str) -> tuple[bool, str]:
    """Vérifie que le code des tests est syntaxiquement valide."""
    try:
        ast.parse(test_code)
        return True, ""
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc}"


def _parse_pytest_counts(output: str) -> tuple[int, int, int]:
    """Extrait (passed, failed, errors) de la ligne de résumé pytest.

    On lit la dernière ligne de résumé du type '3 passed, 1 failed in 0.2s'.
    Parsing volontairement simple et robuste.
    """
    passed = failed = errors = 0
    for token, _ in [("passed", 0), ("failed", 0), ("error", 0)]:
        # cherche un motif 'N passed', 'N failed', 'N error(s)'
        import re
        m = re.search(rf"(\d+)\s+{token}", output)
        if m:
            n = int(m.group(1))
            if token == "passed":
                passed = n
            elif token == "failed":
                failed = n
            else:
                errors = n
    return passed, failed, errors


def validate_enhanced_tests(
    repo_dir: Path,
    enhanced_tests: str,
    base_test_path: str | None = None,
) -> ValidationResult:
    """Valide les tests renforcés en les exécutant dans le repo préparé.

    Args:
        repo_dir: le repo déjà cloné/patché/installé (sortie de swe_runner).
        enhanced_tests: le code Python des tests renforcés (string).
        base_test_path: chemin relatif (dans le repo) du fichier de test
            original, pour placer le fichier renforcé au bon endroit (mêmes
            imports relatifs disponibles). Si None, on place dans un tmp.

    Returns:
        ValidationResult avec le verdict détaillé.
    """
    notes: list[str] = []

    # 1. Vérification syntaxique (rapide, avant de toucher au disque)
    syntax_ok, syntax_err = check_syntax(enhanced_tests)
    if not syntax_ok:
        notes.append(syntax_err)
        return ValidationResult(
            syntax_ok=False, collected_ok=False, passed=False, notes=notes,
        )

    repo_dir = Path(repo_dir)

    # 2. Écrire le fichier de tests renforcés à un endroit où les imports
    #    du projet fonctionnent. Le plus sûr : à côté du fichier de test
    #    original (même package, mêmes imports relatifs).
    if base_test_path:
        base = repo_dir / base_test_path
        target_dir = base.parent
    else:
        target_dir = repo_dir

    target_dir.mkdir(parents=True, exist_ok=True)
    enhanced_file = target_dir / "test_te_enhanced.py"
    try:
        enhanced_file.write_text(enhanced_tests, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Écriture du fichier de test échouée : {exc}")
        return ValidationResult(
            syntax_ok=True, collected_ok=False, passed=False, notes=notes,
        )

    # 3. Lancer pytest sur ce fichier dans un sous-processus isolé.
    #    Sous-processus (pas pytest.main en in-process) pour éviter les
    #    conflits d'état avec un éventuel pytest déjà importé, et pour ne
    #    pas polluer le process courant.
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    # On s'assure que le repo est dans le PYTHONPATH pour les imports.
    env["PYTHONPATH"] = str(repo_dir) + os.pathsep + env.get("PYTHONPATH", "")

    # -rA : résumé de TOUS les tests (raison de passage/échec)
    # --tb=short : traceback court mais lisible (pour diagnostiquer les échecs)
    cmd = [sys.executable, "-m", "pytest", "-rA", "--tb=short", "--no-header",
           "-p", "no:cacheprovider", str(enhanced_file)]

    success = False
    stdout = stderr = ""
    n_passed = n_failed = n_errors = 0
    try:
        proc = subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, text=True,
            env=env, timeout=300,
        )
        stdout, stderr = proc.stdout, proc.stderr
        n_passed, n_failed, n_errors = _parse_pytest_counts(stdout)
        # pytest renvoie 0 si tout passe, 1 si échecs, 2+ si erreurs de collecte
        collected_ok = (proc.returncode in (0, 1)) and n_errors == 0
        passed = (proc.returncode == 0) and n_passed > 0 and n_failed == 0
        success = passed
        if proc.returncode >= 2:
            notes.append("pytest n'a pas pu collecter/exécuter les tests "
                         "(erreur d'import ou de collecte).")
    except subprocess.TimeoutExpired:
        notes.append("Timeout (>300s) pendant l'exécution des tests renforcés.")
        collected_ok = False
        passed = False
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Exception pendant pytest : {exc}")
        collected_ok = False
        passed = False
    finally:
        # On nettoie le fichier de test temporaire pour ne pas polluer le repo.
        with contextlib.suppress(Exception):
            enhanced_file.unlink()

    return ValidationResult(
        syntax_ok=True,
        collected_ok=collected_ok,
        passed=passed,
        n_passed=n_passed,
        n_failed=n_failed,
        n_errors=n_errors,
        stdout=stdout,
        stderr=stderr,
        test_file=str(enhanced_file.relative_to(repo_dir)) if base_test_path else "test_te_enhanced.py",
        notes=notes,
    )
