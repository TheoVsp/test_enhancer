"""
Tracer d'exécution : capture l'évolution des variables ligne par ligne.

C'est le coeur du pipeline. Sur le whiteboard, cette étape correspond à :
  "ask LLM to inject print statements -> get debugging info -> Table"

Plutôt que d'injecter physiquement des `print(...)` dans le code source
(fragile : il faut re-parser, gérer l'indentation, réécrire les fichiers,
rebuild le conteneur...), on utilise le mécanisme natif `sys.settrace` de
Python. À chaque ligne exécutée, on capture le nom de la fonction, le numéro
de ligne, et la valeur des variables locales. Le résultat est exactement le
"tableau d'évolution des variables" voulu, mais obtenu proprement.

NOTE pour discussion avec Peter : si l'équipe tient absolument à l'injection
de print (par ex. pour rester proche de PingFL), ce module peut être remplacé
par un injecteur basé sur le module `ast`. L'interface de sortie (une liste de
TraceRow) resterait identique, donc le reste du pipeline ne changerait pas.
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_TRACE_ROWS = 20000  
MAX_VALUE_REPR_LEN = 200  

#from config import MAX_TRACE_ROWS, MAX_VALUE_REPR_LEN

@dataclass
class TraceRow:
    """Une ligne du tableau d'évolution des variables."""
    step: int            # numéro d'ordre global de l'événement
    filename: str        # fichier exécuté
    lineno: int          # numéro de ligne
    function: str        # nom de la fonction
    event: str           # "line", "call", "return"
    variables: dict[str, str]  # snapshot {nom_variable: repr_valeur}

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "filename": self.filename,
            "lineno": self.lineno,
            "function": self.function,
            "event": self.event,
            "variables": self.variables,
        }


def _is_noise(value: Any) -> bool:
    """Vrai si la valeur est du bruit (fonction, module, classe, builtin).

    Ces objets polluent le tableau sans rien apprendre sur l'état métier."""
    return (
        inspect.isfunction(value)
        or inspect.ismodule(value)
        or inspect.isclass(value)
        or inspect.isbuiltin(value)
        or inspect.ismethod(value)
    )


def _safe_repr(value: Any) -> str:
    """Représentation textuelle robuste d'une valeur de variable.

    On tronque les valeurs trop longues et on attrape toute exception
    (certains objets ont un __repr__ qui plante)."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - on veut vraiment tout attraper ici
        return "<unreprable>"
    if len(text) > MAX_VALUE_REPR_LEN:
        text = text[:MAX_VALUE_REPR_LEN] + "...<truncated>"
    return text


class VariableTracer:
    """Trace l'exécution du code dont le fichier est sous `watch_dir`.

    On ne trace QUE les fichiers situés sous `watch_dir` (le repo cible),
    sinon on capturerait l'intégralité de la stdlib et de pytest, ce qui
    serait inutilisable.
    """

    def __init__(self, watch_dir: str | Path, target_files: set[str] | None = None) -> None:
        self.watch_dir = os.path.normcase(str(Path(watch_dir).resolve()))
        self.target_files = (
            {os.path.normcase(str(Path(f).resolve())) for f in target_files}
            if target_files else None
        )
        self.rows: list[TraceRow] = []
        self._step = 0
        self._stopped = False

    def reset(self) -> None:
        self.rows.clear()
        self._step = 0
        self._stopped = False

    def _should_trace(self, filename: str) -> bool:
        # 1. Ignorer les fichiers internes de l'interpréteur (<string>, <frozen...>)
        if not filename or filename.startswith("<"):
            return False
            
        try:
            p = Path(filename).resolve()
            
            # 2. LE CORRECTIF : Si le fichier n'existe pas physiquement ici, 
            # c'est un chemin relatif trompeur de la stdlib.
            if not p.exists():
                return False
                
            resolved = os.path.normcase(str(p))
        except Exception:
            return False
        
        # Start the tracing in the taget files if specified
        if self.target_files is not None and resolved not in self.target_files:
            return False  
         
        # 3. Vérifier qu'on est bien dans le repo cible
        if not resolved.startswith(self.watch_dir):
            return False
            
        # 4. Ignorer l'infrastructure de test et de setup
        name = p.name.lower()
        if name in ("setup.py", "conftest.py"):
            return False
            
        return True

    def _trace_func(self, frame, event, arg):  # noqa: ANN001
        if self._stopped:
            return None
        filename = frame.f_code.co_filename
        if not self._should_trace(filename):
            # Renvoyer None ici dit à Python de ne pas tracer cette frame
            # ni ses sous-appels -> gros gain de performance.
            return None

        if event in ("line", "return") and self._step < MAX_TRACE_ROWS:
            # Snapshot des variables locales de la frame courante.
            snapshot = {
                name: _safe_repr(val)
                for name, val in frame.f_locals.items()
                # on ignore les variables "privées", les fonctions et modules
                if not name.startswith("__") and not _is_noise(val)
            }
            if event == "return":
                snapshot["return_value"] = _safe_repr(arg)

            self.rows.append(
                TraceRow(
                    step=self._step,
                    filename=filename,
                    lineno=frame.f_lineno,
                    function=frame.f_code.co_name,
                    event=event,
                    variables=snapshot,
                )
            )
            self._step += 1
            if self._step >= MAX_TRACE_ROWS:
                self._stopped = True

        # On retourne self._trace_func pour continuer à tracer ligne par ligne
        # dans cette frame.
        return self._trace_func

    def start(self) -> None:
        self._stopped = False
        sys.settrace(self._trace_func)
        threading.settrace(self._trace_func)
        frame = sys._getframe(1)
        while frame is not None:
            frame.f_trace = self._trace_func
            frame.f_trace_lines = True
            frame = frame.f_back
    def stop(self) ->None:
        sys.settrace(None)
        threading.settrace(None)

    def __enter__(self) -> "VariableTracer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # noqa: ANN001
        self.stop()
        return False  # ne pas avaler les exceptions
