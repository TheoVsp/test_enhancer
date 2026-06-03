"""
Construction des deux artefacts d'analyse à partir d'une trace :

  1. Le TABLEAU d'évolution des variables (whiteboard: "-> Table (variable)")
     exporté en CSV (et Excel si openpyxl dispo). C'est une structure EXTERNE
     au code, comme demandé.

  2. Le CODE ANNOTÉ : le code source avec, en fin de chaque ligne exécutée,
     un commentaire indiquant les valeurs observées des variables
     (whiteboard: "comment in code at end of line to help LLM analysis").

Ces deux artefacts sont ensuite donnés au LLM pour l'analyse.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .tracer import TraceRow


def build_variable_table(rows: list[TraceRow]) -> list[dict]:
    """Aplatit la trace en lignes de tableau (une ligne par variable observée).

    Format de sortie (colonnes) :
      step | function | lineno | event | variable | value
    """
    table: list[dict] = []
    for row in rows:
        if not row.variables:
            # ligne sans variable locale visible -> on garde une trace minimale
            table.append({
                "step": row.step,
                "function": row.function,
                "lineno": row.lineno,
                "event": row.event,
                "variable": "",
                "value": "",
            })
            continue
        for var_name, var_value in row.variables.items():
            table.append({
                "step": row.step,
                "function": row.function,
                "lineno": row.lineno,
                "event": row.event,
                "variable": var_name,
                "value": var_value,
            })
    return table


def write_table_csv(table: list[dict], out_path: Path) -> Path:
    """Écrit le tableau en CSV (lisible dans Excel, Google Sheets, etc.)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["step", "function", "lineno", "event", "variable", "value"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(table)
    return out_path


def write_table_xlsx(table: list[dict], out_path: Path) -> Path | None:
    """Écrit le tableau en Excel si openpyxl est installé, sinon None."""
    try:
        from openpyxl import Workbook
    except ImportError:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "variable_trace"
    headers = ["step", "function", "lineno", "event", "variable", "value"]
    ws.append(headers)
    for entry in table:
        ws.append([entry[h] for h in headers])
    wb.save(out_path)
    return out_path


def annotate_source(
    source_path: Path,
    rows: list[TraceRow],
    out_path: Path,
) -> Path:
    """Produit une version du fichier source avec des commentaires inline.

    Pour chaque ligne du fichier, si elle a été exécutée (présente dans la
    trace), on ajoute en fin de ligne un commentaire du type :
        x = y + 1   # [TE] x=3 | y=2

    On agrège toutes les valeurs distinctes observées sur cette ligne pour
    chaque variable (utile pour les boucles : montre la plage de valeurs).
    """
    source_path = Path(source_path)
    target_name = str(source_path.resolve())

    # On regroupe les valeurs observées par (ligne -> variable -> set de valeurs)
    per_line: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if str(Path(row.filename).resolve()) != target_name:
            continue
        for var, val in row.variables.items():
            seen = per_line[row.lineno][var]
            if val not in seen:
                seen.append(val)

    original_lines = source_path.read_text(encoding="utf-8").splitlines()
    annotated: list[str] = []
    for idx, line in enumerate(original_lines, start=1):
        if idx in per_line:
            parts = []
            for var, values in per_line[idx].items():
                if len(values) == 1:
                    parts.append(f"{var}={values[0]}")
                else:
                    # plusieurs valeurs (ex. boucle) -> on montre jusqu'à 3
                    shown = ", ".join(values[:3])
                    suffix = ", ..." if len(values) > 3 else ""
                    parts.append(f"{var}=[{shown}{suffix}]")
            comment = "  # [TE] " + " | ".join(parts)
            annotated.append(line + comment)
        else:
            annotated.append(line)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(annotated), encoding="utf-8")
    return out_path
