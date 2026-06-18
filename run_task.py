"""
Windowless launcher for the scheduled pipelines.

The Windows tasks run this via pythonw.exe (no console window) instead of
`cmd.exe ... >> log`, so nothing flashes on the desktop at 07:00 / 08:00. Because
there's no shell to redirect output and pythonw has no stdout/stderr, this script
points stdout/stderr at the task's log file FIRST — before importing the pipeline —
so every log handler and any uncaught traceback is captured. It then runs the
target pipeline exactly as its own `__main__` (same as `python pipeline.py` /
`python -m news_analyzer.pipeline`).

Usage:  pythonw run_task.py <daily|news> <logfile>
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.resolve()

# kind -> (how to run, target). "path" runs a script file; "module" runs `-m`.
_TARGETS = {
    "daily": ("path",   str(PROJECT / "pipeline.py")),
    "news":  ("module", "news_analyzer.pipeline"),
}


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in _TARGETS:
        sys.stderr.write("usage: run_task.py <daily|news> <logfile>\n")
        return 2
    kind, logfile = sys.argv[1], sys.argv[2]

    Path(logfile).parent.mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(logfile, "a", encoding="utf-8", buffering=1)

    mode, target = _TARGETS[kind]
    # Reset argv so the pipeline's own argparse/__main__ sees a bare invocation
    # (the launcher's "daily <log>" args must not leak into it).
    sys.argv = [target]
    if mode == "path":
        runpy.run_path(target, run_name="__main__")
    else:
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
