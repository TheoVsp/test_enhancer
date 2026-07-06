import inspect
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union


MAX_TRACE_ROWS = 20000
MAX_VALUE_REPR_LEN = 200


class TraceRow:
    """Une ligne du tableau d'évolution des variables."""

    def __init__(
        self,
        step,
        filename,
        lineno,
        function,
        event,
        variables,
    ):
        self.step = step
        self.filename = filename
        self.lineno = lineno
        self.function = function
        self.event = event
        self.variables = variables

    def as_dict(self):
        return {
            "step": self.step,
            "filename": self.filename,
            "lineno": self.lineno,
            "function": self.function,
            "event": self.event,
            "variables": self.variables,
        }


def _is_noise(value):
    return (
        inspect.isfunction(value)
        or inspect.ismodule(value)
        or inspect.isclass(value)
        or inspect.isbuiltin(value)
        or inspect.ismethod(value)
    )


def _safe_repr(value):
    try:
        text = repr(value)
    except Exception:
        return "<unreprable>"

    if len(text) > MAX_VALUE_REPR_LEN:
        text = text[:MAX_VALUE_REPR_LEN] + "...<truncated>"

    return text


class VariableTracer:
    def __init__(
        self,
        watch_dir,
        target_files=None,
    ):
        self.watch_dir = os.path.normcase(str(Path(watch_dir).resolve()))

        if target_files:
            self.target_files = {
                os.path.normcase(str(Path(f).resolve())) for f in target_files
            }
        else:
            self.target_files = None

        self.rows = []
        self._step = 0
        self._stopped = False

    def reset(self):
        self.rows = []
        self._step = 0
        self._stopped = False

    def _should_trace(self, filename):
        if not filename or filename.startswith("<"):
            return False

        try:
            p = Path(filename).resolve()

            if not p.exists():
                return False

            resolved = os.path.normcase(str(p))
            
        except Exception:
            return False
        if self.target_files is not None:
            if resolved not in self.target_files:
                return False

        name = p.name.lower()
        if name in ("setup.py", "conftest.py"):
            return False

        return True

    def _trace_func(self, frame, event, arg):
        if self._stopped:
            return None

        filename = frame.f_code.co_filename

        if not self._should_trace(filename):
            return None

        if event in ("line", "return") and self._step < MAX_TRACE_ROWS:

            snapshot = {}
            for name, val in frame.f_locals.items():
                if name.startswith("__"):
                    continue
                if _is_noise(val):
                    continue
                snapshot[name] = _safe_repr(val)

            if event == "return":
                snapshot["return_value"] = _safe_repr(arg)

            self.rows.append(
                TraceRow(
                    self._step,
                    filename,
                    frame.f_lineno,
                    frame.f_code.co_name,
                    event,
                    snapshot,
                )
            )

            self._step += 1

            if self._step >= MAX_TRACE_ROWS:
                self._stopped = True

        return self._trace_func

    def start(self):
        self._stopped = False
        sys.settrace(self._trace_func)
        threading.settrace(self._trace_func)

        frame = sys._getframe(1)
        while frame is not None:
            frame.f_trace = self._trace_func
            #frame.f_trace_lines = True
            frame = frame.f_back

    def stop(self):
        sys.settrace(None)
        threading.settrace(None)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False