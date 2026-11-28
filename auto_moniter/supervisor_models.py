"""Configuration, reports and shared values for supervised runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from auto_moniter.providers import Provider, extract_error
from auto_moniter.verdict import Verdict


class BudgetExceeded(RuntimeError):
    pass


class RecoveryError(RuntimeError):
    pass


class RefereeRedirect(RuntimeError):
    """Reload a durable recovery decision at a normal dispatch boundary."""


@dataclass
class RunConfig:
    requirement: str
    cwd: Path
    provider: Provider
    reviewer: Provider | None = None
    model: str = ""
    reviewer_model: str = ""
    extra_args: list[str] = field(default_factory=list)
    reviewer_extra_args: list[str] = field(default_factory=list)
    max_rounds: int = 0
    turn_timeout_s: float | None = None
    idle_timeout_s: float | None = None
    retry_delay_s: float | None = None
    stall_limit: int = 0
    failure_limit: int = 0
    acceptance: str = ""
    run_dir: Path | None = None
    no_review: bool = False
    fresh_review: bool = True
    resume: bool = False
    checks: list[str] = field(default_factory=list)
    check_timeout_s: float | None = None
    restart_after_stall: int = 3
    modules: list[dict] = field(default_factory=list)
    max_runtime_s: float = 0
    max_agent_calls: int = 0
    max_output_bytes: int | None = None
    review_isolation: str = "provider"
    keep_rounds: int | None = None
    worker_isolation: str = "provider"
    protect_paths: list[str] = field(default_factory=list)
    memory_bytes: int = 0
    file_bytes: int = 0
    max_run_bytes: int = 0
    max_history_bytes: int = 0
    max_agent_state_bytes: int = 0
    review_every: int | None = None
    git_history: str = 'auto'
    quality_policy: dict = field(default_factory=dict)
    workflow: str = 'legacy'
    function_dispatch: bool = False
    full_function_plan: bool = False
    task_type: str = 'code'
    referee: bool | None = None
    referee_model: str = ''
    assignment_workspaces: list[str] | None = None
    check_source_aliases: list[str] | None = None
    review_saved_round: int | None = None
    assignment_deliveries: list[str] | None = None
    assignment_contracts: dict[str, dict] | None = None


@dataclass
class RoundRecord:
    index: int
    role: str
    verdict: Verdict
    exit_code: int
    timed_out: bool
    elapsed_s: float
    session_id: str
    output_path: str
    tree: str = ""
    failure: str = ""
    interruption: str = ""
    git_checkpoint: dict = field(default_factory=dict)
    watcher_result: dict = field(default_factory=dict)
    reported_status: str = ""
    delivery: dict = field(default_factory=dict)
    concluded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RunReport:
    status: str
    rounds: int
    summary: str
    stop_reason: str
    records: list[RoundRecord] = field(default_factory=list)
    session_id: str = ""
    reviewer_session_id: str = ""
    git_history: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "session_id": self.session_id,
            "reviewer_session_id": self.reviewer_session_id,
            "git_history": self.git_history,
            "records": [
                {
                    "index": record.index,
                    "role": record.role,
                    "status": record.verdict.status,
                    "scope": record.verdict.scope,
                    "module_ids": record.verdict.module_ids,
                    "summary": record.verdict.summary,
                    "done": record.verdict.done,
                    "gaps": record.verdict.gaps,
                    "next_actions": record.verdict.next_actions,
                    "evidence": record.verdict.evidence,
                    "blockers": record.verdict.blockers,
                    "issue_resolutions": record.verdict.issue_resolutions,
                    "exit_code": record.exit_code,
                    "timed_out": record.timed_out,
                    "elapsed_s": round(record.elapsed_s, 3),
                    **({"concluded_at": record.concluded_at} if record.concluded_at else {}),
                    "session_id": record.session_id,
                    "output_path": record.output_path,
                    "tree": record.tree,
                    "failure": record.failure,
                    "interruption": record.interruption,
                    "git_checkpoint": record.git_checkpoint,
                    "watcher_result": record.watcher_result,
                    "reported_status": record.reported_status or record.verdict.status,
                    **({"delivery": record.delivery} if record.delivery else {}),
                    **({key: record.verdict.to_dict()[key] for key in ("task_id", "task_status", "next_task")}
                       if record.verdict.task_id else {}),
                }
                for record in self.records
            ],
        }


@dataclass
class _CompletedTurn:
    text: str
    session_id: str
    exit_code: int
    timed_out: bool
    elapsed_s: float
    output_path: Path
    command: list[str]
    structured: bool = False
    stderr: str = ""
    interruption: str = ""


def _limit_reached(limit: int, count: int) -> bool:
    return limit > 0 and count >= limit


def _accepted(verdict: Verdict) -> bool:
    return verdict.status == "done" and verdict.scope in {"module", "requirement"} and bool(verdict.summary) and not verdict.gaps and not verdict.next_actions and not verdict.blockers and bool(verdict.evidence)


def _audit_findings(verdict: Verdict) -> list[str]:
    synthetic = {"Unresolved dependency: " + blocker for blocker in verdict.blockers}
    findings = [gap for gap in verdict.gaps if gap not in synthetic] + verdict.blockers
    return list(dict.fromkeys(findings or verdict.next_actions))


def _continue_remaining_modules(verdict: Verdict) -> None:
    verdict.status = "continue"
    verdict.done.append("Module accepted: " + verdict.summary)
    verdict.gaps.append("Completion of a module does not establish completion of the entire original requirement.")
    verdict.next_actions.append(
        "Preserve the accepted module. Identify and implement the next unfinished module in the original requirement. "
        "If no modules remain, verify all original modules and their integration and submit scope=requirement."
    )


def _failure_reason(turn: "_CompletedTurn", verdict: Verdict) -> str:
    if turn.interruption == "input request":
        return ""
    if turn.timed_out:
        return "timed out"
    if turn.exit_code != 0:
        detail = extract_error(turn.stderr, turn.text)
        return f"exit {turn.exit_code}" + (f": {detail}" if detail else "")
    # A conclusion that is present but unstructured is a gap, not a dead process.
    if not turn.text.strip():
        return "empty output"
    return ""


def _consecutive_failures(records: list[RoundRecord], role: str) -> int:
    count = 0
    for record in reversed(records):
        if record.role != role:
            continue
        if not record.failure:
            break
        count += 1
    return count


LogFn = Callable[[str], None]


_EXECUTION_DEFAULTS = {"turn_timeout_s": 0, "idle_timeout_s": 900, "retry_delay_s": 5,
                       "check_timeout_s": 600, "max_output_bytes": 8 * 1024 * 1024}
