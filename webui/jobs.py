"""
Background job manager.

Every long-running backend action (pipelines, backtester, hyperparameter
search, initialize, tests, backups, Windows-task management, DB maintenance)
is exposed here as a named job. Each job runs the existing project script as a
subprocess so it never blocks the web server, and its combined stdout/stderr is
streamed live to the browser.

A fixed JOB REGISTRY (no arbitrary commands) keeps this safe: the UI can only
launch one of the predefined jobs, optionally toggling a small set of declared
flags.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


# ── Job registry ──────────────────────────────────────────────────────────────
#
# Each entry: id -> {
#   "label":  human name shown in the UI,
#   "group":  section header,
#   "base":   base argv (after the python executable),
#   "desc":   one-line description,
#   "flags":  optional {flag_id: {"label":..., "args":[...]}}  toggles,
#   "danger": True to render a confirm button,
# }

JOBS: dict[str, dict] = {
    "pipeline": {
        "label": "Technical pipeline",
        "group": "Daily run",
        "base": ["pipeline.py"],
        "desc": "Download yesterday's OHLCV, run all setups, send signal alert.",
    },
    "news": {
        "label": "News + AI picks",
        "group": "Daily run",
        "base": ["-m", "news_analyzer.pipeline"],
        "desc": "Fetch news, run Ollama analysis + 3 scout lenses, send picks.",
    },
    "backtester": {
        "label": "Backtester",
        "group": "Calibration",
        "base": ["backtester.py"],
        "desc": "Compute avg-return weights for every setup.",
        "flags": {
            "quick": {"label": "Quick (stride=3)", "args": ["--quick"]},
        },
    },
    "hyper_grid": {
        "label": "Hyperparameter search (grid)",
        "group": "Calibration",
        "base": ["hyperparameter_search.py"],
        "desc": "Exhaustive search of a fixed parameter grid (incl. stop-loss). "
                "Writes db/optimal_params.json — run the Backtester afterwards "
                "to refresh weights from the new params.",
        "flags": {
            "quick": {"label": "Quick grid", "args": ["--quick"]},
            "force": {"label": "Force re-run all", "args": ["--force-rerun"]},
        },
    },
    "hyper_random": {
        "label": "Hyperparameter search (random)",
        "group": "Calibration",
        "base": ["hyperparameter_search.py", "--random", "--samples", "200", "--peaks", "5"],
        "desc": "Random sampling over a wider range to find local maxima a fixed "
                "grid can miss; each peak is re-validated at full precision. Also "
                "writes db/optimal_params.json — run the Backtester afterwards "
                "to refresh weights from the new params.",
    },
    "initialize": {
        "label": "Initialize / full reset",
        "group": "Calibration",
        "base": ["initialize.py"],
        "desc": "WIPES data, re-downloads full history, runs backtester (~15-25 min).",
        "danger": True,
    },
    "hft_backtest": {
        "label": "HFT / intraday backtest",
        "group": "Calibration",
        "base": ["hft_backtester.py"],
        "desc": "Run all setups across 1/5/10/15-min bars (long+short, "
                "EOD square-off, 10bps cost). Pick one timeframe option below "
                "(default: 15min only); leave years blank for the full "
                "2015-2026 dataset, or use the extra-args box for "
                "--years 2024,2025,2026 and/or --symbols 250. "
                "Writes db/hft_results.json.",
        "flags": {
            "all": {"label": "All timeframes (1/5/10/15min)", "args": ["--timeframes", "1min,5min,10min,15min"]},
            "1min": {"label": "1min only", "args": ["--timeframes", "1min"]},
            "5min": {"label": "5min only", "args": ["--timeframes", "5min"]},
            "10min": {"label": "10min only", "args": ["--timeframes", "10min"]},
            "15min": {"label": "15min only", "args": ["--timeframes", "15min"]},
        },
        "extra_placeholder": "--years 2024,2025,2026 --symbols 250",
    },
    "verify_setups": {
        "label": "Verify setups",
        "group": "Tests",
        "base": ["tests/verify_setups.py"],
        "desc": "Synthetic-data tests for every strategy, both directions.",
    },
    "simulate_backtester": {
        "label": "Simulate backtester",
        "group": "Tests",
        "base": ["tests/simulate_backtester.py"],
        "desc": "269-assertion avg-return + direction-split verification.",
    },
    "integrity": {
        "label": "DB integrity check",
        "group": "Maintenance",
        "base": ["-c", "from data.db import init_db, integrity_check; init_db(); print('integrity_check:', integrity_check())"],
        "desc": "Run SQLite PRAGMA integrity_check.",
    },
    "backup": {
        "label": "Backup database",
        "group": "Maintenance",
        "base": ["-c", "from utils.backup import create_backup; p=create_backup(); print('backup ->', p)"],
        "desc": "WAL checkpoint + timestamped DB backup.",
    },
    "news_clear": {
        "label": "Clear news/scout history",
        "group": "Maintenance",
        "base": ["-m", "news_analyzer.db", "--clear"],
        "desc": "Wipe news + scout dedup tables (next run may resend picks).",
        "danger": True,
    },
    "task_register": {
        "label": "Register Windows task",
        "group": "Scheduler",
        "base": ["setup_windows_task.py"],
        "desc": "Register daily 7 AM news + 8 AM technical tasks.",
    },
    "task_status": {
        "label": "Task status",
        "group": "Scheduler",
        "base": ["setup_windows_task.py", "--status"],
        "desc": "Show registered Task Scheduler status.",
    },
    "task_remove": {
        "label": "Remove Windows task",
        "group": "Scheduler",
        "base": ["setup_windows_task.py", "--remove"],
        "desc": "Delete the registered scheduler tasks.",
        "danger": True,
    },
}


def job_catalog() -> list[dict]:
    """Public, UI-safe view of the registry (no raw argv)."""
    out = []
    for jid, spec in JOBS.items():
        out.append({
            "id": jid,
            "label": spec["label"],
            "group": spec["group"],
            "desc": spec["desc"],
            "danger": spec.get("danger", False),
            "flags": [
                {"id": fid, "label": f["label"]}
                for fid, f in spec.get("flags", {}).items()
            ],
            "extra_placeholder": spec.get("extra_placeholder"),
        })
    return out


# ── Running job model ──────────────────────────────────────────────────────────

class Job:
    def __init__(self, jid: int, spec_id: str, label: str, argv: list[str]):
        self.id = jid
        self.spec_id = spec_id
        self.label = label
        self.argv = argv
        self.started_at = datetime.now()
        self.ended_at: datetime | None = None
        self.returncode: int | None = None
        self.status = "running"               # running | done | failed | stopped
        self._lines: deque[str] = deque(maxlen=5000)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            lines = list(self._lines)
        return {
            "id": self.id,
            "spec_id": self.spec_id,
            "label": self.label,
            "status": self.status,
            "returncode": self.returncode,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": self.ended_at.strftime("%Y-%m-%d %H:%M:%S") if self.ended_at else None,
            "total_lines": len(lines),
            "lines": lines[since:],
            "since": max(since, 0),
        }

    def stop(self) -> bool:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                return True
            except Exception:
                return False
        return False


class JobManager:
    def __init__(self):
        self._jobs: dict[int, Job] = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def launch(self, spec_id: str, flags: list[str] | None = None,
               extra: list[str] | None = None) -> Job:
        spec = JOBS.get(spec_id)
        if not spec:
            raise KeyError(f"unknown job '{spec_id}'")

        argv = list(spec["base"])
        for fid in (flags or []):
            f = spec.get("flags", {}).get(fid)
            if f:
                argv += f["args"]
        # `extra` is only used for whitelisted free-form args (e.g. symbols)
        if extra:
            argv += [str(a) for a in extra]

        with self._lock:
            jid = next(self._counter)
            job = Job(jid, spec_id, spec["label"], argv)
            self._jobs[jid] = job

        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        t.start()
        return job

    def _run(self, job: Job) -> None:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [PY] + job.argv
        job.append(f"$ {' '.join(cmd)}")
        job.append("")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            job.append(f"[launch error] {exc}")
            job.status = "failed"
            job.ended_at = datetime.now()
            job.returncode = -1
            return

        job._proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            job.append(line.rstrip("\n"))
        proc.wait()
        job.returncode = proc.returncode
        job.ended_at = datetime.now()
        if job.status == "running":
            job.status = "done" if proc.returncode == 0 else "failed"
        job.append("")
        job.append(f"[exit code {proc.returncode}]")

    def get(self, jid: int) -> Job | None:
        return self._jobs.get(jid)

    def stop(self, jid: int) -> bool:
        job = self._jobs.get(jid)
        if not job:
            return False
        ok = job.stop()
        if ok:
            job.status = "stopped"
        return ok

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)
        return [{
            "id": j.id,
            "spec_id": j.spec_id,
            "label": j.label,
            "status": j.status,
            "started_at": j.started_at.strftime("%H:%M:%S"),
            "ended_at": j.ended_at.strftime("%H:%M:%S") if j.ended_at else None,
            "returncode": j.returncode,
        } for j in jobs]


# Module-level singleton used by the server
manager = JobManager()
