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

Sorties :
    runs/coverage_batch_summary.json   (détail par instance)
    runs/coverage_batch_summary.md     (tableau prêt pour une réunion)
"""
from __future__ import annotations

import json
import sys
import traceback
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


def eval_one(instance_id: str) -> dict:
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
        mb_before = b["region_missing_branches"] if scoped else b["missing_branches"]
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
    return {"instance_id": instance_id, "status": "ok", "files": rows}


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
        except SystemExit as e:          # measure_coverage fait sys.exit sur erreur
            print(f"[SKIP] {iid}: {e}")
            results.append({"instance_id": iid, "status": f"skip: {e}",
                            "files": []})
        except Exception as e:           # noqa: BLE001
            print(f"[ERREUR] {iid}: {e}")
            traceback.print_exc()
            results.append({"instance_id": iid, "status": f"error: {e}",
                            "files": []})

    # ---- Agrégation ----
    ok = [r for r in results if r["status"] == "ok" and r["files"]]
    md = ["| Instance | File | Branch cov. before | after | Branches closed |",
          "|---|---|---|---|---|"]
    tot_missing_before = tot_missing_after = tot_branches = 0
    n_full, n_improved, n_unchanged = 0, 0, 0
    for r in ok:
        for row in r["files"]:
            md.append(f"| {r['instance_id']} | {Path(row['file']).name} "
                      f"| {row['pct_before']}% | {row['pct_after']}% "
                      f"| {len(row['branches_closed'])}"
                      f"/{row['missing_before']} |")
            tot_missing_before += row["missing_before"]
            tot_missing_after += row["missing_after"]
            tot_branches += row["n_branches"]
            if row["missing_after"] == 0 and row["missing_before"] > 0:
                n_full += 1
            elif row["missing_after"] < row["missing_before"]:
                n_improved += 1
            else:
                n_unchanged += 1

    print("\n===== BILAN BATCH =====")
    print(f"Instances mesurées OK      : {len(ok)}/{len(ids)}")
    print(f"Branches manquantes (avant): {tot_missing_before}")
    print(f"Branches manquantes (après): {tot_missing_after}")
    closed = tot_missing_before - tot_missing_after
    if tot_missing_before:
        print(f"Branches comblées          : {closed} "
              f"({100 * closed / tot_missing_before:.0f}% des gaps)")
    print(f"Fichiers à 100% après      : {n_full}  |  améliorés: {n_improved}"
          f"  |  inchangés: {n_unchanged}")

    md += ["", f"**Totals:** {closed}/{tot_missing_before} missing branches "
               f"closed ({100 * closed / tot_missing_before:.0f}% of measured "
               f"gaps) across {len(ok)} instances; {n_full} file(s) reach "
               f"100% branch coverage." if tot_missing_before else ""]
    Path("runs/coverage_batch_summary.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    Path("runs/coverage_batch_summary.md").write_text(
        "\n".join(md), encoding="utf-8")
    print("\n-> runs/coverage_batch_summary.json")
    print("-> runs/coverage_batch_summary.md")


if __name__ == "__main__":
    main()