"""
Custom Docker runner — construit une image par instance SWE-bench.

Contrairement au runner SWE-bench officiel qui pull des images pré-construites,
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

from ._hub import Instance
from .swe_runner import RunResult
from .tracer_standalone import TraceRow, VariableTracer

# Répertoire du package — contient tracer.py, config.py, runner_inside.py
TRACER_INJECT_DIR = Path(__file__).parent

# Dockerfile template situé à la racine du package
DOCKERFILE_TEMPLATE = Path(__file__).parent.parent / "test_enhancer\Dockerfile.template"


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

    Interface identique à l'ancien docker_runner.run_tests_traced_docker,
    donc run_pipeline n'a pas besoin d'être modifié.

    Args:
        instance:      instance SWE-bench à tester
        test_ids:      liste des tests à exécuter (FAIL_TO_PASS en général)
        force_rebuild: forcer la reconstruction de l'image même si elle existe

    Returns:
        RunResult avec tracer, stdout, stderr, success
    """
    image = _image_name(instance.instance_id)
    package_name = instance.repo.split("/")[1]          # "sympy/sympy" -> "sympy"
    watch_dir_in_container = f"/repo/{package_name}"

    print(f"    [DOCKER] image={image}", file=sys.stderr, flush=True)
    print(f"    [DOCKER] watch_dir={watch_dir_in_container}", file=sys.stderr, flush=True)
    print(f"   [DOCKER] target_files={target_files}", file=sys.stderr, flush=True)
    # 1. S'assurer que l'image est disponible (build si nécessaire)
    if not build_image(instance, force_rebuild=force_rebuild):
        return RunResult(
            success=False,
            repo_dir=Path("/repo"),
            tracer=None,
            stdout="",
            stderr=f"Impossible de construire l'image Docker : {image}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 2. Écrire les patches dans le volume temporaire
        (tmp_path / "gold.patch").write_text(instance.gold_patch, encoding="utf-8")
        (tmp_path / "test.patch").write_text(instance.test_patch, encoding="utf-8")

        # Fichier de sortie JSON (vide par défaut si le runner plante)
        output_file = tmp_path / "trace_rows.json"
        output_file.write_text("[]", encoding="utf-8")

        # Copier le tracer et ses dépendances dans le volume
        shutil.copy(TRACER_INJECT_DIR / "tracer_standalone.py",        tmp_path / "tracer_standalone.py")
        #shutil.copy(TRACER_INJECT_DIR / "config.py",        tmp_path / "config.py")
        shutil.copy(TRACER_INJECT_DIR / "runner_inside.py", tmp_path / "runner_inside.py")

        # 3. Commande shell à l'intérieur du conteneur
        #    Étape A : appliquer les patches
        #    Étape B : lancer runner_inside.py (pytest + tracer)
        patch_cmd = " && ".join([
            "cd /repo",
            # gold patch — 3 tentatives (git apply strict / avec rejet / patch GNU)
            "git apply /tracer_inject/gold.patch --ignore-whitespace "
            "|| git apply /tracer_inject/gold.patch --reject "
            "|| patch -p1 --ignore-whitespace < /tracer_inject/gold.patch",
            # test patch
            "git apply /tracer_inject/test.patch --ignore-whitespace "
            "|| git apply /tracer_inject/test.patch --reject "
            "|| patch -p1 --ignore-whitespace < /tracer_inject/test.patch",
        ])

        # Quote each test id so bash handles spaces/parentheses safely
        quoted_ids = " ".join(f"'{tid}'" for tid in test_ids)
        target_args= ""
        if target_files:
            abs_targets=[f"/repo/{tf}" for tf in target_files]
            quoted_targets= " ".join(f"'{t}'" for t in abs_targets)
            target_args= f" --target-files {quoted_targets}"
        runner_cmd = (
            f"python /tracer_inject/runner_inside.py "
            f"{watch_dir_in_container} "
            f"/output/trace_rows.json "
            + quoted_ids
            + target_args
        )

        full_cmd = f"{patch_cmd} && {runner_cmd}"

        cmd = [
            "docker", "run", "--rm",
            *_extra_env(instance),
            "-v", f"{tmp_path}:/output",
            "-v", f"{tmp_path}:/tracer_inject",
            image,
            "/bin/bash", "-c", full_cmd,
        ]

        print(f"    [DOCKER] démarrage du conteneur...", file=sys.stderr, flush=True)
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        print(f"    [DOCKER] returncode={result.returncode}", file=sys.stderr, flush=True)

        if result.returncode != 0:
            print(f"    [DOCKER] stderr={result.stderr[:500]}", file=sys.stderr, flush=True)

        success = result.returncode == 0

        # 4. Lire les trace rows produites par runner_inside.py
        try:
            rows_data = json.loads(output_file.read_text(encoding="utf-8"))
            rows = [TraceRow(**r) for r in rows_data]
        except Exception as exc:
            print(f"    [DOCKER] lecture trace_rows.json échouée : {exc}",
                  file=sys.stderr, flush=True)
            rows = []

    print(f"    [DOCKER] trace_rows={len(rows)}", file=sys.stderr, flush=True)
    if rows:
        print(f"    [DOCKER] first_row={rows[0]}", file=sys.stderr, flush=True)

    # Reconstruire un VariableTracer pour transporter les rows
    tracer = VariableTracer(watch_dir=watch_dir_in_container, target_files=target_files)
    tracer.rows = rows

    return RunResult(
        success=success,
        repo_dir=Path("/repo"),
        tracer=tracer,
        stdout=result.stdout,
        stderr=result.stderr,
    )