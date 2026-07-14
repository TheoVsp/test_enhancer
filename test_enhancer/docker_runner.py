"""
Custom Docker runner — 3-layer image hierarchy à la SWE-bench.

  sweb.base                                  (built once, shared by everything)
    -> sweb.env.<repo>.<version>.<spec_hash>  (shared across instances of the
                                                same repo+version; tag embeds a
                                                hash of the spec so it rebuilds
                                                automatically when deps change)
      -> sweb.eval.<instance_id>              (repo cloned + pinned to
                                                base_commit, one per instance)

  Runtime flow (unchanged from before):
    1. Build the 3 layers above as needed (cheap no-ops if already cached)
    2. Start a persistent container from sweb.eval.<instance_id>
    3. docker cp the tracer + gold/test patches in, apply patches at runtime
    4. docker exec the traced test run
    5. docker cp trace_rows.json back out
    6. Remove the container

  Patches are always applied at runtime -> no rebuild needed between runs of
  the same instance.

  IMPORTANT (Windows + WSL2): no -v volume mounts anywhere, including at
  build time implicitly via bind-mounted context — build contexts here are
  tiny temp dirs holding only a Dockerfile, so this is unaffected. Runtime
  file transfer still goes through `docker cp`, exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import os
from pathlib import Path

from .dataset import Instance
from .swe_runner import RunResult
from .swe_specs import get_spec
from .tracer import TraceRow, VariableTracer

# Répertoire du package — contient tracer.py, config.py, runner_inside.py
TRACER_INJECT_DIR = Path(__file__).parent

# Dockerfile templates, un par couche
DOCKERFILE_BASE = TRACER_INJECT_DIR / "Dockerfile.base"
DOCKERFILE_ENV  = TRACER_INJECT_DIR / "Dockerfile.env"
DOCKERFILE_EVAL = TRACER_INJECT_DIR / "Dockerfile.eval"

BASE_IMAGE = "sweb.base:latest"

# ── Django instance ──────────────────────────────────────────────────────
def _is_django(instance: Instance) -> bool:
    return instance.repo == "django/django"

_DJANGO_UNITTEST_RE = re.compile(r"^(?P<method>\w+)\s+\((?P<path>[\w\.]+)\)$")


def _to_django_label(raw_id: str) -> str:
    """'test_id (app.tests.TestClass)' -> 'app.tests.TestClass.test_id'.
     Converts it into the label format tests/runtests.py expects on argv.
    """
    m = _DJANGO_UNITTEST_RE.match(raw_id.strip())
    if not m:
        return raw_id.strip()  # already dotted/label form, leave as-is
    return f"{m.group('path')}.{m.group('method')}"

# ── Helpers génériques ──────────────────────────────────────────────────────

def _slug(text: str) -> str:
    """Nom Docker-safe : minuscules, seuls [a-z0-9._-] autorisés."""
    return re.sub(r"[^a-z0-9._-]+", "_", text.lower()).strip("_")


def _spec_hash(spec: dict) -> str:
    """Hash court des champs qui influencent la couche env (pas eval)."""
    relevant = {
        "python": spec.get("python", "3.11"),
        "packages": spec.get("packages", ""),
        "pip_packages": sorted(spec.get("pip_packages", [])),
    }
    blob = json.dumps(relevant, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def _env_image_name(repo: str, version: str, spec: dict) -> str:
    return f"sweb.env.{_slug(repo)}.{_slug(version)}.{_spec_hash(spec)}"


def _eval_image_name(instance_id: str) -> str:
    return f"sweb.eval.{_slug(instance_id)}"


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
    return env


def _docker_build(dockerfile: Path, tag: str, build_args: dict[str, str],
                   log_prefix: str) -> bool:
    """Construit une image à partir d'un unique Dockerfile dans un contexte
    temporaire minimal (le Dockerfile lui-même — pas de fichiers repo)."""
    if not dockerfile.exists():
        print(f"    [BUILD] {log_prefix} Dockerfile introuvable : {dockerfile}",
              file=sys.stderr, flush=True)
        return False

    with tempfile.TemporaryDirectory() as ctx:
        ctx_path = Path(ctx)
        shutil.copy(dockerfile, ctx_path / "Dockerfile")

        cmd = ["docker", "build"]
        for key, val in build_args.items():
            cmd += ["--build-arg", f"{key}={val}"]
        cmd += ["-t", tag, str(ctx_path)]

        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    if result.returncode != 0:
        runs_path= Path("runs/docker_logs")
        runs_path.mkdir(parents=True, exist_ok=True)
        stdout_dir= runs_path /"docker_build_stdout.log"
        stderr_dir= runs_path /"docker_build_stderr.log"
        (stdout_dir).write_text(
    result.stdout,
    encoding="utf-8",
)
        (stderr_dir).write_text(
    result.stderr,
    encoding="utf-8",
)
        print(f"    [BUILD] {log_prefix} ÉCHEC (code {result.returncode})",
              file=sys.stderr, flush=True)
        #print(f"    [BUILD] {log_prefix} stdout (last 3000):\n{result.stdout[-3000:]}",
        #      file=sys.stderr, flush=True)
        #print(f"    [BUILD] {log_prefix} stderr (last 3000):\n{result.stderr[-3000:]}",
         #     file=sys.stderr, flush=True)
        return False

    print(f"    [BUILD] {log_prefix} {tag} construite avec succès.",
          file=sys.stderr, flush=True)
    return True


# ── Layer 1 : base ───────────────────────────────────────────────────────────

def build_base_image(force_rebuild: bool = True) -> bool:
    """Construit sweb.base une seule fois (Ubuntu + Miniconda + apt deps).
    Partagée par toutes les images env, jamais reconstruite sauf demande
    explicite."""
    if not force_rebuild and _image_exists(BASE_IMAGE):
        print(f"    [BUILD] base {BASE_IMAGE} déjà présente, skip build.",
              file=sys.stderr, flush=True)
        return True

    print("    [BUILD] construction de la couche base...", file=sys.stderr, flush=True)
    return _docker_build(DOCKERFILE_BASE, BASE_IMAGE, {}, "[base]")


# ── Layer 2 : env (partagée par repo+version) ────────────────────────────────

def build_env_image(instance: Instance, spec: dict, force_rebuild: bool ) -> str | None:
    """Construit (ou réutilise) sweb.env.<repo>.<version>.<hash>.

    Le tag encode un hash de (python, packages, pip_packages) : si le spec
    change dans swe_specs.py, le tag change, donc l'ancienne image reste en
    cache (inoffensive) et une nouvelle est construite automatiquement — pas
    besoin de force_rebuild pour ça, seulement pour re-forcer un tag existant.
    """
    tag = _env_image_name(instance.repo, instance.version, spec)

    if not force_rebuild and _image_exists(tag):
        print(f"    [BUILD] env {tag} déjà présente, skip build.",
              file=sys.stderr, flush=True)
        return tag

    if not build_base_image():
        return None

    python_version = spec.get("python", "3.11")
    packages       = spec.get("packages", "")
    pip_packages   = spec.get("pip_packages", [])
    pip_packages_str = " ".join(f'"{p}"' for p in pip_packages) if pip_packages else ""

    print(f"    [BUILD] construction de {tag} ...", file=sys.stderr, flush=True)
    print(f"    [BUILD] repo={instance.repo}  version={instance.version}"
          f"  python={python_version}  packages={packages!r}",
          file=sys.stderr, flush=True)

    ok = _docker_build(
        DOCKERFILE_ENV, tag,
        {
            "BASE_IMAGE": BASE_IMAGE,
            "REPO": instance.repo,
            "ENV_SETUP_COMMIT": instance.environment_setup_commit,
            "PYTHON_VERSION": python_version,
            "PACKAGES": packages,
            "PIP_PACKAGES": pip_packages_str,
        },
        "[env]",
    )
    return tag if ok else None


# ── Layer 3 : eval (une par instance) ────────────────────────────────────────

def build_eval_image(instance: Instance, env_tag: str, spec: dict,
                      force_rebuild: bool ) -> str | None:
    """Construit (ou réutilise) sweb.eval.<instance_id> sur la base de
    env_tag : clone le repo, le pin à base_commit, exécute pre_install puis
    install."""
    tag = _eval_image_name(instance.instance_id)

    if not force_rebuild and _image_exists(tag):
        print(f"    [BUILD] eval {tag} déjà présente, skip build.",
              file=sys.stderr, flush=True)
        return tag

    install_cmd = spec.get("install", "python -m pip install -e .")
    pre_install = spec.get("pre_install", [])
    pre_install_str = " && ".join(pre_install) if pre_install else "true"

    print(f"    [BUILD] construction de {tag} ...", file=sys.stderr, flush=True)
    print(f"    [BUILD] base={env_tag}  base_commit={instance.base_commit}",
          file=sys.stderr, flush=True)
    print(f"    [BUILD] install_cmd={install_cmd}", file=sys.stderr, flush=True)

    ok = _docker_build(
        DOCKERFILE_EVAL, tag,
        {
            "BASE_IMAGE": env_tag,
            "REPO": instance.repo,
            "BASE_COMMIT": instance.base_commit,
            "PRE_INSTALL": pre_install_str,
            "INSTALL_CMD": install_cmd,
        },
        "[eval]",
    )
    return tag if ok else None


# ── Orchestration (API inchangée pour pipeline.py) ───────────────────────────

def build_image(instance: Instance, force_rebuild: bool ) -> bool:
    """Construit les 3 couches dans l'ordre pour une instance.

    force_rebuild ne force que la couche eval (la plus courante à vouloir
    reconstruire, ex. après un changement de install_cmd) ; base et env sont
    déjà auto-invalidées par leur clé de cache (nom d'image / hash de spec).
    """
    spec = get_spec(instance.repo, instance.version)

    if not build_base_image():
        return False

    env_tag = build_env_image(instance, spec, force_rebuild)
    if env_tag is None:
        return False

    eval_tag = build_eval_image(instance, env_tag, spec, force_rebuild=force_rebuild)
    return eval_tag is not None


# ── Run ───────────────────────────────────────────────────────────────────────

def run_tests_traced_docker(
    instance: Instance,
    test_ids: list[str],
    force_rebuild: bool ,
    target_files: list[Path] = None,
) -> RunResult:
    """
    Exécute les tests dans un conteneur Docker local, à partir de l'image
    sweb.eval.<instance_id> (construite par build_image() si besoin).

    IMPORTANT (Windows + WSL2) : on n'utilise PAS de montage de volume (-v),
    car les dossiers créés par tempfile ont des permissions que Docker Desktop
    sous WSL2 ne peut pas traverser ("Accès refusé" / returncode 125). À la
    place, on démarre un conteneur persistant, on copie les fichiers dedans
    avec `docker cp`, on exécute avec `docker exec`, puis on récupère
    trace_rows.json avec `docker cp`. Cette approche marche aussi sur Linux/Mac.
    """
    image = _eval_image_name(instance.instance_id)
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
        shutil.copy(TRACER_INJECT_DIR / "sitecustomize.py", tmp_path / "sitecustomize.py")
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
            "/opt/conda/envs/testbed/bin/python "
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
        if _is_django(instance):
            labels=[_to_django_label(id) for id in test_ids]
            quoted_labels="".join(f"'{l}'" for l in labels)
            target_files_env=(
                os.pathsep.join(f"/repo/{tf}" for tf in target_files) 
                                if target_files else ""
            )
            runner_cmd = (
                find_python + " && "
                "export "
                f"TE_WATCH_DIR={watch_dir_in_container} "
                "TE_TRACE_OUT=/tmp/tracer_inject/trace_rows.json "
                f"TE_TARGET_FILES='{target_files_env}' "
                "PYTHONPATH=/tmp/tracer_inject:${PYTHONPATH} && "
                f"$PYTHON tests/runtests.py --settings=test_sqlite "
                f"--parallel 1 --verbosity 2 {quoted_labels}"
            )
        else:     
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
                          "runner_inside.py", "sitecustomize.py"):
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