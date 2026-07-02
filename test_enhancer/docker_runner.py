"""
Custom Docker runner — construit une image par instance SWE-bench.

  1. Construit une image Docker locale  sweb.custom.<instance_id>
     - utilise les specs SWE-bench (swe_specs.get_spec) pour connaître
       la version Python exacte et les dépendances de chaque instance
  2. Exécute les tests dans un conteneur (via docker cp + docker exec)
     - applique gold_patch + test_patch à runtime
     - injecte le tracer
     - retourne un RunResult identique à l'ancien runner

L'image est buildée une seule fois puis mise en cache par Docker.
Les patches sont toujours appliqués à runtime → pas besoin de rebuild.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .dataset import Instance
from .swe_runner import RunResult
from .swe_specs import get_spec
from .tracer import TraceRow, VariableTracer

# Répertoire du package — contient tracer.py, config.py, runner_inside.py
TRACER_INJECT_DIR = Path(__file__).parent

# Dockerfile template situé à la racine du package
DOCKERFILE_TEMPLATE = Path(__file__).parent / "Dockerfile.template"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _image_name(instance_id: str) -> str:
    return f"sweb.custom.{instance_id.lower()}"


def _image_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _unix_text(text: str) -> str:
    """Normalise les fins de ligne en LF pur (évite les erreurs git apply sur Windows)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extra_env(instance: Instance) -> list[str]:
    """Returns docker run flags for repos that need special env vars."""
    env = []
    if instance.repo == "django/django":
        env += ["-w", "/repo"]
        env += ["-e", "PYTHONPATH=/repo"]
        env += ["-e", "DJANGO_SETTINGS_MODULE=tests.test_sqlite"]
    return env


# ── Build ─────────────────────────────────────────────────────────────────────

def build_image(instance: Instance, force_rebuild: bool = False) -> bool:
    """
    Construit l'image Docker pour une instance si elle n'existe pas encore.
    Utilise swe_specs.get_spec(repo, version) pour obtenir :
      - la version Python exacte
      - les pip_packages à pré-installer
      - la commande d'installation éditable
      - les pre_install commands (apt-get, sed …)
    """
    image = _image_name(instance.instance_id)

    if not force_rebuild and _image_exists(image):
        print(f"    [BUILD] image {image} déjà présente, skip build.",
              file=sys.stderr, flush=True)
        return True

    if not DOCKERFILE_TEMPLATE.exists():
        print(f"    [BUILD] Dockerfile.template introuvable : {DOCKERFILE_TEMPLATE}",
              file=sys.stderr, flush=True)
        return False

    # Récupérer le spec SWE-bench pour ce repo+version
    spec = get_spec(instance.repo, instance.version)
    python_version  = spec.get("python", "3.11")
    pip_packages    = spec.get("pip_packages", [])
    install_cmd     = spec.get("install", "python -m pip install -e .")
    pre_install     = spec.get("pre_install", [])

    # Sérialiser pip_packages et pre_install en chaînes séparées par des espaces/newlines
    pip_packages_str = " ".join(f'"{p}"' for p in pip_packages) if pip_packages else ""
    pre_install_str  = " && ".join(pre_install) if pre_install else "true"

    print(f"    [BUILD] construction de {image} ...", file=sys.stderr, flush=True)
    print(f"    [BUILD] repo={instance.repo}  version={instance.version}"
          f"  python={python_version}", file=sys.stderr, flush=True)
    print(f"    [BUILD] install_cmd={install_cmd}", file=sys.stderr, flush=True)
    print(f"    [BUILD] pip_packages={pip_packages_str[:120]}...", file=sys.stderr, flush=True)

    with tempfile.TemporaryDirectory() as ctx:
        ctx_path = Path(ctx)
        shutil.copy(DOCKERFILE_TEMPLATE, ctx_path / "Dockerfile")

        cmd = [
            "docker", "build",
            "--build-arg", f"REPO={instance.repo}",
            "--build-arg", f"BASE_COMMIT={instance.base_commit}",
            "--build-arg", f"ENV_SETUP_COMMIT={instance.environment_setup_commit}",
            "--build-arg", f"PYTHON_VERSION={python_version}",
            "--build-arg", f"PIP_PACKAGES={pip_packages_str}",
            "--build-arg", f"INSTALL_CMD={install_cmd}",
            "--build-arg", f"PRE_INSTALL={pre_install_str}",
            "-t", image,
            str(ctx_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    if result.returncode != 0:
        print(f"    [BUILD] ÉCHEC (code {result.returncode})", file=sys.stderr, flush=True)
        print(f"    [BUILD] stdout (last 3000):\n{result.stdout[-3000:]}",
              file=sys.stderr, flush=True)
        print(f"    [BUILD] stderr (last 3000):\n{result.stderr[-3000:]}",
              file=sys.stderr, flush=True)
        return False

    print(f"    [BUILD] image {image} construite avec succès.", file=sys.stderr, flush=True)
    return True


# ── Run ───────────────────────────────────────────────────────────────────────

def run_tests_traced_docker(
    instance: Instance,
    test_ids: list[str],
    force_rebuild: bool = False,
    target_files: list[Path] = None,
) -> RunResult:
    """
    Exécute les tests dans un conteneur Docker local.

    IMPORTANT (Windows + WSL2) : on n'utilise PAS de montage de volume (-v),
    car les dossiers créés par tempfile ont des permissions que Docker Desktop
    sous WSL2 ne peut pas traverser ("Accès refusé" / returncode 125). À la
    place, on démarre un conteneur persistant, on copie les fichiers dedans
    avec `docker cp`, on exécute avec `docker exec`, puis on récupère
    trace_rows.json avec `docker cp`. Cette approche marche aussi sur Linux/Mac.
    """
    image = _image_name(instance.instance_id)
    package_name = instance.repo.split("/")[1]
    watch_dir_in_container = f"/repo/{package_name}"

    print(f"    [DOCKER] image={image}", file=sys.stderr, flush=True)
    print(f"    [DOCKER] watch_dir={watch_dir_in_container}", file=sys.stderr, flush=True)
    print(f"    [DOCKER] target_files={target_files}", file=sys.stderr, flush=True)

    if not build_image(instance, force_rebuild=force_rebuild):
        return RunResult(
            success=False, repo_dir=Path("/repo"), tracer=None,
            stdout="",
            stderr=f"Impossible de construire l'image Docker : {image}",
        )

    container_name = f"te_run_{instance.instance_id.lower()}"

    # Zone de transit locale stable
    docker_tmp = TRACER_INJECT_DIR.parent / "runs" / "_docker_tmp"
    docker_tmp.mkdir(parents=True, exist_ok=True)

    rows = []
    result = None

    with tempfile.TemporaryDirectory(dir=str(docker_tmp)) as tmp:
        tmp_path = Path(tmp)

        # 2. Écrire les patches (LF seulement — évite les erreurs git apply Windows)
        (tmp_path / "gold.patch").write_text(
            _unix_text(instance.gold_patch), encoding="utf-8", newline="\n")
        (tmp_path / "test.patch").write_text(
            _unix_text(instance.test_patch), encoding="utf-8", newline="\n")

        # Copier le tracer et ses dépendances
        shutil.copy(TRACER_INJECT_DIR / "tracer.py",        tmp_path / "tracer.py")
        shutil.copy(TRACER_INJECT_DIR / "config.py",        tmp_path / "config.py")
        shutil.copy(TRACER_INJECT_DIR / "runner_inside.py", tmp_path / "runner_inside.py")

        # 3. Commande shell dans le conteneur (chemins /tracer_inject -> /tmp/tracer_inject)
        def _apply(fname: str) -> str:
            return (
                f"git apply /tmp/tracer_inject/{fname} --ignore-whitespace "
                f"|| git apply /tmp/tracer_inject/{fname} --reject "
                f"|| patch -p1 -f --ignore-whitespace < /tmp/tracer_inject/{fname}"
            )

        patch_cmd = " && ".join([
            "cd /repo",
            "echo HEAD=$(git rev-parse HEAD)",
            _apply("gold.patch"),
            _apply("test.patch"),
        ])

        find_python = (
            "PYTHON=; "
            "for _py in "
            "/opt/conda/envs/testenv/bin/python "
            "python3.13 python3.12 python3.11 python3.10 python3.9 "
            "python3.8 python3.7 python3.6 python3 python; do "
            "  if command -v $_py >/dev/null 2>&1 && "
            '     $_py -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" '
            "     >/dev/null 2>&1; then "
            "    PYTHON=$_py; break; "
            "  fi; "
            "done; "
            'if [ -z "$PYTHON" ]; then echo "[runner] No Python 3 found" >&2; exit 1; fi; '
            'echo "[runner] using $PYTHON" >&2'
        )

        quoted_ids = " ".join(f"'{tid}'" for tid in test_ids)
        target_args = ""
        if target_files:
            abs_targets   = [f"/repo/{tf}" for tf in target_files]
            quoted_targets = " ".join(f"'{t}'" for t in abs_targets)
            target_args    = f" --target-files {quoted_targets}"

        runner_cmd = (
            find_python + " && "
            "$PYTHON /tmp/tracer_inject/runner_inside.py "
            f"{watch_dir_in_container} "
            f"/tmp/tracer_inject/trace_rows.json "
            + quoted_ids
            + target_args
        )

        full_cmd = f"{patch_cmd} && {runner_cmd}"

        def _drun(args):
            return subprocess.run(args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")

        try:
            # 4. Démarrer un conteneur persistant (pas de --rm, pas de -v)
            _drun(["docker", "rm", "-f", container_name])  # nettoyage résiduel
            start = _drun(["docker", "run", "-d", "--name", container_name,
                           *_extra_env(instance), image, "sleep", "infinity"])
            if start.returncode != 0:
                print(f"    [DOCKER] échec démarrage conteneur : {start.stderr[:500]}",
                      file=sys.stderr, flush=True)
                raise RuntimeError("docker run -d failed")

            # 5. Copier les fichiers dans le conteneur (sous /tmp/tracer_inject)
            _drun(["docker", "exec", container_name, "mkdir", "-p", "/tmp/tracer_inject"])
            for fname in ("gold.patch", "test.patch", "tracer.py", "config.py",
                          "runner_inside.py"):
                _drun(["docker", "cp", str(tmp_path / fname),
                       f"{container_name}:/tmp/tracer_inject/{fname}"])

            # 6. Exécuter la commande dans le conteneur
            print(f"    [DOCKER] exécution dans le conteneur...", file=sys.stderr, flush=True)
            result = _drun(["docker", "exec", container_name, "/bin/bash", "-c", full_cmd])
            print(f"    [DOCKER] returncode={result.returncode}", file=sys.stderr, flush=True)
            if result.returncode != 0:
                print(f"    [DOCKER] stderr={result.stderr[:800]}", file=sys.stderr, flush=True)

            # 7. Récupérer trace_rows.json du conteneur vers l'hôte
            out_json = tmp_path / "trace_rows.json"
            _drun(["docker", "cp",
                   f"{container_name}:/tmp/tracer_inject/trace_rows.json",
                   str(out_json)])
            try:
                rows_data = json.loads(out_json.read_text(encoding="utf-8"))
                rows = [TraceRow(**r) for r in rows_data]
            except Exception as exc:
                print(f"    [DOCKER] lecture trace_rows.json échouée : {exc}",
                      file=sys.stderr, flush=True)
                rows = []
        finally:
            # 8. Toujours nettoyer le conteneur
            _drun(["docker", "rm", "-f", container_name])

    print(f"    [DOCKER] trace_rows={len(rows)}", file=sys.stderr, flush=True)

    success = (result is not None) and (result.returncode == 0)
    tracer = VariableTracer(watch_dir=watch_dir_in_container, target_files=target_files)
    tracer.rows = rows

    return RunResult(
        success=success,
        repo_dir=Path("/repo"),
        tracer=tracer,
        stdout=result.stdout if result else "",
        stderr=result.stderr if result else "",
    )


def run_single_test_traced_docker(
    instance: Instance,
    test_id: str,
    force_rebuild: bool = False,
    target_files: list[Path] = None,
) -> RunResult:
    """Wrapper pour exécuter un seul test."""
    return run_tests_traced_docker(
        instance=instance,
        test_ids=[test_id],
        force_rebuild=force_rebuild,
        target_files=target_files,
    )
