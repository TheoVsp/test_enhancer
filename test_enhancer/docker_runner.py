"""
Custom Docker runner — construit une image par instance SWE-bench.

ce runner :
  1. Construit une image Docker locale  sweb.custom.<instance_id>
     - clone le repo à base_commit
     - détecte la version Python
     - installe les dépendances
  2. Exécute les tests dans un conteneur éphémère (--rm)
     - applique gold_patch + test_patch à runtime
     - injecte le tracer
     - retourne un RunResult identique à l'ancien runner

L'image est buildée une seule fois puis mise en cache par Docker.
Les patches sont toujours appliqués à runtime → pas besoin de rebuild.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import json
import tempfile
from pathlib import Path

from .dataset import Instance
from .swe_runner import RunResult
from .tracer import TraceRow, VariableTracer

# Répertoire du package — contient tracer.py, config.py, runner_inside.py
TRACER_INJECT_DIR = Path(__file__).parent

# Zone de transit locale pour docker cp (sous un chemin stable, pas C:\TEMP)
DOCKER_TMP_BASE = Path(__file__).parent.parent / "runs" / "_docker_tmp"

# Dockerfile template situé à la racine du package
DOCKERFILE_TEMPLATE = Path(__file__).parent / "Dockerfile.template"


# ── Helpers image ─────────────────────────────────────────────────────────────

def _image_name(instance_id: str) -> str:
    """Nom de l'image locale pour une instance donnée."""
    return f"sweb.custom.{instance_id.lower()}"


def _image_exists(image: str) -> bool:
    """Vérifie si l'image est déjà buildée localement."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    return result.returncode == 0

def _extra_env(instance: Instance) -> list[str]:
    """Returns docker run -e flags for repos that need special env vars."""
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

    Args:
        instance:      l'instance SWE-bench à préparer
        force_rebuild: si True, rebuilde même si l'image existe déjà

    Returns:
        True si l'image est disponible (déjà présente ou build réussi).
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

    print(f"    [BUILD] construction de {image} ...", file=sys.stderr, flush=True)
    print(f"    [BUILD] repo={instance.repo}  commit={instance.base_commit[:12]}",
          file=sys.stderr, flush=True)

    with tempfile.TemporaryDirectory() as ctx:
        ctx_path = Path(ctx)
        # Copier le template dans un contexte Docker temporaire
        dockerfile = ctx_path / "Dockerfile"
        shutil.copy(DOCKERFILE_TEMPLATE, dockerfile)

        cmd = [
            "docker", "build",

            "--build-arg", f"REPO={instance.repo}",
            "--build-arg", f"BASE_COMMIT={instance.base_commit}",
            "-t", image,
            str(ctx_path),
        ]
        print(f"the cms message:{cmd}", file=sys.stderr, flush= True)
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')

    if result.returncode != 0:
        print(f"    [BUILD] ÉCHEC (code {result.returncode})", file=sys.stderr, flush=True)
        print(f"    [BUILD] stderr={result.stderr[-800:]}", file=sys.stderr, flush=True)
        return False

    print(f"    [BUILD] image {image} construite avec succès.", file=sys.stderr, flush=True)
    return True


# ── Run ───────────────────────────────────────────────────────────────────────

def run_tests_traced_docker(
    instance: Instance,
    test_ids: list[str],
    force_rebuild: bool = False,
    target_files: list[Path] = None
) -> RunResult:
    """
    Exécute les tests dans un conteneur Docker local.

    IMPORTANT (Windows + WSL2) : on n'utilise PAS de montage de volume (-v),
    car les dossiers créés par tempfile ont des permissions que Docker Desktop
    sous WSL2 ne peut pas traverser ("Accès refusé" / returncode 125).
    À la place, on copie les fichiers DANS le conteneur avec `docker cp`, on
    exécute avec `docker exec`, puis on récupère le résultat avec `docker cp`.

    Interface identique à l'ancienne version, donc run_pipeline est inchangé.
    """
    image = _image_name(instance.instance_id)
    package_name = instance.repo.split("/")[1]          # "sympy/sympy" -> "sympy"
    watch_dir_in_container = f"/repo/{package_name}"

    print(f"    [DOCKER] image={image}", file=sys.stderr, flush=True)
    print(f"    [DOCKER] watch_dir={watch_dir_in_container}", file=sys.stderr, flush=True)
    print(f"    [DOCKER] target_files={target_files}", file=sys.stderr, flush=True)

    # 1. S'assurer que l'image est disponible (build si nécessaire)
    if not build_image(instance, force_rebuild=force_rebuild):
        return RunResult(
            success=False,
            repo_dir=Path("/repo"),
            tracer=None,
            stdout="",
            stderr=f"Impossible de construire l'image Docker : {image}",
        )

    container_name = f"te_run_{instance.instance_id.lower()}"
    rows = []
    stdout = ""
    stderr = ""
    success = False

    # Dossier temp local (sert juste de zone de transit pour docker cp).
    DOCKER_TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(DOCKER_TMP_BASE)) as tmp:
        tmp_path = Path(tmp)

        # 2. Préparer les fichiers à copier dans le conteneur
        (tmp_path / "gold.patch").write_text(instance.gold_patch, encoding="utf-8")
        (tmp_path / "test.patch").write_text(instance.test_patch, encoding="utf-8")
        shutil.copy(TRACER_INJECT_DIR / "tracer.py",        tmp_path / "tracer.py")
        shutil.copy(TRACER_INJECT_DIR / "config.py",        tmp_path / "config.py")
        shutil.copy(TRACER_INJECT_DIR / "runner_inside.py", tmp_path / "runner_inside.py")

        # 3. Construire la commande shell (chemins /tracer_inject -> /tmp/tracer_inject)
        patch_cmd = " && ".join([
            "cd /repo",
            "git apply /tmp/tracer_inject/gold.patch --ignore-whitespace "
            "|| git apply /tmp/tracer_inject/gold.patch --reject "
            "|| patch -p1 --ignore-whitespace < /tmp/tracer_inject/gold.patch",
            "git apply /tmp/tracer_inject/test.patch --ignore-whitespace "
            "|| git apply /tmp/tracer_inject/test.patch --reject "
            "|| patch -p1 --ignore-whitespace < /tmp/tracer_inject/test.patch",
        ])

        quoted_ids = " ".join(f"'{tid}'" for tid in test_ids)
        target_args = ""
        if target_files:
            abs_targets = [f"/repo/{tf}" for tf in target_files]
            quoted_targets = " ".join(f"'{t}'" for t in abs_targets)
            target_args = f" --target-files {quoted_targets}"
        runner_cmd = (
            f"python /tmp/tracer_inject/runner_inside.py "
            f"{watch_dir_in_container} "
            f"/tmp/tracer_inject/trace_rows.json "
            + quoted_ids
            + target_args
        )
        full_cmd = f"{patch_cmd} && {runner_cmd}"

        try:
            # 4. Démarrer un conteneur persistant (pas de --rm, pas de -v)
            #    On le garde vivant avec `sleep infinity` pour pouvoir cp/exec.
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True,
            )  # nettoyage d'un éventuel conteneur résiduel
            start = subprocess.run(
                ["docker", "run", "-d", "--name", container_name,
                 *_extra_env(instance), image, "sleep", "infinity"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if start.returncode != 0:
                print(f"    [DOCKER] échec démarrage conteneur : {start.stderr[:500]}",
                      file=sys.stderr, flush=True)
                raise RuntimeError("docker run -d failed")

            # 5. Copier les fichiers dans le conteneur (sous /tmp/tracer_inject)
            subprocess.run(
                ["docker", "exec", container_name, "mkdir", "-p", "/tmp/tracer_inject"],
                capture_output=True, text=True,
            )
            for fname in ("gold.patch", "test.patch", "tracer.py", "config.py",
                          "runner_inside.py"):
                subprocess.run(
                    ["docker", "cp", str(tmp_path / fname),
                     f"{container_name}:/tmp/tracer_inject/{fname}"],
                    capture_output=True, text=True,
                )

            # 6. Exécuter la commande dans le conteneur
            print(f"    [DOCKER] exécution dans le conteneur...", file=sys.stderr, flush=True)
            run = subprocess.run(
                ["docker", "exec", container_name, "/bin/bash", "-c", full_cmd],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            stdout, stderr = run.stdout, run.stderr
            success = run.returncode == 0
            print(f"    [DOCKER] returncode={run.returncode}", file=sys.stderr, flush=True)
            if run.returncode != 0:
                print(f"    [DOCKER] stderr={run.stderr[:500]}", file=sys.stderr, flush=True)

            # 7. Récupérer trace_rows.json du conteneur vers l'hôte
            out_json = tmp_path / "trace_rows.json"
            subprocess.run(
                ["docker", "cp",
                 f"{container_name}:/tmp/tracer_inject/trace_rows.json",
                 str(out_json)],
                capture_output=True, text=True,
            )
            try:
                rows_data = json.loads(out_json.read_text(encoding="utf-8"))
                rows = [TraceRow(**r) for r in rows_data]
            except Exception as exc:
                print(f"    [DOCKER] lecture trace_rows.json échouée : {exc}",
                      file=sys.stderr, flush=True)
                rows = []
        finally:
            # 8. Toujours nettoyer le conteneur
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True,
            )

    print(f"    [DOCKER] trace_rows={len(rows)}", file=sys.stderr, flush=True)
    if rows:
        print(f"    [DOCKER] first_row={rows[0]}", file=sys.stderr, flush=True)

    tracer = VariableTracer(watch_dir=watch_dir_in_container, target_files=target_files)
    tracer.rows = rows

    return RunResult(
        success=success,
        repo_dir=Path("/repo"),
        tracer=tracer,
        stdout=stdout,
        stderr=stderr,
    )
