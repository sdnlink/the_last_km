"""Read-only health inspection using heartbeat age and the run lock."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


_TERMINAL = {"done", "failed", "stopped", "blocked", "max_rounds", "stalled", "budget_exceeded",
             "action_required", "recovery_failed", "invalid", "locked"}


def valid_report(report: object) -> bool:
    return (isinstance(report, dict) and isinstance(report.get("status"), str) and report["status"] in _TERMINAL
            and type(report.get("rounds")) is int and report["rounds"] >= 0
            and isinstance(report.get("summary"), str) and isinstance(report.get("stop_reason"), str))


def _lock_held(path: Path, *, missing=None):
    try:
        import fcntl
        with path.open("r") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return missing
    except (OSError, ImportError):
        return None


def read_run_snapshot(run_dir: Path) -> dict:
    """Sample locks and persisted state without trusting an orphan/stale report."""
    held = _lock_held(run_dir / "supervisor.lock")
    guardian = _lock_held(run_dir / "guardian.lock", missing=False)
    def read(name):
        try:
            value = json.loads((run_dir / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None
    report = read("report.json")
    state = read("state.json")
    if state is None and not (run_dir / "state.json").exists() and not (run_dir / "report.json").exists():
        raise FileNotFoundError(f"No run state in {run_dir}")
    warning = ""
    if state is None:
        state = {"status": "unknown"}
        warning = "state_missing_or_invalid"
    elif report is not None or (run_dir / "report.json").exists():
        if not valid_report(report):
            warning = "report_invalid"
        elif report["status"] != state.get("status"):
            warning = "report_state_mismatch"
    # A monitor/guardian observed holding its lock at either end is still active.
    after = _lock_held(run_dir / "supervisor.lock")
    guardian_after = _lock_held(run_dir / "guardian.lock", missing=False)
    held = True if held is True or after is True else after
    guardian = True if guardian is True or guardian_after is True else guardian_after
    if report and report.get("status") == "done" and (run_dir / "process.json").exists():
        warning = "outstanding_process_record"
    finished = bool(report and not warning and held is False and guardian is False)
    return {"state": report if finished else state, "finished": finished,
            "lock_held": held, "guardian_lock_held": guardian, "report_warning": warning}


def run_health(run_dir: Path, stale_after: float = 60, *, snapshot: dict | None = None) -> dict:
    snapshot = snapshot if snapshot is not None else read_run_snapshot(run_dir)
    state = snapshot["state"]
    heartbeat = {}
    try:
        heartbeat = json.loads((run_dir / "heartbeat").read_text(encoding="utf-8"))
        if not isinstance(heartbeat, dict):
            heartbeat = {}
    except (OSError, ValueError):
        pass
    age = None
    try:
        at = datetime.fromisoformat(heartbeat["at"])
        age = max(0, (datetime.now(timezone.utc) - at).total_seconds())
    except (KeyError, ValueError, TypeError):
        pass
    held = snapshot["lock_held"]
    note = str(heartbeat.get("note") or "")
    if snapshot["finished"]:
        health = "finished"
    elif snapshot["guardian_lock_held"] is True and held is not True:
        health = "recovering"
    elif held is False:
        health = "monitor_exited"
    elif age is None:
        health = "heartbeat_missing"
    elif age > stale_after:
        health = "heartbeat_stale"
    elif held is None:
        health = "unknown"
    else:
        health = "active"
    phase = "preparing"
    for token, name in [("paused", "paused"), ("recovering", "recovering"), ("acceptance check", "validating"), ("retrying", "retry_wait"), ("closing", "closing_agents"), ("review", "reviewing"), ("worker", "working")]:
        if token in note.lower():
            phase = name
            break
    if any(token in note for token in ("awaiting_permission", "waiting_for_permission", "waiting_for_approval", "awaiting_approval", "waiting_for_input", "awaiting_input", "needs_input", "requires_action", "requires_permission")):
        phase = "waiting_for_input"
    if health in {"finished", "monitor_exited"}:
        phase = health
    elif health == "recovering":
        phase = "recovering"
    usage = dict(state.get("usage") or {})
    if held:
        try:
            recorded = datetime.fromisoformat(usage["recorded_at"])
            usage["elapsed_s"] = round(float(usage["elapsed_s"]) + max(0, (datetime.now(timezone.utc) - recorded).total_seconds()), 3)
        except (KeyError, TypeError, ValueError):
            pass
    modules = state.get("modules") or []
    rounds = state.get("rounds") if isinstance(state.get("rounds"), list) else state.get("records", [])
    guardian = {}
    try:
        guardian = json.loads((run_dir / "guardian.json").read_text(encoding="utf-8"))
        if not isinstance(guardian, dict):
            guardian = {}
    except (OSError, ValueError):
        pass
    return {
        "status": state.get("status"), "health": health, "phase": phase,
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "lock_held": held, "activity": note,
        "guardian_lock_held": snapshot["guardian_lock_held"], "report_warning": snapshot["report_warning"],
        "delivery_confirmed": snapshot["finished"] and state.get("status") == "done",
        "rounds": sum(item.get("role") == "worker" for item in rounds) + (state.get("history") or {}).get("pruned_worker_rounds", 0),
        "history": state.get("history", {}),
        "last_interruption": next((item["interruption"] for item in reversed(rounds) if item.get("interruption")), ""),
        "stall": state.get("stall", 0), "usage": usage,
        "no_progress_turns": state.get("no_progress_turns", 0), "recovery_count": state.get("recovery_count", 0),
        "interrupted_process_recorded": (run_dir / "process.json").exists(),
        "guardian": guardian,
        "review_schedule": state.get("review_schedule", {}),
        "audit_issues": state.get("audit_issues", {}),
        "accepted_modules": [item["id"] for item in modules if item.get("status") == "accepted"],
        "pending_modules": [item["id"] for item in modules if item.get("status") != "accepted"],
    }
