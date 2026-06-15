"""
Préparation de l'environnement SWE-bench et exécution tracée des tests.
"""
from __future__ import annotations
print(f"LOADING swe_runner from {__file__}", flush=True)
import contextlib
import io
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ._hub import Instance
from .tracer import VariableTracer


@dataclass
class RunResult:
    success: bool
    repo_dir: Path
    tracer: VariableTracer | None
    stdout: str
    stderr: str


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
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
    work_root.mkdir(parents=True, exist_ok=True)
    repo_url = f"https://github.com/{instance.repo}.git"
    repo_dir = work_root / instance.instance_id

    if not repo_dir.exists():
        _run(["git", "clone", repo_url, str(repo_dir)])

    _run(["git", "checkout", "-f", instance.base_commit], cwd=repo_dir)
    _run(["git", "clean", "-fdx"], cwd=repo_dir, check=False)

    for label, patch_text in [("gold", instance.gold_patch), ("test", instance.test_patch)]:
        if not patch_text.strip():
            continue
        patch_file = repo_dir / f"_te_{label}.patch"
        patch_file.write_text(patch_text, encoding="utf-8")
        res = _run(["git", "apply", "--verbose", str(patch_file)], cwd=repo_dir, check=False)
        if res.returncode != 0:
            _run(["patch", "-p1", "-i", str(patch_file)], cwd=repo_dir, check=False)

    return repo_dir


def install_repo(repo_dir: Path) -> None:
    _run([sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
         cwd=repo_dir, check=False)


def extract_test_files(test_patch: str) -> list[str]:
    files: list[str] = []
    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            if path.endswith(".py") and path not in files:
                files.append(path)
    return files


def _normalize_test_id(tid: str) -> str:
    """Strip unittest-style class suffix: 'test_foo (pkg.TestClass)' -> 'test_foo'."""
    import re
    m = re.match(r'^(\w+)\s+\(([^)]+)\)$', tid.strip())
    if m:
        return m.group(1)
    return tid


def resolve_node_ids(test_ids: list[str], test_patch: str) -> list[str]:
    test_files = extract_test_files(test_patch)
    resolved: list[str] = []
    for tid in test_ids:
        tid = _normalize_test_id(tid)
        if "::" in tid or tid.endswith(".py") or "/" in tid:
            resolved.append(tid)
            continue
        if test_files:
            for tf in test_files:
                resolved.append(f"{tf}::{tid}")
        else:
            resolved.append(tid)
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


def run_tests_traced(repo_dir: Path, test_ids: list[str], target_files:list[str] | None =None) -> RunResult:
    """Exécute les tests donnés sous le tracer de variables."""
    watch_dir = _get_watch_dir(repo_dir)
    abs_target= None
    if target_files:
        abs_target = {str((repo_dir / f).resolve()) for f in target_files}
    tracer = VariableTracer(watch_dir=watch_dir, target_files=abs_target)

    print(f"    [DEBUG] watch_dir={watch_dir}", file=sys.stderr, flush=True)

    pytest_args = ["-x", "-q", "--no-header", "--tb=short", *test_ids]
    out_buf, err_buf = io.StringIO(), io.StringIO()
    success = False

    try:
        import pytest

        class TracerPlugin:
            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_call(self, item):
                # Reset in case the tracer is reused across multiple tests
                tracer.reset()
                # Start as late as possible — after pytest's own sys.settrace
                # calls (assert rewriting, coverage) have already fired
                tracer.start()
                try:
                    yield
                finally:
                    tracer.stop()

        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            old_cwd = os.getcwd()
            os.chdir(repo_dir)
            try:
                ret = pytest.main(pytest_args, plugins=[TracerPlugin()])
            finally:
                os.chdir(old_cwd)

        success = (ret == 0)
    except Exception as exc:
        err_buf.write(f"\nException pendant l'exécution: {exc}")

    print(f"    [DEBUG] trace_rows={len(tracer.rows)}", file=sys.stderr, flush=True)
    if tracer.rows:
        print(f"    [DEBUG] first row={tracer.rows[0]}", file=sys.stderr, flush=True)

    return RunResult(
        success=success,
        repo_dir=repo_dir,
        tracer=tracer,
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
    )