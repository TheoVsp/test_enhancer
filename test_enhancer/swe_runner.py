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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .dataset import Instance
from .tracer import VariableTracer


class PatchApplicationError(RuntimeError):
    """Le patch de code n'a pas pu être appliqué : instance non exploitable.

    Fréquent avec les patches d'agent NON-RESOLVED, qui sont souvent mal formés
    (contexte décalé, fichier absent, diff corrompu). Ces instances doivent être
    écartées de l'évaluation plutôt que de faire planter tout un run.
    """


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


def _has_command(cmd: str) -> bool:
    """Vrai si la commande existe sur le système (ex. 'patch' absent sur Windows)."""
    return shutil.which(cmd) is not None


def _apply_patch(patch_file: Path, repo_dir: Path, label: str) -> bool:
    """Applique un patch avec plusieurs niveaux de tolérance.

    Les patches générés par un agent (surtout NON-RESOLVED) sont souvent mal
    formés : espaces, contexte décalé, fins de ligne. On tente donc plusieurs
    stratégies, de la plus stricte à la plus permissive :

      1. git apply (strict)
      2. git apply --ignore-whitespace
      3. git apply -3                (merge à 3 points)
      4. patch -p1                   (si l'outil existe ; absent sur Windows)

    Toutes ces stratégies appliquent le patch ENTIÈREMENT ou pas du tout.
    On évite volontairement --reject, qui applique partiellement et laisserait
    le repo dans un état incohérent (résultats d'évaluation invalides).

    Returns:
        True si le patch a été appliqué INTÉGRALEMENT, False sinon.
    """
    # IMPORTANT : on n'utilise PAS --reject. Cette option applique le patch
    # PARTIELLEMENT (elle applique ce qu'elle peut et rejette le reste), ce qui
    # laisse le repo dans un état INCOHÉRENT : ni le patch de l'agent, ni le
    # gold. Évaluer sur un tel repo produirait des résultats faux. On préfère
    # échouer proprement et écarter l'instance.
    attempts = [
        (["git", "apply", "--verbose", str(patch_file)], "strict"),
        (["git", "apply", "--ignore-whitespace", str(patch_file)], "ignore-whitespace"),
        (["git", "apply", "-3", str(patch_file)], "3-way merge"),
    ]
    for cmd, strategy in attempts:
        res = _run(cmd, cwd=repo_dir, check=False)
        if res.returncode == 0:
            if strategy != "strict":
                print(f"    [PATCH] '{label}' appliqué via la stratégie '{strategy}'")
            return True

    # Dernier recours : l'outil Unix `patch`. Absent sur Windows -> on saute
    # proprement au lieu de lever un FileNotFoundError.
    if _has_command("patch"):
        res = _run(["patch", "-p1", "-i", str(patch_file)], cwd=repo_dir, check=False)
        if res.returncode == 0:
            print(f"    [PATCH] '{label}' appliqué via 'patch -p1'")
            return True
    else:
        print("    [PATCH] outil 'patch' indisponible (normal sur Windows) ; "
              "seules les stratégies git ont été tentées.")

    return False


def prepare_repo(instance: Instance, work_root: Path) -> Path:
    """Clone le repo, checkout le commit, applique le patch (gold ou agent) + test patch.

    Le patch appliqué est `instance.patch_to_apply` : le patch de l'agent s'il
    a été chargé depuis une soumission locale, sinon le gold patch. Le test
    patch est toujours appliqué (c'est lui qui amène les tests FAIL_TO_PASS).

    Robustesse : les patches d'agent NON-RESOLVED sont souvent mal formés. On
    tente plusieurs stratégies d'application (voir _apply_patch). Si le patch de
    CODE reste inapplicable, on lève PatchApplicationError : l'instance n'est
    pas exploitable (au lieu de planter avec une erreur obscure).
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
        # Normaliser en LF : évite les échecs 'git apply' dus aux CRLF Windows
        normalized = patch_text.replace("\r\n", "\n").replace("\r", "\n")
        patch_file.write_text(normalized, encoding="utf-8", newline="\n")

        ok = _apply_patch(patch_file, repo_dir, label)
        if not ok:
            if label == "code":
                raise PatchApplicationError(
                    f"Le patch de CODE de {instance.instance_id} est INAPPLICABLE "
                    f"(toutes les stratégies git ont échoué). C'est fréquent pour les "
                    f"patches d'agent non-resolved mal formés : cette instance n'est "
                    f"pas exploitable pour l'évaluation."
                )
            print("    [PATCH] ATTENTION : le test patch n'a pas pu être appliqué.")

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


def run_tests_traced(repo_dir: Path, test_ids: list[str], target_files: list[Path] = None) -> RunResult:
    """Exécute les tests donnés sous le tracer de variables.

    On utilise pytest en l'important programmatiquement et on injecte un
    plugin à la volée pour n'activer le tracer QUE pendant l'exécution
    du test (ignorant ainsi tout le bruit d'importation des modules).
    """
    watch_dir = _get_watch_dir(repo_dir)
    abs_target = None
    if target_files is not None:
        abs_target = {str((repo_dir / f).resolve()) for f in target_files}
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
) -> RunResult:
    """Wrapper : trace UN seul test (pendant local de
    run_single_test_traced_docker). Réutilise run_tests_traced avec une
    liste d'un seul élément."""
    return run_tests_traced(
        repo_dir=repo_dir,
        test_ids=[test_id],
        target_files=target_files,
    )
