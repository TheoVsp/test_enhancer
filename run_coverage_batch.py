"""
Batch de mesure du coverage delta (avant/après enrichissement) sur plusieurs
instances — la métrique headline indépendante des patches agents.

Usage :
    python run_coverage_batch.py                     # auto-découverte
    python run_coverage_batch.py id1 id2 ...         # liste explicite

Auto-découverte : toute instance de runs/ qui possède à la fois
  - runs/<id>/enhanced_tests*.py  (tests générés)
  - runs/_repos/<id>/             (repo préparé)

Aucun appel LLM : on mesure avec les tests déjà générés.

Les instances sont classées par MÉTHODE de génération :
  - "loop"     : tests produits par la boucle mesurée (runs/<id>/loop_history.json présent)
  - "one-shot" : tests produits par l'ancienne génération en un seul appel
ce qui permet de comparer directement les deux approches.

Les gaps comptés sont ceux de la RÉGION PATCHÉE (fonctions touchées par le
patch) quand le scoping est disponible — pas le fichier entier.

Sorties :
    runs/coverage_batch_summary.json   (détail par instance)
    runs/coverage_batch_summary.md     (tableau prêt pour une réunion)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import coverage_probe


def discover_instances() -> list[str]:
    ids = []
    runs = Path("runs")
    if not runs.exists():
        return ids
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        has_tests = any(d.glob("enhanced_tests*.py"))
        has_repo = (runs / "_repos" / d.name).exists()
        if has_tests and has_repo:
            ids.append(d.name)
    return ids


def detect_method(instance_id: str) -> str:
    """"loop" si l'instance a été traitée par la boucle mesurée, sinon
    "one-shot" (ancienne génération en un seul appel LLM)."""
    if Path(f"runs/{instance_id}/loop_history.json").exists():
        return "loop"
    return "one-shot"


def eval_one(instance_id: str) -> dict:
    method = detect_method(instance_id)
    base = coverage_probe.measure_coverage(instance_id, tag="baseline")
    enh_file = coverage_probe._find_enhanced_tests(instance_id)
    after = coverage_probe.measure_coverage(instance_id,
                                            extra_test_file=enh_file,
                                            tag="enhanced")
    rows = []
    for f, b in base["files"].items():
        a = after["files"].get(f)
        if not a:
            continue
        scoped = "region_missing_branches" in b
        mb_before = (b["region_missing_branches"] if scoped
                     else b["missing_branches"])
        mb_after = (a.get("region_missing_branches", a["missing_branches"])
                    if scoped else a["missing_branches"])
        closed = [br for br in mb_before if br not in mb_after]
        rows.append({
            "file": f,
            "scoped": scoped,
            "pct_before": b["percent_covered"],
            "pct_after": a["percent_covered"],
            "n_branches": b["num_branches"],
            "missing_before": len(mb_before),
            "missing_after": len(mb_after),
            "branches_closed": closed,
        })
    return {"instance_id": instance_id, "status": "ok",
            "method": method, "files": rows}


def main():
    ids = sys.argv[1:] or discover_instances()
    if not ids:
        sys.exit("Aucune instance trouvée (runs/<id>/enhanced_tests*.py "
                 "+ runs/_repos/<id> requis).")
    print(f"=== Batch coverage delta sur {len(ids)} instance(s) ===\n")

    results = []
    for iid in ids:
        print(f"\n########## {iid} ##########")
        try:
            results.append(eval_one(iid))
        except SystemExit as e:      # measure_coverage fait sys.exit sur erreur
            print(f"[SKIP] {iid}: {e}")
            results.append({"instance_id": iid, "status": f"skip: {e}",
                            "method": detect_method(iid), "files": []})
        except Exception as e:       # noqa: BLE001
            msg = str(e)
            # Instances anciennes : nécessitent Python <= 3.9 (ex. l'import
            # `from collections import Mapping`, supprimé en 3.10). Ce n'est
            # pas une erreur du pipeline mais une incompatibilité d'env.
            if "cannot import name" in msg or "ImportError" in msg:
                print(f"[SKIP] {iid}: environnement incompatible "
                      f"(instance ancienne, Python <= 3.9 requis)")
                results.append({"instance_id": iid,
                                "status": "skip: python version",
                                "method": detect_method(iid), "files": []})
            else:
                print(f"[ERREUR] {iid}: {msg.splitlines()[0]}")
                results.append({"instance_id": iid, "status": f"error: {msg}",
                                "method": detect_method(iid), "files": []})

    # ---- Agrégation ----
    ok = [r for r in results if r["status"] == "ok" and r["files"]]
    md = ["| Instance | Method | File | Branch cov. before | after | "
          "Gaps closed (patched region) |",
          "|---|---|---|---|---|---|"]
    tot_missing_before = tot_missing_after = 0
    n_full, n_improved, n_unchanged = 0, 0, 0

    for r in ok:
        for row in r["files"]:
            md.append(f"| {r['instance_id']} | {r['method']} "
                      f"| {Path(row['file']).name} "
                      f"| {row['pct_before']}% | {row['pct_after']}% "
                      f"| {len(row['branches_closed'])}"
                      f"/{row['missing_before']} |")
            tot_missing_before += row["missing_before"]
            tot_missing_after += row["missing_after"]
            if row["missing_after"] == 0 and row["missing_before"] > 0:
                n_full += 1
            elif row["missing_after"] < row["missing_before"]:
                n_improved += 1
            else:
                n_unchanged += 1

    closed = tot_missing_before - tot_missing_after
    print("\n===== BILAN BATCH =====")
    print(f"Instances mesurées OK      : {len(ok)}/{len(ids)}")
    print(f"Gaps actionnables (avant)  : {tot_missing_before}")
    print(f"Gaps actionnables (après)  : {tot_missing_after}")
    if tot_missing_before:
        print(f"Gaps comblés               : {closed} "
              f"({100 * closed / tot_missing_before:.0f}%)")
    print(f"Fichiers à 0 gap restant   : {n_full}  |  améliorés: {n_improved}"
          f"  |  inchangés: {n_unchanged}")

    # ---- Comparaison par méthode (le chiffre à présenter) ----
    print("\n----- PAR MÉTHODE -----")
    md.append("")
    md.append("**By method:**")
    md.append("")
    md.append("| Method | Instances | Gaps before | Gaps closed | Rate |")
    md.append("|---|---|---|---|---|")
    for label in ("loop", "one-shot"):
        sub = [r for r in ok if r.get("method") == label]
        mb = sum(row["missing_before"] for r in sub for row in r["files"])
        ma = sum(row["missing_after"] for r in sub for row in r["files"])
        if not sub:
            continue
        rate = f"{100 * (mb - ma) / mb:.0f}%" if mb else "n/a"
        print(f"  {label:9s}: {mb - ma}/{mb} gaps comblés ({rate}) "
              f"sur {len(sub)} instance(s)")
        md.append(f"| {label} | {len(sub)} | {mb} | {mb - ma} | {rate} |")

    # ---- Instances écartées ----
    skipped = [r for r in results if r["status"] != "ok"]
    if skipped:
        print("\n----- ÉCARTÉES -----")
        for r in skipped:
            print(f"  {r['instance_id']}: {r['status'].splitlines()[0]}")

    Path("runs/coverage_batch_summary.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    Path("runs/coverage_batch_summary.md").write_text(
        "\n".join(md), encoding="utf-8")
    print("\n-> runs/coverage_batch_summary.json")
    print("-> runs/coverage_batch_summary.md")


if __name__ == "__main__":
    main()
