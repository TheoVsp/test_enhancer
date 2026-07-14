"""
Auto-loaded by Python at startup whenever this file's directory is first
on PYTHONPATH (see docker_runner.py). No-ops unless TE_WATCH_DIR/TE_TRACE_OUT
are set, so it's harmless for any other python invocation in the container
(pip installs, git, etc. during the same session).
"""
import atexit
import json
import os
import sys

_watch_dir = os.environ.get("TE_WATCH_DIR")
_trace_out = os.environ.get("TE_TRACE_OUT")

if _watch_dir and _trace_out:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)

    from tracer import VariableTracer  # same dir as this file

    _target_files_env = os.environ.get("TE_TARGET_FILES", "")
    _target_files = _target_files_env.split(os.pathsep) if _target_files_env else None

    _tracer = VariableTracer(watch_dir=_watch_dir, target_files=_target_files)

    import unittest

    _orig_run = unittest.TestCase.run

    def _traced_run(self, result=None):
        _tracer.start()
        try:
            return _orig_run(self, result)
        finally:
            _tracer.stop()

    unittest.TestCase.run = _traced_run

    def _dump():
        try:
            with open(_trace_out, "w", encoding="utf-8") as f:
                json.dump([r.as_dict() for r in _tracer.rows], f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[sitecustomize] failed to dump trace: {exc}", file=sys.stderr)

    atexit.register(_dump)