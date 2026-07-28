"""
coverage_probe.py — Mesure la branch coverage de la suite de tests,
restreinte aux fichiers modifiés par le patch.

C'est la "weakness map" objective : au lieu de laisser le LLM DEVINER les
trous de couverture depuis la trace, on les MESURE avec coverage.py.

Utilisable de deux façons :
  1. En standalone :  python coverage_probe.py <instance_id>
     -> mesure baseline, puis (si runs/<id>/enhanced_tests*.py existe)
        mesure enhanced et affiche le DELTA.
  2. Importé par le pipeline (mesure sur un repo déjà préparé) :
        measure_on_prepared_repo(...) + render_weakness_map(...)

Sorties standalone :
    runs/<instance_id>/coverage_baseline.json
    runs/<instance_id>/coverage_enhanced.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from test_enhancer.dataset import get_instance
from test_enhancer import swe_runner


def _run(cmd: list[str], cwd: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _patched_paths(patch_text: str) -> list[str]:
    """Chemins des fichiers modifiés par un diff (lignes '+++ b/...')."""
    paths = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[len("+++ b/"):].strip())
    return paths


def prepare_repo_state(repo_dir: Path, base_commit: str,
                       code_patch: str, test_patch: str) -> None:
    """Remet le repo à base_commit + patch code + patch tests (même logique
    que evaluate_kill.prepare)."""
    _run(["git", "checkout", "-f", base_commit], repo_dir)
    _run(["git", "clean", "-fdx"], repo_dir)
    for label, txt in [("code", code_patch), ("test", test_patch)]:
        if not txt.strip():
            continue
        pf = (repo_dir / f"_covprobe_{label}.patch").resolve()
        pf.write_text(txt, encoding="utf-8")
        r = _run(["git", "apply", "--verbose", str(pf)], repo_dir)
        if r.returncode != 0:
            raise RuntimeError(f"patch {label} inapplicable:\n{r.stderr}")


# ---------------------------------------------------------------------------
# Coeur : mesure sur un repo DÉJÀ dans le bon état (utilisé par le pipeline)
# ---------------------------------------------------------------------------

def measure_on_prepared_repo(repo_dir: Path, node_ids: list[str],
                             patched_paths: list[str],
                             extra_node: list[str] | None = None,
                             verbose: bool = True) -> dict:
    """Exécute node_ids (+ extra_node) sous coverage --branch et renvoie
    la weakness map restreinte aux fichiers patchés.

    Le repo n'est PAS modifié (à part le fichier .coverage_te temporaire).
    """
    extra_node = extra_node or []

    cov_file = str((repo_dir / ".coverage_te").resolve())
    env = {"COVERAGE_FILE": cov_file}
    if verbose:
        print(f"    coverage run --branch sur {len(node_ids) + len(extra_node)} "
              f"test(s)...")
    r = _run([sys.executable, "-m", "coverage", "run", "--branch",
              "-m", "pytest", "-q", *node_ids, *extra_node], repo_dir, env)
    tail = "\n".join(r.stdout.strip().splitlines()[-2:])
    if verbose:
        print(f"    pytest: {tail}")
    if not Path(cov_file).exists():
        raise RuntimeError(
            f"{cov_file} jamais créé — coverage n'a pas tourné.\n"
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")

    json_out = (repo_dir / "_covprobe.json").resolve()
    r = _run([sys.executable, "-m", "coverage", "json", "-o", str(json_out)],
             repo_dir, env)
    if not json_out.exists():
        raise RuntimeError(f"coverage json a échoué :\n{r.stdout}\n{r.stderr}")
    data = json.loads(json_out.read_text(encoding="utf-8"))

    def matches_patched(covered_path: str) -> str | None:
        norm = Path(covered_path).as_posix()
        for p in patched_paths:
            if norm.endswith(Path(p).as_posix()):
                return p
        return None

    result = {"files": {}}
    for fname, fdata in data.get("files", {}).items():
        target = matches_patched(fname)
        if target is None:
            continue
        s = fdata["summary"]
        result["files"][target] = {
            "measured_path": fname,
            "percent_covered": round(s["percent_covered"], 1),
            "num_statements": s["num_statements"],
            "missing_lines": fdata.get("missing_lines", []),
            "missing_branches": fdata.get("missing_branches", []),
            "covered_branches": s.get("covered_branches"),
            "num_branches": s.get("num_branches"),
        }
    return result


def measure_on_prepared_repo_docker(
    container_name: str,
    docker_runner_module,
    node_ids: list[str],
    patched_paths: list[str],
    extra_test_local_path: Path | None = None,
    extra_node_rel: str | None = None,
    verbose: bool = True,
) -> dict:
    """Équivalent Docker de measure_on_prepared_repo : exécute `coverage run
    --branch -m pytest` DANS le conteneur persistant déjà patché (au lieu d'un
    repo_dir hôte), puis rapatrie le rapport JSON pour construire la weakness
    map.

    Args:
        container_name: nom du conteneur persistant déjà démarré et patché
            (voir docker_runner.start_persistent_container).
        docker_runner_module: le module docker_runner (injecté, comme dans
            validate.py, pour éviter les imports circulaires).
        node_ids: node ids pytest relatifs à /repo (FAIL_TO_PASS + PASS_TO_PASS).
        patched_paths: chemins (relatifs à /repo) des fichiers modifiés par le
            patch de code, pour restreindre le rapport de couverture.
        extra_test_local_path: fichier hôte contenant les tests déjà générés
            (combined_tests), à copier dans le conteneur avant de mesurer.
            None si aucun test généré pour l'instant (première itération).
        extra_node_rel: chemin (relatif à /repo) où placer extra_test_local_path
            dans le conteneur, et à inclure comme node id pytest supplémentaire.

    Returns:
        Le même format que measure_on_prepared_repo : {"files": {rel_path: {...}}}.
    """
    find_python = (
        "PYTHON=; for _py in /opt/conda/envs/testbed/bin/python python3; do "
        "command -v $_py >/dev/null 2>&1 && PYTHON=$_py && break; done"
    )
    cov_file_container = "/repo/.coverage_te"
    extra_node: list[str] = []

    # 1. Copier le fichier de tests supplémentaire (déjà généré) dans le
    #    conteneur, s'il y en a un.
    if extra_test_local_path is not None and extra_node_rel is not None:
        remote_dir = Path(extra_node_rel).parent.as_posix()
        docker_runner_module.exec_in_container(
            container_name, f"mkdir -p /repo/{remote_dir}")
        cp = subprocess.run(
            ["docker", "cp", str(extra_test_local_path),
             f"{container_name}:/repo/{extra_node_rel}"],
            capture_output=True, text=True,
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"docker cp du fichier de tests supplémentaire a échoué : {cp.stderr}")
        extra_node = [extra_node_rel]

    # 2. `coverage run --branch -m pytest ...` dans le conteneur.
    quoted_ids = " ".join(f"'{tid}'" for tid in [*node_ids, *extra_node])
    if verbose:
        print(f"    [DOCKER-COV] coverage run --branch sur "
              f"{len(node_ids) + len(extra_node)} test(s)...")
    run_cmd = (
        find_python + " && "
        f"COVERAGE_FILE={cov_file_container} $PYTHON -m coverage run --branch "
        f"-m pytest -q {quoted_ids}"
    )
    result = docker_runner_module.exec_in_container(container_name, run_cmd)
    if verbose:
        tail = "\n".join(result.stdout.strip().splitlines()[-2:])
        print(f"    [DOCKER-COV] pytest: {tail}")

    # 3. `coverage json` puis rapatriement du rapport vers l'hôte.
    json_cmd = (
        find_python + " && "
        f"COVERAGE_FILE={cov_file_container} $PYTHON -m coverage json "
        f"-o /repo/_covprobe.json"
    )
    json_result = docker_runner_module.exec_in_container(container_name, json_cmd)

    with tempfile.TemporaryDirectory() as tmp:
        local_json = Path(tmp) / "_covprobe.json"
        ok = docker_runner_module.copy_from_container(
            container_name, "/repo/_covprobe.json", local_json)
        if not ok or not local_json.exists():
            raise RuntimeError(
                "coverage json introuvable dans le conteneur — coverage n'a "
                f"pas tourné.\n--- pytest stdout ---\n{result.stdout}\n"
                f"--- pytest stderr ---\n{result.stderr}\n"
                f"--- coverage json stdout ---\n{json_result.stdout}\n"
                f"--- coverage json stderr ---\n{json_result.stderr}")
        data = json.loads(local_json.read_text(encoding="utf-8"))

    # Nettoyage best-effort dans le conteneur (ne doit pas faire échouer la mesure).
    docker_runner_module.exec_in_container(
        container_name, f"rm -f {cov_file_container} /repo/_covprobe.json")

    def matches_patched(covered_path: str) -> str | None:
        norm = Path(covered_path).as_posix()
        for p in patched_paths:
            if norm.endswith(Path(p).as_posix()):
                return p
        return None

    result_map: dict = {"files": {}}
    for fname, fdata in data.get("files", {}).items():
        target = matches_patched(fname)
        if target is None:
            continue
        s = fdata["summary"]
        result_map["files"][target] = {
            "measured_path": fname,
            "percent_covered": round(s["percent_covered"], 1),
            "num_statements": s["num_statements"],
            "missing_lines": fdata.get("missing_lines", []),
            "missing_branches": fdata.get("missing_branches", []),
            "covered_branches": s.get("covered_branches"),
            "num_branches": s.get("num_branches"),
        }
    return result_map


def render_weakness_map(result: dict, repo_dir: Path) -> str:
    """Rend la weakness map lisible pour un humain OU un LLM : chaque ligne /
    branche manquante est montrée AVEC son code source, pas juste son numéro.
    """
    out = []
    for fpath, e in result["files"].items():
        src_file = repo_dir / fpath
        try:
            src_lines = src_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            src_lines = []

        def src(n: int) -> str:
            if 1 <= n <= len(src_lines):
                return src_lines[n - 1].strip()
            return "?"

        out.append(f"File: {fpath}")
        out.append(f"Branch coverage measured on the existing suite: "
                   f"{e['percent_covered']}% "
                   f"({e['covered_branches']}/{e['num_branches']} branches)")

        if e["missing_lines"]:
            out.append("Lines NEVER executed by any existing test:")
            for n in e["missing_lines"]:
                out.append(f"  - line {n}: {src(n)}")
        else:
            out.append("Lines never executed: none")

        if e["missing_branches"]:
            out.append("Branch outcomes NEVER taken by any existing test:")
            for a, b in e["missing_branches"]:
                if b < 0:
                    out.append(f"  - line {a} ({src(a)}) exiting the function "
                               f"without entering the block")
                else:
                    out.append(f"  - line {a} ({src(a)}) -> line {b} ({src(b)})")
        else:
            out.append("Branch outcomes never taken: none")
        out.append("")
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Mode standalone (baseline + enhanced + delta)
# ---------------------------------------------------------------------------

def measure_coverage(instance_id: str, extra_test_file: Path | None = None,
                     tag: str = "baseline") -> dict:
    """Prépare le repo (base + gold + tests) puis mesure la coverage."""
    inst = get_instance(instance_id, use_agent_patch=False)
    repo_dir = Path(f"runs/_repos/{instance_id}").resolve()
    if not repo_dir.exists():
        sys.exit(f"[ERREUR] repo absent : {repo_dir} (lance d'abord le pipeline)")

    out_dir = Path(f"runs/{instance_id}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1] Préparation du repo (base + gold + tests)...")
    prepare_repo_state(repo_dir, inst.base_commit, inst.patch_to_apply,
                       inst.test_patch)

    f2p = swe_runner.resolve_node_ids(inst.fail_to_pass, inst.test_patch)
    p2p = swe_runner.resolve_node_ids(inst.pass_to_pass, inst.test_patch)
    node_ids = f2p + p2p
    print(f"[2] {len(f2p)} FAIL_TO_PASS + {len(p2p)} PASS_TO_PASS "
          f"= {len(node_ids)} test(s)")

    extra_node = []
    if extra_test_file is not None:
        f2p_file = f2p[0].split("::")[0]
        dest_rel = Path(f2p_file).parent / "test_te_enhanced.py"
        (repo_dir / dest_rel).write_text(
            extra_test_file.read_text(encoding="utf-8"), encoding="utf-8")
        extra_node = [str(dest_rel)]
        print(f"    + tests enrichis installés : {dest_rel}")

    paths = _patched_paths(inst.patch_to_apply)
    print(f"[3] Fichiers cibles : {paths}")
    print(f"[4] Exécution sous coverage (branch mode, tag={tag})...")

    result = measure_on_prepared_repo(repo_dir, node_ids, paths,
                                      extra_node=extra_node)
    result["instance_id"] = instance_id
    result["tag"] = tag

    if not result["files"]:
        sys.exit("[ATTENTION] aucun fichier patché trouvé dans le rapport.")

    out_file = out_dir / f"coverage_{tag}.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"\n===== WEAKNESS MAP ({tag}) — {instance_id} =====")
    print(render_weakness_map(result, repo_dir))
    print(f"\n  -> sauvegardé : {out_file}")
    return result


def _find_enhanced_tests(instance_id: str) -> Path | None:
    enh = Path(f"runs/{instance_id}/enhanced_tests.py")
    if enh.exists():
        return enh
    cands = sorted(Path(f"runs/{instance_id}").glob("enhanced_tests*.py"))
    return cands[0] if cands else None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python coverage_probe.py <instance_id>")
    iid = sys.argv[1]

    base = measure_coverage(iid, tag="baseline")

    enh = _find_enhanced_tests(iid)
    if enh is None:
        print("\n[i] pas de tests enrichis dans runs/ — delta non calculé")
        sys.exit(0)

    print(f"\n[+] Tests enrichis trouvés : {enh} — mesure après enrichissement")
    after = measure_coverage(iid, extra_test_file=enh, tag="enhanced")

    print(f"\n===== COVERAGE DELTA — {iid} =====")
    for f, b in base["files"].items():
        a = after["files"].get(f)
        if not a:
            continue
        closed = [br for br in b["missing_branches"]
                  if br not in a["missing_branches"]]
        print(f"  {f}")
        print(f"    branch coverage : {b['percent_covered']}% "
              f"-> {a['percent_covered']}%")
        print(f"    branches comblées  : {closed}")
        print(f"    branches restantes : {a['missing_branches']}")