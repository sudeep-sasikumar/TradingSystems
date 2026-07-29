#!/usr/bin/env python3
"""
InsiderSwing — one-click background pipeline runner.

Runs the whole insider system end to end:

    universe -> ingest -> score -> backtest  [-> sweep]

WHY THIS EXISTS AS A SEPARATE PROCESS
-------------------------------------
The EDGAR backfill takes hours. Running it inside a Streamlit callback ties it
to the browser: closing the tab, the session expiring, or the user's laptop
sleeping would all kill it mid-way. The dashboard therefore launches THIS script
detached — new session / new process group, no controlling terminal, stdio
redirected to a log file — so it keeps running as long as the container does,
which on an always-on VPS means it just finishes.

Because nothing is watching it, progress goes to the DB (``ins_pipeline_runs``)
rather than to a screen. The user can come back in ten minutes or tomorrow and
the Setup & Admin tab reads the current state from there.

``heartbeat_at`` is touched every 30s by a daemon thread. During a 4-hour ingest
step there is otherwise no way to distinguish "still working" from "the process
was OOM-killed an hour ago" — both look like a row stuck on 'running'.

Run manually (equivalent to the button, but in the foreground):
    python InsiderSwing/pipeline.py --start 2016-01-01
    python InsiderSwing/pipeline.py --start 2022-01-01 --quick --sweep
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent          # InsiderSwing/
_ROOT = _HERE.parent                             # project root
for _p in (str(_ROOT), str(_HERE), str(_HERE / "sources"), str(_HERE / "backtest")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg                             # noqa: E402
from db import get_engine, session_scope         # noqa: E402
from models import InsiderPipelineRun            # noqa: E402

_RUN_INSIDER = str(_HERE / "run_insider.py")
_PIPELINE = str(_HERE / "pipeline.py")

# A run whose heartbeat is older than this is treated as dead, not slow.
STALE_HEARTBEAT_SECONDS = 300
HEARTBEAT_INTERVAL = 30

# How long the launcher waits for the child to register itself in the DB.
# Interpreter start-up plus the pandas/sqlalchemy/yfinance imports run ~10s on a
# modest VPS, so anything shorter reports healthy launches as failures.
STARTUP_WAIT_SECONDS = 45

# 12 tickers used by quick mode. Liquid large caps with real insider activity —
# enough to exercise every stage without committing to the full backfill.
QUICK_TICKERS = "INTC,F,KMI,OXY,T,PARA,WBA,MRNA,DVN,APA,HAL,NEM"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shift_year(date_str: str, delta: int) -> str:
    try:
        y, m, d = (int(x) for x in str(date_str).strip().split("-"))
        return f"{y + delta:04d}-{m:02d}-{d:02d}"
    except Exception:
        return date_str


# ──────────────────────────────────────────────────────────────────────────────
#  Status queries (used by the dashboard)
# ──────────────────────────────────────────────────────────────────────────────

def latest_run() -> Optional[dict]:
    """Most recent pipeline run as a plain dict, or None."""
    from sqlalchemy import text

    try:
        with get_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT id, status, mode, params_json, current_step, step_index, "
                "       total_steps, steps_json, started_at, heartbeat_at, finished_at, "
                "       pid, log_path, backtest_run_id, error_message "
                "FROM ins_pipeline_runs ORDER BY id DESC LIMIT 1"
            )).fetchone()
    except Exception:
        return None
    if row is None:
        return None

    out = dict(row._mapping)
    out["steps"] = json.loads(out.get("steps_json") or "[]")
    out["params"] = json.loads(out.get("params_json") or "{}")
    out["stale"] = False

    if out["status"] == "running":
        out["stale"] = heartbeat_age_seconds(out.get("heartbeat_at")) > STALE_HEARTBEAT_SECONDS
    return out


def heartbeat_age_seconds(heartbeat_at: Optional[str]) -> float:
    if not heartbeat_at:
        return float("inf")
    try:
        hb = datetime.fromisoformat(str(heartbeat_at))
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - hb).total_seconds()
    except Exception:
        return float("inf")


def elapsed_seconds(started_at: Optional[str], finished_at: Optional[str] = None) -> float:
    if not started_at:
        return 0.0
    try:
        start = datetime.fromisoformat(str(started_at))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        if finished_at:
            end = datetime.fromisoformat(str(finished_at))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        return max((end - start).total_seconds(), 0.0)
    except Exception:
        return 0.0


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def is_active() -> bool:
    """True if a pipeline is running AND its heartbeat is fresh."""
    run = latest_run()
    return bool(run and run["status"] == "running" and not run["stale"])


def reap_stale_runs() -> int:
    """
    Mark every dead 'running' row as cancelled. Returns how many were reaped.

    Sweeps ALL rows, not just the newest: once a second run is started the dead
    predecessor is no longer the latest row, and a version of this that only
    checked the latest would leave it marked 'running' forever.

    Only touches rows whose heartbeat has gone stale, so it can never kill a
    healthy run — the job here is clearing wreckage from a container restart,
    not providing a stop button.
    """
    from sqlalchemy import text

    reaped = 0
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(
                "SELECT id, heartbeat_at FROM ins_pipeline_runs WHERE status='running'"
            )).fetchall()
    except Exception:
        return 0

    for row_id, heartbeat in rows:
        if heartbeat_age_seconds(heartbeat) <= STALE_HEARTBEAT_SECONDS:
            continue        # still alive — leave it alone
        with session_scope() as sess:
            row = sess.get(InsiderPipelineRun, int(row_id))
            if row is not None and row.status == "running":
                row.status = "cancelled"
                row.finished_at = _now()
                row.error_message = (
                    f"No heartbeat for over {STALE_HEARTBEAT_SECONDS // 60} minutes — "
                    "the process died (container restart or out-of-memory). Marked "
                    "cancelled so a new run can start. Nothing is lost: EDGAR documents "
                    "are cached on disk and re-runs are idempotent."
                )
                reaped += 1
    return reaped


def cancel_stalled() -> bool:
    """Backwards-compatible wrapper: True if anything was reaped."""
    return reap_stale_runs() > 0


def tail_log(run: Optional[dict], lines: int = 60) -> str:
    """Last N lines of a run's log file."""
    if not run or not run.get("log_path"):
        return ""
    p = Path(str(run["log_path"]))
    if not p.exists():
        return ""
    try:
        content = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception as exc:
        return f"(could not read log: {exc})"


# ──────────────────────────────────────────────────────────────────────────────
#  Launcher (called by the dashboard)
# ──────────────────────────────────────────────────────────────────────────────

def launch_detached(start: str, quick: bool = False, sweep: bool = False) -> dict:
    """
    Start the pipeline as a fully detached background process.

    Detachment specifics, because this is the part that has to be right:
      * POSIX — ``start_new_session=True`` calls setsid(), so the child leaves
        the dashboard's process group and session and survives its parent.
      * Windows — DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, the equivalent.
      * stdin from DEVNULL so nothing can ever block on a read.
      * stdout/stderr to a bootstrap log rather than DEVNULL. The runner writes
        its own detailed log, but it can only do that AFTER its imports succeed.
        A missing dependency in the image would otherwise kill the child before
        it writes anything at all, and the launch would look successful — the
        worst possible failure mode for a job nobody is watching.

    Returns {"ok": bool, "pid"/"reason": ...}.
    """
    if is_active():
        return {"ok": False, "reason": "a pipeline run is already in progress"}

    cfg.ensure_dirs()
    cmd = [sys.executable, _PIPELINE, "--start", start]
    if quick:
        cmd.append("--quick")
    if sweep:
        cmd.append("--sweep")

    boot_log = cfg.REPORT_DIR / "pipeline_launch.log"
    try:
        logf = open(boot_log, "a", encoding="utf-8", errors="replace")
        logf.write(f"\n=== launch {_now()} : {' '.join(cmd)}\n")
        logf.flush()
    except Exception:
        logf = None

    kwargs: dict = {
        "cwd": str(_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": logf or subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if logf else subprocess.DEVNULL,
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "reason": f"could not start the background process: {exc}"}
    finally:
        if logf is not None:
            logf.close()      # the child holds its own inherited handle

    # Wait for the child to register itself. Interpreter start-up plus the
    # pandas/sqlalchemy/yfinance imports take ~10s, so a short wait would report
    # a healthy launch as a failure.
    before = latest_run()
    before_id = before["id"] if before else 0
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS

    while time.monotonic() < deadline:
        time.sleep(0.5)
        run = latest_run()
        if run and run["id"] != before_id:
            return {"ok": True, "pid": proc.pid, "run_id": run["id"]}
        if proc.poll() is not None:
            # Exited before registering — a real startup failure.
            tail = ""
            try:
                tail = "\n".join(
                    boot_log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
                )
            except Exception:
                pass
            return {"ok": False,
                    "reason": f"the background process exited immediately "
                              f"(code {proc.returncode}). Last output:\n{tail}"}

    # Still alive but slow to register — not an error, just a busy host.
    return {"ok": True, "pid": proc.pid, "run_id": None, "slow_start": True}


def _RUN_INSIDER_PIPELINE() -> str:
    return str(_HERE / "pipeline.py")


# ──────────────────────────────────────────────────────────────────────────────
#  Runner
# ──────────────────────────────────────────────────────────────────────────────

class _Progress:
    """Owns the DB row for one pipeline run."""

    def __init__(self, mode: str, params: dict, total_steps: int, log_path: Path):
        self.log_path = log_path
        with session_scope() as sess:
            row = InsiderPipelineRun(
                status="running", mode=mode,
                params_json=json.dumps(params),
                current_step="starting", step_index=0, total_steps=total_steps,
                steps_json="[]", started_at=_now(), heartbeat_at=_now(),
                pid=os.getpid(), log_path=str(log_path),
            )
            sess.add(row)
            sess.flush()
            self.run_id = row.id
        self.steps: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            try:
                with session_scope() as sess:
                    row = sess.get(InsiderPipelineRun, self.run_id)
                    if row is not None and row.status == "running":
                        row.heartbeat_at = _now()
            except Exception:
                pass      # a transient DB lock must never take down the pipeline

    def start_step(self, index: int, label: str) -> None:
        self.steps.append({"name": label, "status": "running",
                           "started_at": _now(), "finished_at": None,
                           "returncode": None, "tail": None})
        self._save(current_step=label, step_index=index)

    def finish_step(self, ok: bool, returncode: Optional[int], tail: str) -> None:
        if self.steps:
            self.steps[-1].update({
                "status": "success" if ok else "failed",
                "finished_at": _now(), "returncode": returncode,
                "tail": tail[-2000:] if tail else None,
            })
        self._save()

    def finish(self, status: str, error: Optional[str] = None,
               backtest_run_id: Optional[int] = None) -> None:
        self._stop.set()
        with session_scope() as sess:
            row = sess.get(InsiderPipelineRun, self.run_id)
            if row is not None:
                row.status = status
                row.finished_at = _now()
                row.heartbeat_at = _now()
                row.steps_json = json.dumps(self.steps)
                row.error_message = error
                row.current_step = "done" if status == "success" else (row.current_step or "")
                if backtest_run_id:
                    row.backtest_run_id = backtest_run_id

    def _save(self, current_step: Optional[str] = None,
              step_index: Optional[int] = None) -> None:
        with session_scope() as sess:
            row = sess.get(InsiderPipelineRun, self.run_id)
            if row is None:
                return
            row.steps_json = json.dumps(self.steps)
            row.heartbeat_at = _now()
            if current_step is not None:
                row.current_step = current_step
            if step_index is not None:
                row.step_index = step_index


def _latest_backtest_run_id() -> Optional[int]:
    from sqlalchemy import text
    try:
        with get_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT id FROM ins_backtest_runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
            )).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def run_pipeline(start: str, quick: bool = False, sweep: bool = False) -> int:
    """
    Execute every stage in order, recording progress as we go.

    Stops at the first failure: each stage consumes the previous one's output,
    so continuing past a failure would produce a backtest over missing data and
    report it as a finding.

    Returns a process exit code (0 = success).
    """
    cfg.ensure_dirs()

    # Starting a new run is the natural moment to clean up dead predecessors —
    # otherwise a row killed by a container restart stays 'running' forever and
    # the status panel keeps reporting a job that no longer exists.
    reap_stale_runs()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = cfg.REPORT_DIR / f"pipeline_{stamp}.log"

    ticker_args = ["--tickers", QUICK_TICKERS] if quick else []
    ingest_start = _shift_year(start, -2)      # trailing-average history for the size score

    steps: list[tuple[str, list[str]]] = [
        ("1/N  Universe & SEC CIK resolution",
         ["--checkpoint", "universe", "--start", start]),
        (f"2/N  Ingest Form 4 filings from {ingest_start} (the long one)",
         ["--checkpoint", "ingest", "--start", ingest_start] + ticker_args),
        ("3/N  Conviction scoring",
         ["--checkpoint", "score", "--start", start] + ticker_args),
        ("4/N  Three-arm backtest + saved report",
         ["--checkpoint", "backtest", "--start", start,
          "--label", "quick_test" if quick else "vps_baseline"] + ticker_args),
    ]
    if sweep:
        steps.append(("5/N  Parameter stability sweep",
                      ["--checkpoint", "sweep", "--start", start,
                       "--label", "vps"] + ticker_args))

    total = len(steps)
    steps = [(label.replace("/N", f"/{total}"), args) for label, args in steps]

    params = {"start": start, "ingest_start": ingest_start,
              "quick": quick, "sweep": sweep,
              "tickers": QUICK_TICKERS if quick else "full universe"}
    prog = _Progress("quick" if quick else "full", params, total, log_path)

    with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"=== InsiderSwing pipeline run #{prog.run_id} ===\n")
        logf.write(f"started : {_now()}\n")
        logf.write(f"mode    : {'quick (12 tickers)' if quick else 'full universe'}\n")
        logf.write(f"params  : {json.dumps(params)}\n")
        logf.write(f"pid     : {os.getpid()}\n\n")
        logf.flush()

        for i, (label, args) in enumerate(steps, 1):
            prog.start_step(i, label)
            logf.write(f"\n\n===== [{_now()}] {label} =====\n")
            logf.flush()

            cmd = [sys.executable, _RUN_INSIDER] + args
            logf.write(f"$ {' '.join(cmd)}\n\n")
            logf.flush()

            try:
                result = subprocess.run(
                    cmd, cwd=str(_ROOT), stdout=logf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
                )
                rc = result.returncode
            except Exception as exc:      # noqa: BLE001
                logf.write(f"\n!! step raised: {exc}\n")
                logf.flush()
                prog.finish_step(False, None, str(exc))
                prog.finish("failed", error=f"{label} raised: {exc}")
                return 1

            logf.flush()
            tail = tail_log({"log_path": str(log_path)}, lines=25)

            if rc != 0:
                logf.write(f"\n!! step failed with exit code {rc}\n")
                logf.flush()
                prog.finish_step(False, rc, tail)
                prog.finish(
                    "failed",
                    error=(f"{label} failed with exit code {rc}. Later steps were skipped "
                           "because each one consumes the previous step's output."),
                )
                return rc

            prog.finish_step(True, rc, tail)

        logf.write(f"\n\n=== pipeline complete at {_now()} ===\n")

    prog.finish("success", backtest_run_id=_latest_backtest_run_id())
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="InsiderSwing one-click pipeline")
    ap.add_argument("--start", default=cfg.DEFAULT_CONFIG.backtest_start or "2016-01-01")
    ap.add_argument("--quick", action="store_true",
                    help="12-ticker smoke run instead of the full universe")
    ap.add_argument("--sweep", action="store_true",
                    help="also run the parameter-stability sweep")
    ap.add_argument("--force", action="store_true",
                    help="start even if another run appears to be in progress")
    args = ap.parse_args()

    # The dashboard button checks this too, but the CLI is the path where a
    # second run gets started by accident — two pipelines writing the same
    # SQLite file will interleave their scores and trades.
    if is_active() and not args.force:
        run = latest_run()
        print(f"A pipeline run is already in progress (run #{run['id']}, "
              f"{run['current_step']}, started {fmt_duration(elapsed_seconds(run['started_at']))} ago).\n"
              f"Wait for it, or pass --force to override.", file=sys.stderr)
        sys.exit(2)

    sys.exit(run_pipeline(args.start, quick=args.quick, sweep=args.sweep))


if __name__ == "__main__":
    main()
