"""Read-only health inspection using heartbeat age and the run lock."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


_TERMINAL = {"done", "failed", "stopped", "blocked", "max_rounds", "stalled", "budget_exceeded",
             "action_required", "recovery_failed", "invalid", "locked"}


def valid_report(report: object) -> bool:
    return (isinstance(report, dict) and isinstance(report.get("status"), str) and report["status"] in _TERMINAL
            and type(report.get("rounds")) is int and report["rounds"] >= 0
            and isinstance(report.get("summary"), str) and isinstance(report.get("stop_reason"), str))


def delivery_recovery_warning(run_dir: Path) -> str:
    """An unresolved transaction cannot serve as final delivery confirmation."""
    for name, warning in (('process.json', 'outstanding_process_record'),
                          ('publication.json', 'publication_recovery_pending'),
                          ('work-checkpoint.json', 'work_checkpoint_recovery_pending')):
        path = run_dir / name
        if path.exists() or path.is_symlink():
            return warning
    return ''


def follow_up_wait_digest(run_dir: Path, state: object, report: dict) -> str:
    """Bind a waiting report to the running checkpoint it is about to replace."""
    if (not isinstance(state, dict) or state.get("status") != "running"
            or state.get("workflow") != "review-first" or report.get("workflow") != "review-first"
            or report.get("status") != "action_required" or report.get("stop_reason") != "action_required"):
        return ""
    cycles = state.get("requirement_cycles")
    review = state.get("requirements_review")
    reported_review = report.get("requirements_review")
    if not isinstance(cycles, list) or not cycles or not isinstance(cycles[-1], dict):
        return ""
    published = cycles[-1]
    handoff = published.get("handoff")
    if (published.get("status") != "committed" or published.get("index") != len(cycles)
            or not isinstance(handoff, dict) or handoff.get("cycle") != len(cycles)
            or not handoff.get("commit") or not handoff.get("ref")):
        return ""
    if any(not isinstance(brief, dict) or brief.get("status") != "needs_input"
           or brief.get("cycle") != len(cycles) + 1 for brief in (review, reported_review)):
        return ""
    reason = reported_review.get("summary")
    if not isinstance(reason, str) or not reason or report.get("summary") not in (reason, "Next requirement not ready: " + reason):
        return ""
    payload = {key: value for key, value in report.items() if key != "follow_up_wait_digest"}
    encoded = json.dumps([str(run_dir.resolve()), state, payload], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def confirms_follow_up_wait(run_dir: Path, state: dict, report: dict) -> bool:
    digest = report.get("follow_up_wait_digest")
    return (isinstance(digest, str) and bool(digest)
            and not delivery_recovery_warning(run_dir)
            and digest == follow_up_wait_digest(run_dir, state, report))


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
        elif report["status"] != state.get("status") and not confirms_follow_up_wait(run_dir, state, report):
            warning = "report_state_mismatch"
    # A monitor/guardian observed holding its lock at either end is still active.
    after = _lock_held(run_dir / "supervisor.lock")
    guardian_after = _lock_held(run_dir / "guardian.lock", missing=False)
    held = True if held is True or after is True else after
    guardian = True if guardian is True or guardian_after is True else guardian_after
    if report and report.get("status") == "done":
        warning = delivery_recovery_warning(run_dir) or warning
    finished = bool(report and not warning and held is False and guardian is False)
    resume_error = read('resume-error.json')
    if (held is False and guardian is False and resume_error
            and resume_error.get('status') == 'recovery_failed'
            and isinstance(resume_error.get('summary'), str)):
        try:
            newer = (run_dir / 'resume-error.json').stat().st_mtime_ns >= max(
                (run_dir / name).stat().st_mtime_ns for name in ('state.json', 'report.json')
                if (run_dir / name).is_file())
        except (OSError, ValueError):
            newer = False
        if newer:
            return {'state': {**state, 'status': 'recovery_failed', 'summary': resume_error['summary'],
                              'resume_error': True}, 'finished': True, 'lock_held': held,
                    'guardian_lock_held': guardian, 'report_warning': ''}
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
    for token, name in [("paused", "paused"), ("recovering", "recovering"), ("acceptance check", "validating"), ("publishing approved", "publishing"), ("retrying", "retry_wait"), ("closing", "closing_agents"), ("review", "reviewing"), ("worker", "working")]:
        if token in note.lower():
            phase = name
            break
    if any(token in note for token in ("awaiting_permission", "waiting_for_permission", "waiting_for_approval", "awaiting_approval", "waiting_for_input", "awaiting_input", "needs_input", "requires_action", "requires_permission")):
        phase = "waiting_for_input"
    if 'referee' in note.lower():
        phase = 'refereeing'
    if health in {"finished", "monitor_exited"}:
        phase = health
    elif health == "recovering":
        phase = "recovering"
    elif ((state.get('requirements_review') or {}).get('status')=='reviewing'
          and health=='active' and phase in {'preparing','reviewing'}):
        phase='refining_requirements'
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
