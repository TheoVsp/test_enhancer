"""
Standalone tracer — no package imports, safe to run inside Docker via sys.path injection.
Constants that were in config.py are inlined here so this file is fully self-contained.
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Inlined from config.py so this module has zero relative-import dependencies
MAX_TRACE_ROWS = 30000
MAX_VALUE_REPR_LEN = 200


@dataclass
class TraceRow:
    """Une ligne du tableau d'évolution des variables."""
    step: int
    filename: str
    lineno: int
    function: str
    event: str
    variables: dict[str, str]

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
    return (
        inspect.isfunction(value)
        or inspect.ismodule(value)
        or inspect.isclass(value)
        or inspect.isbuiltin(value)
        or inspect.ismethod(value)
    )


def _safe_repr(value: Any) -> str:
    try:
        text = repr(value)
    except Exception:
        return "<unrepr able>"
    if len(text) > MAX_VALUE_REPR_LEN:
        text = text[:MAX_VALUE_REPR_LEN] + "...<truncated>"
    return text


class VariableTracer:
    """Trace l'exécution du code dont le fichier est sous `watch_dir`."""

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
        if not filename or filename.startswith("<"):
            return False
        try:
            p = Path(filename).resolve()
            if not p.exists():
                return False
            resolved = os.path.normcase(str(p))
        except Exception:
            return False
        
        if self.target_files is not None and resolved not in self.target_files:
            return False
        
        if not resolved.startswith(self.watch_dir):
            return False

        name = p.name.lower()
        if name in ("setup.py", "conftest.py"):
            return False

        return True

    def _trace_func(self, frame, event, arg):  # noqa: ANN001
        if self._step == 0 and event == "call":
            print(f"[TRACE CALLED] file={frame.f_code.co_filename}", file=sys.stderr, flush=True)
        if self._stopped:
            return None

        filename = frame.f_code.co_filename
        if not self._should_trace(filename):
            return None

        if event in ("line", "call", "return") and self._step < MAX_TRACE_ROWS:
            snapshot = {
                name: _safe_repr(val)
                for name, val in frame.f_locals.items()
                if not name.startswith("__") and not _is_noise(val)
            }
            if event =="return":
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

    def stop(self) -> None:
        sys.settrace(None)
        threading.settrace(None)

    def __enter__(self) -> "VariableTracer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # noqa: ANN001
        self.stop()
        return False
