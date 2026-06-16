"""
Script exécuté DANS le conteneur Docker.
Activates the tracer, runs pytest, writes trace rows to JSON.

Usage (inside container):
    python /tracer_inject/runner_inside.py <watch_dir> <output_json> <test_id> [test_id ...]

NOTE: This script is intentionally standalone — no relative imports.
      It loads tracer.py from the same directory via sys.path.
"""
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the injected directory importable so we can load tracer.py
# without any package context (no relative imports).
# ---------------------------------------------------------------------------
_inject_dir = str(Path(__file__).parent.resolve())
if _inject_dir not in sys.path:
    sys.path.insert(0, _inject_dir)

from tracer import VariableTracer  # noqa: E402

# ---------------------------------------------------------------------------
# Parse CLI arguments
# ---------------------------------------------------------------------------
if len(sys.argv) < 3:
    print("Usage: runner_inside.py <watch_dir> <output_json> [test_id ...]",
          file=sys.stderr)
    sys.exit(2)

watch_dir   = sys.argv[1]
output_path = sys.argv[2]
rest   = sys.argv[3:]

target_files = None
if "--target-files" in rest:
    idx = rest.index("--target-files")
    test_ids = rest[:idx]
    target_files = rest[idx + 1:]
else:
    test_ids = rest

print(f"[runner_inside] watch_dir={watch_dir}", file=sys.stderr, flush=True)
print(f"[runner_inside] test_ids={test_ids}",   file=sys.stderr, flush=True)
print(f"[runner_inside] target_files={target_files}", file=sys.stderr, flush=True)

# ---------------------------------------------------------------------------
# Set up tracer + pytest plugin
# ---------------------------------------------------------------------------
tracer = VariableTracer(watch_dir=watch_dir, target_files=target_files)

import pytest


class TracerPlugin:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        tracer.reset()
        tracer.start()
        try:
            yield
        finally:
            tracer.stop()


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------
os.chdir("/repo")
ret = pytest.main(
    ["-x", "-q", "--no-header", "--tb=short", *test_ids],
    plugins=[TracerPlugin()],
)

print(f"[runner_inside] pytest returned {ret}, trace_rows={len(tracer.rows)}",
      file=sys.stderr, flush=True)

# ---------------------------------------------------------------------------
# Write trace rows to output JSON
# ---------------------------------------------------------------------------
Path(output_path).write_text(
    json.dumps([r.as_dict() for r in tracer.rows], ensure_ascii=False),
    encoding="utf-8",
)

sys.exit(0 if ret == 0 else 1)
