"""
coverage_probe.py — Mesure la branch coverage de la suite de tests,
restreinte aux fichiers modifiés par le patch, et SCOPÉE à la région patchée.

Weakness map objective : au lieu de laisser le LLM DEVINER les trous de
couverture, on les MESURE avec coverage.py. Et pour éviter de noyer le signal
dans les gros fichiers (ex. fu.py : 541 branches manquantes hors sujet), la
map est restreinte aux FONCTIONS touchées par le patch ; le reste du fichier
est résumé en une ligne.

Utilisable de deux façons :
  1. Standalone :  python coverage_probe.py <instance_id>
  2. Importé par le pipeline : measure_on_prepared_repo(...) +
     render_weakness_map(...)

Sorties standalone :
    runs/<instance_id>/coverage_baseline.json
    runs/<instance_id>/coverage_enhanced.json
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
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


# ---------------------------------------------------------------------------
# Scoping à la région patchée
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def patched_lines_by_file(patch_text: str) -> dict[str, set[int]]:
    """Lignes (numérotation du NOUVEAU fichier) touchées par chaque hunk."""
    result: dict[str, set[int]] = {}
    current: str | None = None
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):].strip()
            result.setdefault(current, set())
        elif current and (m := _HUNK_RE.match(line)):
            start = int(m.group(1))
            count = int(m.group(2) or "1")
            result[current].update(range(start, start + count))
    return result


def patched_regions(repo_dir: Path, patch_text: str) -> dict[str, list[list[int]]]:
    """Pour chaque fichier patché : les spans [start, end] des fonctions/
    méthodes contenant au moins une ligne touchée par le patch. Si une ligne
    touchée est hors de toute fonction (top-level), son hunk est inclus tel
    quel comme span."""
    regions: dict[str, list[list[int]]] = {}
    for fpath, touched in patched_lines_by_file(patch_text).items():
        src_file = repo_dir / fpath
        try:
            tree = ast.parse(src_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        spans: list[list[int]] = []

        def visit(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lo, hi = child.lineno, child.end_lineno or child.lineno
                    if any(lo <= n <= hi for n in touched):
                        spans.append([lo, hi])
                visit(child)

        visit(tree)
        covered = set()
        for lo, hi in spans:
            covered.update(range(lo, hi + 1))
        orphans = sorted(n for n in touched if n not in covered)
        if orphans:  # lignes top-level : regrouper les runs contigus
            start = prev = orphans[0]
            for n in orphans[1:]:
                if n != prev + 1:
                    spans.append([start, prev])
                    start = n
                prev = n
            spans.append([start, prev])
        regions[fpath] = sorted(spans)
    return regions


def _in_regions(line: int, spans: list[list[int]]) -> bool:
    return any(lo <= line <= hi for lo, hi in spans)


# ---------------------------------------------------------------------------
# Coeur : mesure sur un repo DÉJÀ dans le bon état (utilisé par le pipeline)
# ---------------------------------------------------------------------------

def measure_on_prepared_repo(repo_dir: Path, node_ids: list[str],
                             patched_paths: list[str],
                             extra_node: list[str] | None = None,
                             regions: dict[str, list[list[int]]] | None = None,
                             verbose: bool = True) -> dict:
    """Exécute node_ids (+ extra_node) sous coverage --branch et renvoie la
    weakness map des fichiers patchés. Si `regions` est fourni, chaque fichier
    porte en plus les gaps restreints à la région patchée (region_*)."""
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
    if not re.search(r"\d+ (passed|failed)", r.stdout):
        raise RuntimeError(
            "pytest n'a exécuté aucun test (pas de 'N passed/failed' dans la "
            "sortie) — la couverture mesurée serait celle de l'import, pas "
            "des tests. Dernières lignes pytest :\n"
            + "\n".join(r.stdout.strip().splitlines()[-15:]))    
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
        entry = {
            "measured_path": fname,
            "percent_covered": round(s["percent_covered"], 1),
            "num_statements": s["num_statements"],
            "missing_lines": fdata.get("missing_lines", []),
            "missing_branches": fdata.get("missing_branches", []),
            "covered_branches": s.get("covered_branches"),
            "num_branches": s.get("num_branches"),
        }
        spans = (regions or {}).get(target)
        if spans:
            entry["region_spans"] = spans
            entry["region_missing_lines"] = [
                n for n in entry["missing_lines"] if _in_regions(n, spans)]
            entry["region_missing_branches"] = [
                b for b in entry["missing_branches"] if _in_regions(b[0], spans)]
        result["files"][target] = entry
    return result


def render_weakness_map(result: dict, repo_dir: Path) -> str:
    """Rend la weakness map lisible pour un humain OU un LLM. Si la mesure
    est scopée (region_*), seuls les gaps de la région patchée sont détaillés ;
    le reste du fichier est résumé en une ligne."""
    out = []
    for fpath, e in result["files"].items():
        src_file = repo_dir / fpath
        try:
            src_lines = src_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            src_lines = []

        def src(n: int) -> str:
            return src_lines[n - 1].strip() if 1 <= n <= len(src_lines) else "?"

        scoped = "region_spans" in e
        miss_lines = e["region_missing_lines"] if scoped else e["missing_lines"]
        miss_branches = (e["region_missing_branches"] if scoped
                         else e["missing_branches"])

        out.append(f"File: {fpath}")
        out.append(f"Branch coverage measured on the existing suite: "
                   f"{e['percent_covered']}% "
                   f"({e['covered_branches']}/{e['num_branches']} branches, "
                   f"whole file)")
        if scoped:
            spans = ", ".join(f"{lo}-{hi}" for lo, hi in e["region_spans"])
            out.append(f"SCOPE: gaps below are restricted to the functions "
                       f"touched by the patch (lines {spans}). Target ONLY "
                       f"these.")

        if miss_lines:
            out.append("Lines NEVER executed by any existing test:")
            for n in miss_lines:
                out.append(f"  - line {n}: {src(n)}")
        else:
            out.append("Lines never executed (in scope): none")

        if miss_branches:
            out.append("Branch outcomes NEVER taken by any existing test:")
            for a, b in miss_branches:
                if b < 0:
                    out.append(f"  - line {a} ({src(a)}) exiting the function "
                               f"without entering the block")
                else:
                    out.append(f"  - line {a} ({src(a)}) -> line {b} ({src(b)})")
        else:
            out.append("Branch outcomes never taken (in scope): none")

        if scoped:
            rest = len(e["missing_branches"]) - len(miss_branches)
            if rest > 0:
                out.append(f"(Outside the patched region: {rest} more missing "
                           f"branch outcome(s) in this file — NOT targets.)")
        out.append("")
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Mode standalone (baseline + enhanced + delta)
# ---------------------------------------------------------------------------

def measure_coverage(instance_id: str, extra_test_file: Path | None = None,
                     tag: str = "baseline") -> dict:
    """Prépare le repo (base + gold + tests) puis mesure la coverage,
    scopée à la région patchée."""
    inst = get_instance(instance_id, use_agent_patch=False)
    repo_dir = Path(f"runs/_repos/{instance_id}").resolve()
    if not repo_dir.exists():
        sys.exit(f"[ERREUR] repo absent : {repo_dir} (lance d'abord le pipeline)")

    out_dir = Path(f"runs/{instance_id}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1] Préparation du repo (base + gold + tests)...")
    prepare_repo_state(repo_dir, inst.base_commit, inst.patch_to_apply,
                       inst.test_patch)

    f2p = swe_runner.resolve_node_ids(inst.fail_to_pass, inst.test_patch,
                                      repo_dir=repo_dir)
    p2p = swe_runner.resolve_node_ids(inst.pass_to_pass, inst.test_patch,
                                      repo_dir=repo_dir)
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
    regions = patched_regions(repo_dir, inst.patch_to_apply)
    print(f"[3] Fichiers cibles : {paths}")
    print(f"[4] Exécution sous coverage (branch mode, tag={tag})...")

    result = measure_on_prepared_repo(repo_dir, node_ids, paths,
                                      extra_node=extra_node, regions=regions)
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


def _find_enhanced_tests(instance_id: str) -> Path | None:
    enh = Path(f"runs/{instance_id}/enhanced_tests.py")
    if enh.exists():
        return enh
    cands = sorted(Path(f"runs/{instance_id}").glob("enhanced_tests*.py"))
    return cands[0] if cands else None


def _gap_counts(entry: dict) -> tuple[int, int]:
    """(missing lines, missing branches) — scopés si dispo."""
    if "region_spans" in entry:
        return (len(entry["region_missing_lines"]),
                len(entry["region_missing_branches"]))
    return len(entry["missing_lines"]), len(entry["missing_branches"])


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

    print(f"\n===== COVERAGE DELTA — {iid} (scopé région patchée) =====")
    for f, b in base["files"].items():
        a = after["files"].get(f)
        if not a:
            continue
        bl, bb = _gap_counts(b)
        al, ab = _gap_counts(a)
        print(f"  {f}")
        print(f"    branch coverage (fichier) : {b['percent_covered']}% "
              f"-> {a['percent_covered']}%")
        print(f"    gaps en région patchée    : {bb} branches / {bl} lignes "
              f"-> {ab} branches / {al} lignes")