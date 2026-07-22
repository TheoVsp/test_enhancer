"""
Préparation de l'environnement SWE-bench et exécution tracée des tests.

Sur le whiteboard, étapes 1-3 et 5 :
  1. find a patch (gold patch fourni par le dataset)
  2. apply the patch
  3. run test cases
  5. rerun the test case / get the debugging info

IMPLÉMENTATION V1 (sans Docker) :
Pour démarrer vite et pouvoir débugger le pipeline sur ta machine, ce runner
clone le repo en local, checkout le bon commit, applique le gold patch + le
test patch, installe le package en editable, puis exécute les FAIL_TO_PASS
sous le tracer.

ATTENTION : certaines instances SWE-bench ont des dépendances système lourdes.
Pour un run complet et reproductible, il faudra passer par le harness Docker
officiel (voir README section "Passage à Docker"). Ce module est volontairement
simple pour la V1 et fonctionne bien sur des repos "légers" (ex. certaines
instances sympy, flask, requests).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .dataset import Instance
from .tracer import VariableTracer


# ── Django instance ──────────────────────────────────────────────────────────────────────
def _is_django(instance: Instance) -> bool:
    return instance.repo == "django/django"


_DJANGO_UNITTEST_RE = re.compile(r"^(?P<method>\w+)\s+\((?P<path>[\w\.]+)\)$")


def _to_django_label(raw_id: str) -> str:
    """'test_id (app.tests.TestClass)' -> 'app.tests.TestClass.test_id'.

    Convertit un FAIL_TO_PASS Django au format attendu par
    tests/runtests.py (label pointé), au lieu d'un node id pytest.
    """
    m = _DJANGO_UNITTEST_RE.match(raw_id.strip())
    if not m:
        return raw_id.strip()  # déjà au format label pointé, laissé tel quel
    return f"{m.group('path')}.{m.group('method')}"


@dataclass
class RunResult:
    success: bool
    repo_dir: Path
    tracer: VariableTracer | None
    stdout: str
    stderr: str


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Wrapper subprocess avec logs."""
    print(f"  $ {' '.join(cmd)}")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False, env=env
    )
    if check and result.returncode != 0:
        print(f"  [!] commande échouée (code {result.returncode})")
        print(f"  stderr: {result.stderr[:500]}")
    return result


def prepare_repo(instance: Instance, work_root: Path) -> Path:
    """Clone le repo, checkout le commit, applique le patch (gold ou agent) + test patch.

    Le patch appliqué est `instance.patch_to_apply` : le patch de l'agent s'il
    a été chargé depuis une soumission locale, sinon le gold patch. Le test
    patch est toujours appliqué (c'est lui qui amène les tests FAIL_TO_PASS).
    """
    work_root.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{instance.repo}.git"
    repo_dir = work_root / instance.instance_id

    if not repo_dir.exists():
        _run(["git", "clone", repo_url, str(repo_dir)])

    # On se place au commit de base (avant le fix)
    _run(["git", "checkout", "-f", instance.base_commit], cwd=repo_dir)
    _run(["git", "clean", "-fdx"], cwd=repo_dir, check=False)

    # Application du patch de code (gold OU agent) puis du test patch (les tests).
    # IMPORTANT : on applique le test_patch APRÈS le patch de code, car c'est
    # lui qui introduit les tests FAIL_TO_PASS qu'on va exécuter.
    for label, patch_text in [
        ("code", instance.patch_to_apply),
        ("test", instance.test_patch),
    ]:
        if not patch_text.strip():
            continue
        patch_file = repo_dir / f"_te_{label}.patch"
        patch_file.write_text(patch_text, encoding="utf-8")
        res = _run(["git", "apply", "--verbose", str(patch_file)], cwd=repo_dir, check=False)
        if res.returncode != 0:
            # fallback : patch -p1 est parfois plus tolérant que git apply
            _run(["patch", "-p1", "-i", str(patch_file)], cwd=repo_dir, check=False)

    return repo_dir


def install_repo(repo_dir: Path) -> None:
    """Installe le package en editable (best effort)."""
    _run([sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
         cwd=repo_dir, check=False)


def extract_test_files(test_patch: str) -> list[str]:
    """Extrait les chemins des fichiers de test modifiés par le test_patch.

    On lit les lignes '+++ b/<chemin>' du diff. Ce sont les fichiers où
    se trouvent les tests FAIL_TO_PASS.
    """
    files: list[str] = []
    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            if path.endswith(".py") and path not in files:
                files.append(path)
    return files


def resolve_node_ids(test_ids: list[str], test_patch: str) -> list[str]:
    """Reconstruit des node ids pytest complets.

    Les FAIL_TO_PASS de SWE-bench sont parfois des noms de fonction nus
    (ex. 'test_prefix_operations') que pytest ne sait pas localiser. On les
    préfixe avec le(s) fichier(s) de test extraits du test_patch pour obtenir
    'chemin/test_file.py::test_prefix_operations'.

    Les ids déjà au format 'fichier.py::test' sont laissés tels quels.
    """
    test_files = extract_test_files(test_patch)
    resolved: list[str] = []
    for tid in test_ids:
        if "::" in tid or tid.endswith(".py") or "/" in tid:
            # déjà un node id exploitable par pytest
            resolved.append(tid)
            continue
        if test_files:
            # nom de fonction nu -> on le cherche dans chaque fichier de test
            for tf in test_files:
                resolved.append(f"{tf}::{tid}")
        else:
            resolved.append(tid)  # fallback : on laisse pytest se débrouiller
    return resolved

def _get_watch_dir(repo_dir: Path) -> Path:
    """Dérive le dossier du package à tracer depuis le nom de l'instance.
    
    'sympy__sympy-20590' -> repo_dir/sympy
    'django__django-12345' -> repo_dir/django
    Falls back to repo_dir if the subdirectory doesn't exist.
    """
    package_name = repo_dir.name.split("__")[0]
    watch_dir = repo_dir / package_name
    if watch_dir.exists():
        return watch_dir
    # Some repos use src/ layout
    src_layout = repo_dir / "src" / package_name
    if src_layout.exists():
        return src_layout
    return repo_dir

def _run_django_tests(repo_dir: Path, test_ids: list[str]) -> RunResult:
    """Exécute les tests Django via tests/runtests.py (pas pytest).

    Django a son propre test runner et ses labels ne sont pas des node ids
    pytest ('app.tests.TestClass.test_id' au lieu de 'path.py::test_id').
    Contrairement à la version Docker, le tracer n'est PAS injecté ici pour
    la V1 (pas de sitecustomize/PYTHONPATH runtime) : ce wrapper local sert
    surtout à valider que les FAIL_TO_PASS passent, sans trace de variables.
    """
    labels = [_to_django_label(tid) for tid in test_ids]
    cmd = [
        sys.executable, "tests/runtests.py",
        "--settings=test_sqlite", "--parallel", "1", "--verbosity", "2",
        *labels,
    ]
    result = _run(cmd, cwd=repo_dir, check=False)
    return RunResult(
        success=(result.returncode == 0),
        repo_dir=repo_dir,
        tracer=None,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_tests_traced(
    repo_dir: Path,
    test_ids: list[str],
    target_files: list[Path] = None,
    instance: Instance | None = None,
) -> RunResult:
    """Exécute les tests donnés sous le tracer de variables.

    On utilise pytest en l'important programmatiquement et on injecte un
    plugin à la volée pour n'activer le tracer QUE pendant l'exécution
    du test (ignorant ainsi tout le bruit d'importation des modules).

    Si `instance` est fourni et correspond à django/django, on bascule sur
    tests/runtests.py (voir docker_runner.py pour l'équivalent en conteneur),
    car pytest ne sait pas exécuter la suite de tests Django telle quelle.
    """
    if _is_django(instance):
        return _run_django_tests(repo_dir, test_ids)

    watch_dir = _get_watch_dir(repo_dir)
    abs_target= None
    if target_files is not None:
        abs_target= {str((repo_dir /f).resolve())for f in target_files}
    tracer = VariableTracer(watch_dir=watch_dir, target_files=abs_target)

    # On construit les arguments pytest
    pytest_args = ["-x", "-q", "--no-header", *test_ids]

    import io
    import contextlib

    out_buf, err_buf = io.StringIO(), io.StringIO()
    success = False
    try:
        import pytest
        
        # --- NOTRE MICRO-PLUGIN PYTEST ---
        class TracerPlugin:
            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_call(self, item):
                # Cette méthode enveloppe uniquement l'exécution pure du test.
                # On active le tracer juste avant...
                with tracer:
                    yield  # ... on laisse le test tourner ...
                # ... et le 'with tracer' s'éteint tout seul à la sortie !

        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            import os
            old_cwd = os.getcwd()
            os.chdir(repo_dir)
            try:
                # On passe notre plugin à pytest
                ret = pytest.main(pytest_args, plugins=[TracerPlugin()])
            finally:
                os.chdir(old_cwd)
        success = (ret == 0)
    except Exception as exc:  # noqa: BLE001
        err_buf.write(f"\nException pendant l'exécution: {exc}")

    return RunResult(
        success=success,
        repo_dir=repo_dir,
        tracer=tracer,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
    )

def run_single_test_traced(
    repo_dir: Path,
    test_id: str,
    target_files: list[Path] = None,
    instance: Instance | None = None,
) -> RunResult:
    """Wrapper : trace UN seul test (pendant local de
    run_single_test_traced_docker). Réutilise run_tests_traced avec une
    liste d'un seul élément."""
    return run_tests_traced(
        repo_dir=repo_dir,
        test_ids=[test_id],
        target_files=target_files,
        instance=instance,
    )