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
    # Décomposition des « failed » de pytest en deux catégories métier :
    n_run_errors: int = 0       # tests qui ne tournent pas (exception != assertion)
    n_assertion_fails: int = 0  # tests qui tournent mais assertion fausse
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
            "n_run_errors": self.n_run_errors,
            "n_assertion_fails": self.n_assertion_fails,
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


def _classify_failures(output: str) -> tuple[int, int]:
    """Distingue, parmi les tests FAILED, deux catégories :

      - run_errors      : le test NE TOURNE PAS vraiment (il lève une exception
                          autre qu'AssertionError : NameError, ImportError,
                          TypeError, AttributeError...). -> à réparer.
      - assertion_fails : le test tourne mais son assertion est fausse
                          (AssertionError). -> gardé et signalé.

    IMPORTANT : pytest compte ces DEUX cas comme « failed » dans son résumé.
    La seule façon fiable de les séparer est de lire les lignes de détail
    « FAILED ... - <ExceptionType>: ... » produites par l'option -rA.

    Returns (run_errors, assertion_fails).
    """
    import re
    run_errors = 0
    assertion_fails = 0
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("FAILED "):
            continue
        # Forme attendue : "FAILED path::test - ExceptionType: message"
        # (s'il n'y a pas de " - ", on ne peut pas savoir -> on suppose assertion)
        if " - " not in line:
            assertion_fails += 1
            continue
        reason = line.split(" - ", 1)[1]
        if reason.startswith("AssertionError") or reason.startswith("assert"):
            assertion_fails += 1
        else:
            # NameError, ImportError, TypeError, AttributeError, SyntaxError...
            run_errors += 1
    return run_errors, assertion_fails


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
        # On décompose les « failed » en : tests qui ne tournent pas (exception
        # != assertion) vs assertions fausses. C'est la distinction du prof.
        n_run_errors, n_assertion_fails = _classify_failures(stdout)
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
        n_run_errors = n_assertion_fails = 0
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Exception pendant pytest : {exc}")
        collected_ok = False
        passed = False
        n_run_errors = n_assertion_fails = 0
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
        n_run_errors=n_run_errors,
        n_assertion_fails=n_assertion_fails,
        stdout=stdout,
        stderr=stderr,
        test_file=str(enhanced_file.relative_to(repo_dir)) if base_test_path else "test_te_enhanced.py",
        notes=notes,
    )


# ===========================================================================
# Boucle de réparation (étape 4 du prof : « if the test does not run, fix it »)
# ===========================================================================

@dataclass
class RepairOutcome:
    """Résultat de la boucle de validation + réparation."""
    final_tests: str            # le code de test après réparations
    iterations: int             # nombre d'itérations de réparation effectuées
    result: ValidationResult    # la dernière validation
    repaired: bool              # True si au moins une réparation a eu lieu

    @property
    def has_run_errors(self) -> bool:
        """Reste-t-il des tests qui NE TOURNENT PAS ?

        Cela inclut : les erreurs de collecte/import pytest (n_errors), ET les
        tests qui lèvent une exception autre qu'AssertionError (n_run_errors)."""
        return (self.result.n_errors > 0
                or self.result.n_run_errors > 0
                or not self.result.collected_ok)

    @property
    def has_assertion_failures(self) -> bool:
        """Reste-t-il des tests qui tournent mais échouent sur assertion ?

        Ces tests sont GARDÉS et SIGNALÉS : ils peuvent révéler un vrai bug
        du patch (politique décidée avec le prof)."""
        return self.result.n_assertion_fails > 0


def _count_test_functions(code: str) -> int:
    """Compte les fonctions de test dans un bloc de code (pour détecter une
    troncature pendant la réparation)."""
    import ast as _ast
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        import re
        return len(re.findall(r"^\\s*def\\s+test\\w*\\s*\\(", code, re.MULTILINE))
    return sum(
        1 for n in _ast.walk(tree)
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and n.name.startswith("test")
    )


def validate_with_repair(
    repo_dir: Path,
    enhanced_tests: str,
    annotated_code: str,
    base_test_path: str | None = None,
    max_iterations: int = 3,
) -> RepairOutcome:
    """Valide les tests, et CORRIGE en boucle ceux qui NE TOURNENT PAS.

    Politique (décidée avec le prof) :
      - Un test qui PLANTE (erreur de syntaxe / import / API) -> on le renvoie
        au LLM pour correction, jusqu'à `max_iterations` fois.
      - Un test qui TOURNE mais échoue sur une assertion -> on NE le corrige
        PAS : on le garde et on le signale (il peut révéler un vrai bug du
        patch). C'est `has_assertion_failures`.

    Returns:
        RepairOutcome avec le code final et le verdict détaillé.
    """
    from . import enhancer  # import local pour éviter les cycles

    current = enhanced_tests
    iterations = 0
    repaired = False

    result = validate_enhanced_tests(repo_dir, current, base_test_path)

    # On boucle TANT QU'il reste des erreurs d'exécution (pas des assertions)
    # et qu'on n'a pas atteint la limite.
    while iterations < max_iterations:
        run_errors = (
            (result.n_errors > 0)
            or (result.n_run_errors > 0)
            or (not result.collected_ok)
            or (not result.syntax_ok)
        )
        if not run_errors:
            break  # plus rien à réparer (les assertions ratées ne se réparent pas)

        # On demande au LLM de corriger UNIQUEMENT ce qui empêche de tourner.
        error_output = (result.stdout or "") + "\n" + (result.stderr or "")
        fixed = enhancer.repair_tests(
            failing_code=current,
            error_output=error_output,
            annotated_code=annotated_code,
        )
        iterations += 1
        if not fixed.strip() or fixed.strip() == current.strip():
            # le LLM n'a rien changé -> inutile de continuer
            break

        # PROTECTION ANTI-TRONCATURE : on n'accepte la réparation que si elle
        # ne fait PAS perdre de tests. Le LLM renvoie parfois un fichier tronqué
        # (il "oublie" les tests qui marchaient), ce qui détruirait du travail
        # valide. Si le fichier réparé a moins de fonctions de test, on rejette
        # la réparation et on garde la version précédente.
        n_before = _count_test_functions(current)
        n_after = _count_test_functions(fixed)
        if n_after < n_before:
            # réparation rejetée : elle tronque le fichier
            break

        current = fixed
        repaired = True
        result = validate_enhanced_tests(repo_dir, current, base_test_path)

    return RepairOutcome(
        final_tests=current,
        iterations=iterations,
        result=result,
        repaired=repaired,
    )
