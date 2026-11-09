"""An opt-in parent process that resumes a monitor after an unexpected exit."""

from __future__ import annotations

import json
import multiprocessing
import os
import math
import signal
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.storage import write_json
from auto_moniter.paths import default_run_dir, private_directory, state_root
from auto_moniter.redaction import redact
from auto_moniter.health import valid_report
from auto_moniter.supervisor import RunConfig, RunReport, Supervisor
from auto_moniter.workspace import LockError, file_lock
from auto_moniter.locking import workspace_lease


def _monitor(config: RunConfig, sender) -> None:
    # The monitor owns its signal handlers independently of the guardian.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        report = Supervisor(config).run()
        sender.send({"status": report.status, "rounds": report.rounds,
                     "summary": report.summary, "stop_reason": report.stop_reason})
    finally:
        sender.close()


class Guardian:
    def __init__(self, config: RunConfig, *, max_restarts: int = 3, delay_s: float = 5, shutdown_s: float = 30, log=None) -> None:
        self.config = config
        self.config.cwd = config.cwd.expanduser().resolve()
        self.run_dir = (config.run_dir or default_run_dir(self.config.cwd)).expanduser().resolve()
        self.config.run_dir = self.run_dir
        self.max_restarts = max_restarts
        self.delay_s = delay_s
        self.shutdown_s = shutdown_s
        self.log = log or (lambda message: print(message, flush=True))
        self._stop = threading.Event()
        self._child = None
        self._restarts = 0

    def request_stop(self) -> None:
        self._stop.set()

    def _check_control(self) -> None:
        try:
            if json.loads((self.run_dir / "control.json").read_text(encoding="utf-8")).get("action") == "stop":
                self.request_stop()
        except FileNotFoundError:
            pass

    def _write(self, status: str, reason: str = "") -> None:
        write_json(self.run_dir / "guardian.json", {
            "status": status, "restarts": self._restarts, "max_restarts": self.max_restarts,
            "monitor_pid": self._child.pid if self._child else None,
            "guardian_pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(), "reason": redact(reason)[:2000],
        })
        if status in {"restart_limit", "recovery_failed", "force_stopping"}:
            write_json(self.run_dir / "alert.json", {"status": status, "summary": redact(reason)[:2000],
                       "at": datetime.now(timezone.utc).isoformat()}, durable=True)

    def run(self) -> RunReport:
        if self.max_restarts < 0 or self.delay_s < 0 or not math.isfinite(self.delay_s) or not math.isfinite(self.shutdown_s) or self.shutdown_s <= 0:
            return RunReport("invalid", 0, "Guardian restart count and delay must be nonnegative", "invalid")
        private_directory(state_root())
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda _sig, _frame: self.request_stop())
        try:
            with file_lock(self.run_dir / "guardian.lock"):
                # Refuse to wrap an already-running independent monitor.
                with workspace_lease(self.config.cwd), file_lock(self.config.cwd / ".auto-moniter/workspace.lock"), file_lock(self.run_dir / "supervisor.lock"):
                    if not self.config.resume and any((self.run_dir / name).exists() for name in ("state.json", "report.json", "requirement.md", "baseline.json.gz")):
                        return RunReport("invalid", 0, "Run directory already contains a run. Use --resume or a new --run-dir.", "invalid")
                    if self.config.resume and (self.run_dir / "guardian.json").exists():
                        old = json.loads((self.run_dir / "guardian.json").read_text(encoding="utf-8"))
                        if not isinstance(old, dict):
                            raise ValueError("invalid saved guardian state")
                        self._restarts = int(old.get("restarts") or 0)
                        if self._restarts < 0:
                            raise ValueError("invalid saved guardian restart count")
                return self._run_locked()
        except LockError as exc:
            return RunReport("locked", 0, str(exc), "locked")
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _run_locked(self) -> RunReport:
        context = multiprocessing.get_context("spawn")
        config = self.config
        while not self._stop.is_set():
            self._check_control()
            if self._stop.is_set():
                break
            started_ns = time.time_ns()
            receiver, sender = context.Pipe(duplex=False)
            self._child = context.Process(target=_monitor, args=(config, sender))
            try:
                self._child.start()
                sender.close()
                self._write("monitor_running")
                self.log(f"guardian: monitor pid={self._child.pid} run={self.run_dir}")
                stopping = False
                stop_deadline = None
                result = None
                while self._child.is_alive():
                    self._check_control()
                    if self._stop.is_set() and not stopping:
                        stopping = True
                        self._write("stopping", "Stop requested; automatic restart disabled")
                        self._child.terminate()
                        stop_deadline = time.monotonic() + self.shutdown_s
                    if stopping and time.monotonic() >= stop_deadline:
                        self._write("force_stopping", "Graceful shutdown deadline exceeded")
                        self._child.kill()
                        self._child.join(2)
                        if self._child.is_alive():
                            raise RuntimeError("Monitor did not exit after SIGKILL")
                        break
                    # Drain the tiny result before join, avoiding a full-pipe deadlock.
                    if receiver.poll(0.1):
                        try:
                            result = receiver.recv()
                        except EOFError:
                            pass
                    self._child.join(0.1)
                self._child.join(2)
                if result is None and receiver.poll():
                    try:
                        result = receiver.recv()
                    except EOFError:
                        pass
                if result is None:
                    result = self._new_report(started_ns)
                if result is not None:
                    self._write("finished", result["status"])
                    return RunReport(**result)
                if self._stop.is_set():
                    break
                state = self._resumable_state()
                if not state:
                    self._write("recovery_failed", "No complete resumable monitor checkpoint")
                    return RunReport("recovery_failed", 0, "Monitor exited before saving a resumable checkpoint; no automatic restart.", "recovery_failed")
                if self._restarts >= self.max_restarts:
                    stopped = Supervisor(replace(self.config, resume=True)).stop_interrupted("Guardian restart limit reached; interrupted work stopped.")
                    if stopped.status != "stopped":
                        self._write("recovery_failed", stopped.summary)
                        return stopped
                    self._write("restart_limit", f"Unexpected monitor exit {self._child.exitcode}")
                    return RunReport("restart_limit", 0, "Guardian restart limit reached; inspect the run before resuming.", "restart_limit")
                self._restarts += 1
                self._write("restarting", f"Unexpected monitor exit {self._child.exitcode}")
                self.log(f"guardian: restarting monitor ({self._restarts}/{self.max_restarts})")
                delay = min(60, self.delay_s * 2 ** min(self._restarts - 1, 6))
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self._stop.is_set():
                    self._check_control()
                    self._stop.wait(min(0.2, max(0, deadline - time.monotonic())))
                if self._stop.is_set():
                    break
                config = replace(self.config, resume=True)
            finally:
                sender.close()
                receiver.close()
                # Never leave a monitor running if guardian setup or bookkeeping fails.
                if self._child.is_alive():
                    self._child.terminate()
                    self._child.join(self.shutdown_s)
                    if self._child.is_alive():
                        self._child.kill()
                        self._child.join(2)
                    if self._child.is_alive():
                        raise RuntimeError("Monitor did not exit after forced shutdown")
                self._child.close()
                self._child = None
        if self._resumable_state():
            stopped = Supervisor(replace(self.config, resume=True)).stop_interrupted("Guardian stopped by request; interrupted work stopped.")
            if stopped.status != "stopped":
                self._write("recovery_failed", stopped.summary)
                return stopped
        self._write("stopped", "Stop requested; automatic restart disabled")
        return RunReport("stopped", 0, "Guardian stopped by request.", "stopped")

    def _new_report(self, started_ns: int) -> dict | None:
        path = self.run_dir / "report.json"
        try:
            if path.stat().st_mtime_ns < started_ns:
                return None
            report = json.loads(path.read_text(encoding="utf-8"))
            state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
            if not valid_report(report) or not isinstance(state, dict) or state.get("status") != report["status"]:
                return None
            if report.get("status") == "done" and (self.run_dir / "process.json").exists():
                return None
            return {"status": report["status"], "rounds": report["rounds"],
                    "summary": report["summary"], "stop_reason": report["stop_reason"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _resumable_state(self) -> bool:
        try:
            state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
            return isinstance(state, dict) and state.get("status") in {"running", "stopped", "blocked", "budget_exceeded", "recovery_failed"} and (self.run_dir / "requirement.md").is_file()
        except (OSError, ValueError):
            return False
