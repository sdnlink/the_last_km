"""Keep a coding agent working until an independent review accepts the result."""

from __future__ import annotations

import codecs
import gzip
import json
import os
import signal
import selectors
import math
import hashlib
import sys
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from auto_moniter.providers import (
    PreparedCommand,
    Provider,
    ProviderError,
    TurnResult,
    extract_error,
    extract_session_id,
    extract_text,
    paseo_transcript,
    redact_command,
)
from auto_moniter.workspace import (
    LockError,
    acceptance_problems,
    file_lock,
    file_snapshot,
    workspace_diff,
    workspace_fingerprint,
    workspace_snapshot,
)
from auto_moniter.verdict import VERDICT_SCHEMA, Verdict, input_request, parse_verdict, verdict_instructions
from auto_moniter.runlog import clipped, diagnostic_summary
from auto_moniter.modules import ModuleLedger
from auto_moniter.storage import write_json
from auto_moniter.paths import default_run_dir, private_directory, workspace_lock, state_root
from auto_moniter.processes import process_identity, recover_process, reap_background
from auto_moniter.events import record as record_event
from auto_moniter.failures import classify
from auto_moniter.isolation import ReviewIsolation, WorkerIsolation
from auto_moniter.history import prune_files
from auto_moniter.preflight import validate
from auto_moniter.redaction import redact, safe
from auto_moniter.audit import AuditLedger, AuditCapacityError
from auto_moniter.live import LiveOutput
from auto_moniter.directions import Directions
from auto_moniter.git_history import GitHistory, GitHistoryError


LogFn = Callable[[str], None]


class BudgetExceeded(RuntimeError):
    pass


class RecoveryError(RuntimeError):
    pass


_EXECUTION_DEFAULTS = {"turn_timeout_s": 0, "idle_timeout_s": 900, "retry_delay_s": 5,
                       "check_timeout_s": 600, "max_output_bytes": 8 * 1024 * 1024}


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
    review_every: int | None = None
    git_history: str = 'auto'


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
                    "session_id": record.session_id,
                    "output_path": record.output_path,
                    "tree": record.tree,
                    "failure": record.failure,
                    "interruption": record.interruption,
                    "git_checkpoint": record.git_checkpoint,
                }
                for record in self.records
            ],
        }


class Supervisor:
    def __init__(self, config: RunConfig, log: LogFn | None = None) -> None:
        self.config = config
        self._execution_overrides = {key for key in _EXECUTION_DEFAULTS if getattr(config, key) is not None}
        for key, value in _EXECUTION_DEFAULTS.items():
            if getattr(config, key) is None:
                setattr(config, key, value)
        self.config.cwd = config.cwd.expanduser().resolve()
        sink = log or (lambda message: print(message, flush=True))
        self.log = lambda message: sink(redact(message))
        self.run_dir = (config.run_dir or default_run_dir(self.config.cwd)).expanduser().resolve()
        self.schema_path = self.run_dir / "verdict.schema.json"
        self._stop = False
        self._stop_event = threading.Event()
        self._child: subprocess.Popen[str] | None = None
        self._heartbeat = self.run_dir / "heartbeat"
        self.worker_session_id = ""
        self.reviewer_session_id = ""
        self._next_round = 1
        self._drop_worker_session = False
        self._resume_refused = ""
        self._stop_requested = False
        self._active_reviewer_id = ""
        self._records: list[RoundRecord] = []
        self._review_feedback: Verdict | None = None
        self._audit_feedback: Verdict | None = None
        self._rounds_since_review = 0
        self._stop_reason = ""
        self._baseline_saved = False
        self._recovery_note = ""
        self.modules = ModuleLedger(config.modules)
        self.audit = AuditLedger()
        self._started = time.monotonic()
        self._elapsed_before = 0.0
        self._agent_calls = 0
        self._no_progress = 0
        self._progress_reset = False
        self._recovery_count = 0
        self._delivery_tree = ""
        self._isolation = None
        self._worker_isolation = None
        self._last_space_check = 0.0
        self._space_exceeded = False
        self._pruned_worker_rounds = 0
        self._pruned_records = 0
        self._pruned_failures = {"worker": 0, "reviewer": 0}
        self.directions = Directions(self.run_dir)
        self._direction_ids = {'worker': [], 'reviewer': []}
        self.git_history: GitHistory | None = None
        self._git_before = ''

    def request_stop(self) -> None:
        self._stop = True
        self._stop_requested = True
        self._stop_reason = self._stop_reason or "stop requested"
        self._stop_event.set()
        child = self._child
        if child and child.poll() is None:
            _terminate(child)

    def stop_interrupted(self, reason: str) -> RunReport:
        """Stop owned work after a dead monitor, without launching any new turn."""
        try:
            with file_lock(workspace_lock(self.config.cwd)), file_lock(self.config.cwd / ".auto-moniter/workspace.lock"), file_lock(self.run_dir / "supervisor.lock"):
                loaded = self._load_resume()
                if self._resume_refused or loaded is None:
                    return RunReport("invalid", 0, "Run is not resumable; no cleanup attempted.", "invalid")
                self._records = loaded[0]
                if (self.run_dir / 'git-history.json').exists():
                    self.git_history = GitHistory.open(self.config.cwd, self.run_dir, self.config.git_history)
                self._recover_interrupted_run()
                return self._finish("stopped", reason, self._records, None)
        except (OSError, ValueError, TypeError, AttributeError, RecoveryError, LockError, GitHistoryError) as exc:
            return RunReport("recovery_failed", 0, f"Could not stop interrupted work: {exc}", "recovery_failed")

    def run(self) -> RunReport:
        self._started = time.monotonic()
        try:
            validate(self.config)
        except (ValueError, ProviderError) as exc:
            return RunReport("invalid", 0, str(exc), "invalid")
        if (not math.isfinite(self.config.max_runtime_s) or self.config.max_runtime_s < 0
                or self.config.max_agent_calls < 0 or self.config.max_output_bytes <= 0 or (self.config.keep_rounds is not None and self.config.keep_rounds < 0)):
            return RunReport("invalid", 0, "Resource budgets must be nonnegative and output limit must be positive", "invalid")
        if self.config.reviewer and not self.config.no_review:
            try:
                self.config.reviewer.validate_review(self.config.reviewer_extra_args)
                if self.config.reviewer.name == "paseo" and not self.config.fresh_review:
                    raise ProviderError("Paseo read-only review requires a fresh session; remove --no-fresh-review.")
                if self.config.review_isolation == "strict":
                    self._isolation = ReviewIsolation(self.config.cwd, self.run_dir)
                    if self.config.reviewer.name == "paseo":
                        raise ProviderError("Strict read-only isolation requires a native reviewer")
            except ProviderError as exc:
                return RunReport("invalid", 0, str(exc), "invalid")
        private_directory(state_root())
        previous_signals = self._install_signals()
        try:
            with file_lock(workspace_lock(self.config.cwd)), file_lock(self.config.cwd / ".auto-moniter" / "workspace.lock"), file_lock(self.run_dir / "supervisor.lock"):
                try:
                    return self._run_locked()
                except Exception as exc:
                    summary = f"{type(exc).__name__}: {exc}"
                    if self._stop_reason:
                        summary = f"Stopped: {self._stop_reason}. {summary}"
                    self.log(f"run error: {summary}")
                    status = "recovery_failed" if isinstance(exc, RecoveryError) else ("budget_exceeded" if isinstance(exc, BudgetExceeded) else ("stopped" if self._stop_requested else "failed"))
                    try:
                        return self._finish(status, summary, self._records, None)
                    except OSError as write_error:
                        self.log(f"could not save final state: {write_error}")
                        return RunReport(status, len([r for r in self._records if r.role == "worker"]), summary, status, self._records)
        except LockError as exc:
            self.log(str(exc))
            return RunReport(status="locked", rounds=0, summary=str(exc), stop_reason="locked")
        finally:
            for sig, handler in previous_signals.items():
                signal.signal(sig, handler)

    def _run_locked(self) -> RunReport:
        private_directory(self.run_dir)
        if self.config.worker_isolation == "strict":
            try:
                self._worker_isolation = WorkerIsolation(self.config.cwd, self.run_dir)
                self._worker_isolation.verify(self.config.provider.name)
            except ProviderError as exc:
                return RunReport("invalid", 0, str(exc), "invalid")
        if self.config.check_timeout_s <= 0:
            return RunReport("invalid", 0, "check timeout must be positive", "invalid")
        if self._isolation:
            try:
                self._isolation.verify(self.config.reviewer.name)
            except ProviderError as exc:
                return RunReport("invalid", 0, str(exc), "invalid")
        requirement_path = self.run_dir / "requirement.md"
        if not self.config.resume and any((self.run_dir / name).exists() for name in ("state.json", "report.json", "requirement.md", "baseline.json.gz")):
            return RunReport("invalid", 0, "Run directory already contains a run. Use --resume or a new --run-dir.", "invalid")
        loaded = None
        if self.config.resume:
            try:
                loaded = self._load_resume()
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                return RunReport("invalid", 0, f"Cannot resume run: {exc}", "invalid")
            if self._resume_refused:
                return RunReport(self._resume_refused, 0, f"Run already finished with status {self._resume_refused}. Start a new --run-dir to run again.", "invalid")
            if not requirement_path.is_file():
                return RunReport("invalid", 0, "Cannot resume run: requirement.md is missing.", "invalid")
        self.schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")
        if self.config.keep_rounds is None:
            self.config.keep_rounds = 100
        if self.config.review_every is None:
            self.config.review_every = 5
        if not requirement_path.exists():
            requirement_path.write_text(self.config.requirement.strip() + "\n", encoding="utf-8")
            with requirement_path.open("rb") as handle:
                os.fsync(handle.fileno())
        elif self.config.resume:
            saved = requirement_path.read_text(encoding="utf-8").strip()
            incoming = self.config.requirement.strip()
            if incoming and incoming != saved:
                self.log("resume uses the requirement saved in the run directory")
            self.config.requirement = saved
        if not hasattr(self, "_baseline_files"):
            self._baseline_files = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        self.git_history = GitHistory.open(self.config.cwd, self.run_dir, self.config.git_history)
        if self.git_history:
            if (self.git_history.last_worker and self.git_history.last_worker['round'] >= self._next_round):
                raise RecoveryError('Git checkpoint is ahead of run state; reconcile the saved round before resuming')
            self.log(f'project Git history: {self.git_history.ref}')
        self.log(f"run directory: {self.run_dir}")
        self.log(f"worker: {self.config.provider.name}")
        reviewer = None if self.config.no_review else self.config.reviewer
        if reviewer is None and not self.config.no_review:
            return RunReport("invalid", 0, "Pass --reviewer, or --no-review to trust the worker.", "invalid")
        elif reviewer:
            self.log(f"reviewer: {reviewer.name}")

        records = self._records
        stall = 0
        previous_gap_key = ""
        previous_tree = ""
        worker_failures = 0
        reviewer_failures = 0
        last_worker: Verdict | None = None
        last_review: Verdict | None = None
        if loaded:
            records, stall, previous_gap_key, previous_tree = loaded[0], loaded[1], loaded[2], loaded[3]
            self._records = records
            worker_failures, reviewer_failures = loaded[4], loaded[5]
            last_worker, last_review = loaded[6], loaded[7]
            self.log(
                f"resumed at round {self._next_round} "
                f"worker_session={self.worker_session_id or '-'} "
                f"reviewer_session={self.reviewer_session_id or '-'}"
            )
            self._recover_interrupted_run()
        (self.run_dir / "report.json").unlink(missing_ok=True)
        self._write_state(records, "running", stall, previous_gap_key, previous_tree)

        while not self._stop:
            self._process_control()
            if self._stop:
                break
            self._check_budget()
            round_index = self._next_round
            if self.config.max_rounds and round_index > self.config.max_rounds:
                return self._finish(
                    "max_rounds",
                    f"Stopped after {self.config.max_rounds} worker rounds.",
                    records,
                    last_worker,
                )
            self._check_budget(new_call=True)
            self._next_round = round_index + 1
            self._beat(f"round {round_index}: starting worker")
            if self.git_history:
                self._git_before = self.git_history.begin_round(round_index)

            worker_prompt = self._worker_prompt(round_index, last_worker, last_review)
            self.log(f"round {round_index}: worker running")
            reuse_worker = bool(self.worker_session_id) and not self._drop_worker_session
            turn = self._invoke(
                self.config.provider,
                worker_prompt,
                self.worker_session_id if reuse_worker else "",
                role="worker",
                index=round_index,
                model=self.config.model,
                extra_args=self.config.extra_args,
                read_only=False,
            )
            if turn.session_id:
                self.worker_session_id = turn.session_id
            elif not self.worker_session_id:
                self.log(f"round {round_index}: no worker session id; the next round starts a new session")
            verdict = self._verdict_from_turn(turn, role="worker")
            self._rounds_since_review += 1
            tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
            reopened = self.audit.invalidate(tree)
            if reopened:
                self._event("audit_issues_reopened", issues=reopened, tree=tree)
            invalidated = self.modules.invalidate(tree)
            if invalidated:
                self._event("modules_invalidated", modules=invalidated, tree=tree)
            failure = _failure_reason(turn, verdict)
            record = RoundRecord(
                index=round_index,
                role="worker",
                verdict=verdict,
                exit_code=turn.exit_code,
                timed_out=turn.timed_out,
                elapsed_s=turn.elapsed_s,
                session_id=self.worker_session_id,
                output_path=str(turn.output_path),
                tree=tree,
                failure=failure,
                interruption=turn.interruption,
            )
            records.append(record)
            if self.git_history:
                self._checkpoint(record, turn.text, self._git_before)
            if not turn.exit_code and not turn.timed_out and not turn.interruption and not verdict.parse_error:
                self._merge_modules(verdict)
            self._write_state(records, "running", stall, previous_gap_key, tree)
            self.log(
                f"round {round_index}: worker status={verdict.status} "
                f"gaps={len(verdict.gaps)} exit={turn.exit_code} elapsed={turn.elapsed_s:.1f}s "
                + " ".join(verdict.summary.split())[:200]
            )
            if self._stop:
                break

            if failure:
                category, recovery = classify(failure)
                self._event("agent_failure", role="worker", category=category, recovery=recovery)
                if recovery == "stop":
                    return self._finish("action_required", "Worker authentication failed; repair credentials then resume. " + failure, records, verdict)
                if recovery == "fresh_session" and self.config.provider.name == "paseo":
                    errors = self._close_delivered_sessions()
                    if errors:
                        raise RecoveryError("Cannot close invalid session: " + "; ".join(errors))
                    self.worker_session_id = ""
                worker_failures += 1
                # Keep a Paseo agent id. The daemon still owns that agent after
                # a local CLI failure, and the next round must send to it.
                if self.config.provider.name != "paseo":
                    self.worker_session_id = ""
                    self._drop_worker_session = True
                last_worker = verdict
                # A failed implementation attempt does not resolve audit gaps.
                # Carry the last review into the replacement worker session.
                self._write_state(records, "running", stall, previous_gap_key, tree)
                self.log(f"round {round_index}: worker failure {worker_failures}: {failure}")
                if _limit_reached(self.config.failure_limit, worker_failures):
                    return self._finish(
                        "failed",
                        f"Worker failed {worker_failures} times. Last failure: {failure}",
                        records,
                        verdict,
                    )
                self._retry_pause(worker_failures + (3 if recovery == "backoff" else 0), category + " worker failure")
                continue
            worker_failures = 0
            self._drop_worker_session = False

            if self._stop:
                break

            if self.config.no_review:
                worker_problems = acceptance_problems(
                    self.config.cwd,
                    verdict.evidence,
                    baseline=self._baseline_files,
                    ignore_dir=self.run_dir,
                ) if _accepted(verdict) else []
                if _accepted(verdict):
                    worker_problems.extend(self.modules.coverage(verdict))
                if _accepted(verdict) and not worker_problems and verdict.scope == "requirement" and self._delivery_checks(round_index, verdict, expected_tree=tree):
                    self.modules.accept(verdict, None, round_index, tree)
                    return self._finish(
                        "done",
                        verdict.summary or "Worker reported done.",
                        records,
                        verdict,
                    )
                if _accepted(verdict) and not worker_problems and verdict.scope == "module":
                    if self._delivery_checks(round_index, verdict, final=False, expected_tree=tree):
                        self.modules.accept(verdict, None, round_index, tree)
                        self._complete_module(verdict)
                        self.log(f"round {round_index}: module completed; continuing original requirement")
                if _accepted(verdict) and worker_problems:
                    verdict.status = "continue"
                    verdict.gaps.extend(worker_problems)
                last_worker = verdict
                last_review = None
                stall, previous_gap_key, previous_tree = self._track_stall(
                    stall, previous_gap_key, previous_tree, verdict, tree
                )
                self._write_state(records, "running", stall, previous_gap_key, tree)
                if _limit_reached(self.config.stall_limit, stall):
                    return self._finish(
                        "stalled",
                        "Worker repeated the same open gaps without changing the workspace.",
                        records,
                        verdict,
                    )
                if stall:
                    self.log(f"round {round_index}: same gaps and unchanged workspace, continuing")
                if stall or not _accepted(verdict):
                    self._retry_pause(stall + 1, "unfinished worker turn")
                continue

            periodic_review = bool(self.config.review_every and self._rounds_since_review >= self.config.review_every)
            if not _accepted(verdict) and not verdict.blockers and not periodic_review:
                # An unfinished worker should keep implementing. A completion
                # candidate (or an external blocker) is the review checkpoint.
                last_worker = verdict
                stall, previous_gap_key, previous_tree = self._track_stall(
                    stall, previous_gap_key, previous_tree, verdict, tree
                )
                self._write_state(records, "running", stall, previous_gap_key, tree)
                if _limit_reached(self.config.stall_limit, stall):
                    return self._finish("stalled", "Worker repeated the same open gaps without changing the workspace.", records, verdict)
                if stall:
                    self._retry_pause(stall, "unchanged worker gaps")
                continue

            if reviewer is None:
                return self._finish(
                    "failed",
                    "No reviewer is configured. Pass --reviewer, or --no-review to trust the worker.",
                    records,
                    verdict,
                )

            review_prompt = self._review_prompt(round_index, verdict, turn.output_path, tree, last_review)
            self._process_control()
            if self._stop:
                break
            review_tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
            if tree != review_tree:
                raise RuntimeError("Workspace changed after worker conclusion; worker evidence is stale")
            self._beat(f"round {round_index}: starting reviewer")
            self.log(f"round {round_index}: reviewer running")
            self._event("review_checkpoint", reason="completion" if _accepted(verdict) else ("blocker" if verdict.blockers else "periodic"),
                        rounds_since_review=self._rounds_since_review)
            review_session = "" if self.config.fresh_review else self.reviewer_session_id
            try:
                review_turn = self._invoke(
                    reviewer,
                    review_prompt,
                    review_session,
                    role="reviewer",
                    index=round_index,
                    model=self.config.reviewer_model or self.config.model,
                    extra_args=self.config.reviewer_extra_args,
                    read_only=True,
                )
            finally:
                self._beat(f"round {round_index}: checking reviewer writes")
                if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != review_tree:
                    raise RuntimeError(
                        "Workspace changed during read-only review. Stopped without accepting the review. "
                        "No review backup was created; inspect the changes before resuming."
                    )
            if review_turn.session_id and not self.config.fresh_review:
                self.reviewer_session_id = review_turn.session_id
            elif reviewer.name == "paseo" and review_turn.session_id:
                stopped = _paseo_stop(reviewer.binary, review_turn.session_id, self.config.cwd)
                archived = _paseo_archive(reviewer.binary, review_turn.session_id, self.config.cwd)
                if not stopped or not archived:
                    raise RuntimeError("Could not close Paseo reviewer; stop before starting another worker round.")
                self._active_reviewer_id = ""
            review = self._verdict_from_turn(review_turn, role="reviewer")
            if self.git_history and _accepted(review):
                git_problems = self.git_history.review_problems(review.evidence)
                if git_problems:
                    review.status = 'continue'
                    review.gaps.extend(git_problems)
                    review.next_actions.append('Review the exact worker Git checkpoint and cite its full hash in evidence.')
            if not review_turn.exit_code and not review_turn.timed_out and not review_turn.interruption and not review.parse_error:
                self._merge_modules(review)
            review_failure = _failure_reason(review_turn, review)
            if (not review_failure and not review.parse_error and not review_turn.interruption
                    and (_accepted(review) or review.gaps or review.next_actions or review.blockers or review.issue_resolutions)):
                # Provider/format failures must not replace the last business audit.
                try:
                    self._record_audit(review, verdict, round_index, review_tree)
                except AuditCapacityError as exc:
                    self._audit_feedback = review
                    capacity_record = RoundRecord(round_index, "reviewer", review, review_turn.exit_code,
                                                  review_turn.timed_out, review_turn.elapsed_s, review_turn.session_id,
                                                  str(review_turn.output_path), tree=tree)
                    records.append(capacity_record)
                    if self.git_history:
                        self._checkpoint(capacity_record, review_turn.text, self.git_history.last_worker['commit'])
                    raise BudgetExceeded(str(exc)) from exc
                except ValueError as exc:
                    review.status = "continue"
                    review.parse_error = "invalid audit closure"
                    review.gaps.append(f"Audit issue update rejected: {exc}")
                    review.next_actions.append("Preserve open audit IDs; the reviewer must close each issue with valid file evidence.")
                else:
                    self._audit_feedback = review
                    self._rounds_since_review = 0
            review_record = RoundRecord(
                index=round_index,
                role="reviewer",
                verdict=review,
                exit_code=review_turn.exit_code,
                timed_out=review_turn.timed_out,
                elapsed_s=review_turn.elapsed_s,
                session_id=review_turn.session_id,
                output_path=str(review_turn.output_path),
                tree=tree,
                failure=review_failure,
                interruption=review_turn.interruption,
            )
            records.append(review_record)
            if self.git_history:
                self._checkpoint(review_record, review_turn.text, self.git_history.last_worker['commit'])
            self._write_state(records, "running", stall, previous_gap_key, tree)
            self.log(
                f"round {round_index}: reviewer status={review.status} gaps={len(review.gaps)} "
                + " ".join(review.summary.split())[:200]
            )
            if self._stop:
                break

            if review_failure:
                category, recovery = classify(review_failure)
                self._event("agent_failure", role="reviewer", category=category, recovery=recovery)
                if recovery == "stop":
                    return self._finish("action_required", "Reviewer authentication failed; repair credentials then resume. " + review_failure, records, review)
                reviewer_failures += 1
                self.reviewer_session_id = ""
                last_worker = verdict
                last_review = Verdict(
                    status="continue",
                    summary=f"Reviewer failed: {review_failure}",
                    gaps=[f"The independent review did not finish: {review_failure}"],
                    next_actions=[
                        "Do not redo work only because the previous review failed. "
                        "Check the workspace against the requirement, fix anything still missing, and document the evidence."
                    ],
                    evidence=[review_failure],
                )
                review_record.verdict = last_review
                self._write_state(records, "running", stall, previous_gap_key, tree)
                self.log(f"round {round_index}: reviewer failure {reviewer_failures}: {review_failure}")
                if _limit_reached(self.config.failure_limit, reviewer_failures):
                    return self._finish(
                        "failed",
                        f"Reviewer failed {reviewer_failures} times. Last failure: {review_failure}",
                        records,
                        review,
                    )
                self._retry_pause(reviewer_failures + (3 if recovery == "backoff" else 0), category + " reviewer failure")
                continue
            reviewer_failures = 0

            if _accepted(review) and not _accepted(verdict):
                review.status = "continue"
                review.gaps.append("Worker has not explicitly confirmed completion and readiness for delivery.")
                review.next_actions.append("Resolve the worker's open questions or gaps, verify the deliverable, and emit a done conclusion with evidence.")
            problems = acceptance_problems(
                self.config.cwd,
                review.evidence,
                baseline=self._baseline_files,
                ignore_dir=self.run_dir,
            ) if _accepted(review) else []
            if _accepted(review):
                problems.extend(f"Worker completion evidence: {problem}" for problem in acceptance_problems(
                    self.config.cwd, verdict.evidence, baseline=self._baseline_files, ignore_dir=self.run_dir,
                ))
                problems.extend(self.modules.coverage(verdict, review))
                pending = self.audit.pending(None if verdict.scope == review.scope == "requirement"
                                             else set(verdict.module_ids) & set(review.module_ids))
                if pending:
                    problems.append("Unresolved audit issues: " + ", ".join(item["id"] for item in pending)
                                    + ". The reviewer must explicitly close each issue with evidence.")
            if _accepted(review) and not problems:
                if verdict.scope == "requirement" and review.scope == "requirement":
                    if self._delivery_checks(round_index, review, expected_tree=review_tree):
                        self.modules.accept(verdict, review, round_index, review_tree)
                        return self._finish(
                            "done",
                            review.summary or verdict.summary or "Reviewer accepted the work.",
                            records,
                            review,
                        )
                else:
                    joint_ids = set(verdict.module_ids) & set(review.module_ids)
                    if self._delivery_checks(round_index, review, final=False, expected_tree=review_tree, module_ids=joint_ids):
                        self.modules.accept(verdict, review, round_index, review_tree)
                        review.scope = "module"
                        review.module_ids = sorted(joint_ids)
                        self._complete_module(review)
                        self.log(f"round {round_index}: module accepted; continuing original requirement")
            if _accepted(review) and problems:
                review.status = "continue"
                review.gaps.extend(problems)
                review.next_actions.append("Make the required workspace change and cite a real file in evidence.")
                review_record.verdict = review
                self.log(f"round {round_index}: reviewer done rejected: {'; '.join(problems)}")
                self._event("review_rejected", reasons="; ".join(problems))

            last_worker = verdict
            last_review = review
            stall, previous_gap_key, previous_tree = self._track_stall(
                stall, previous_gap_key, previous_tree, review, tree
            )
            self._write_state(records, "running", stall, previous_gap_key, tree)
            if _limit_reached(self.config.stall_limit, stall):
                return self._finish(
                    "stalled",
                    "The same gaps survived consecutive reviews and the workspace did not change.",
                    records,
                    review,
                )
            if stall:
                self.log(
                    f"round {round_index}: same review gaps and unchanged workspace, "
                    "continuing because stall-limit is 0"
                    if self.config.stall_limit == 0
                    else f"round {round_index}: stall {stall}/{self.config.stall_limit}"
                )
            self._retry_pause(stall + 1, "unfinished review")

        return self._finish("stopped", f"Stopped: {self._stop_reason or 'stop requested'}.", records, last_worker)

    def _retry_pause(self, count: int, reason: str) -> None:
        if self._stop or (self.config.max_rounds and self._next_round > self.config.max_rounds):
            return
        delay = min(60, self.config.retry_delay_s * 2 ** min(max(count - 1, 0), 6))
        if delay > 0:
            self.log(f"retrying in {delay:g}s: {reason}")
            self._beat(f"retrying in {delay:g}s: {reason}")
            remaining = self.config.max_runtime_s - self._elapsed() if self.config.max_runtime_s else delay
            deadline = time.monotonic() + max(0, min(delay, remaining))
            while time.monotonic() < deadline and not self._stop:
                self._stop_event.wait(min(0.2, max(0, deadline - time.monotonic())))
                self._process_control(active=True)
            self._check_budget()
            self._process_control()

    def _elapsed(self) -> float:
        return self._elapsed_before + time.monotonic() - self._started

    def _should_stop(self) -> bool:
        self._process_control(active=True)
        if self.config.max_run_bytes and time.monotonic() - self._last_space_check >= 1:
            self._last_space_check = time.monotonic()
            size = 0
            for parent, dirs, files in os.walk(self.run_dir, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
                for name in files:
                    try:
                        size += (Path(parent) / name).lstat().st_size
                    except FileNotFoundError:
                        pass
            self._space_exceeded = size >= self.config.max_run_bytes
        return self._stop or self._space_exceeded or bool(self.config.max_runtime_s and self._elapsed() >= self.config.max_runtime_s)

    def _event(self, kind: str, **details) -> None:
        record_event(self.run_dir, kind, **details)

    def _process_control(self, active: bool = False) -> None:
        path = self.run_dir / "control.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        action = value.get("action")
        if action == "stop":
            self._stop = self._stop_requested = True
            self._stop_reason = "stop command"
            self._stop_event.set()
        elif action == "pause" and not active:
            self._event("paused")
            while not self._stop:
                self._check_budget()
                self._beat("paused at a turn boundary")
                self._stop_event.wait(0.2)
                try:
                    action = json.loads(path.read_text(encoding="utf-8")).get("action")
                except FileNotFoundError:
                    break
                if action == "stop":
                    self._process_control(active=True)
                elif action != "pause":
                    break
            self._event("pause_ended", stopped=self._stop)

    def _check_budget(self, *, new_call: bool = False) -> None:
        if self._space_exceeded:
            raise BudgetExceeded("Run directory storage budget exhausted")
        if self.config.max_runtime_s and self._elapsed() >= self.config.max_runtime_s:
            raise BudgetExceeded("Total active runtime budget exhausted")
        if new_call and self.config.max_agent_calls and self._agent_calls >= self.config.max_agent_calls:
            raise BudgetExceeded("Agent call budget exhausted")

    def _usage(self) -> dict:
        return {"elapsed_s": round(self._elapsed(), 3), "agent_calls": self._agent_calls,
                "max_runtime_s": self.config.max_runtime_s, "max_agent_calls": self.config.max_agent_calls,
                "max_output_bytes": self.config.max_output_bytes, "recorded_at": datetime.now(timezone.utc).isoformat(),
                "memory_bytes": self.config.memory_bytes, "file_bytes": self.config.file_bytes, "max_run_bytes": self.config.max_run_bytes}

    def _review_schedule(self) -> dict:
        return {"enabled": not self.config.no_review, "every_worker_rounds": self.config.review_every,
                "rounds_since_review": self._rounds_since_review}

    def _record_audit(self, review: Verdict, worker: Verdict, index: int, tree: str) -> None:
        modules = sorted(set(worker.module_ids) & self.modules.items.keys()) if worker.scope == "module" else []
        findings = _audit_findings(review)
        open_before = {item["id"] for item in self.audit.pending()}
        def check_evidence(evidence):
            return acceptance_problems(self.config.cwd, evidence, baseline=self._baseline_files, ignore_dir=self.run_dir)
        reported, closed = self.audit.update(findings, review.issue_resolutions, index, tree, modules, check_evidence)
        if reported:
            self._event("audit_issues_reported", issues=reported)
        if closed:
            self._event("audit_issues_resolved", issues=closed, tree=tree)
            if open_before.intersection(closed) - {item["id"] for item in self.audit.pending()}:
                self._progress_reset = True

    def _checkpoint(self, record: RoundRecord, raw: str, before: str) -> None:
        data = RunReport('', 0, '', '', [record]).to_dict()['records'][0]
        data['requirement'] = self.config.requirement
        data['acceptance'] = self.config.acceptance
        data['directions'] = [item for item in self.directions.messages()
                              if item['id'] in self._direction_ids[record.role]]
        record.git_checkpoint = self.git_history.record(record.index, record.role, data, before=before, raw=raw)
        self._event('git_checkpoint', role=record.role, round=record.index,
                    commit=record.git_checkpoint['commit'], ref=self.git_history.ref)

    def _merge_modules(self, verdict: Verdict) -> None:
        try:
            for item in verdict.modules:
                if isinstance(item, dict) and item.get("checks") and (item.get("id") not in self.modules.items or item["checks"] != self.modules.items[item["id"]].get("checks")):
                    raise ValueError("Executable module checks must be supplied by the user in --modules, not by an agent")
            self.modules.merge(verdict.modules)
        except ValueError as exc:
            verdict.status = "continue"
            verdict.parse_error = "invalid module plan"
            verdict.gaps.append(f"Module plan rejected: {exc}")

    def _provider_config(self) -> dict:
        def identity(provider):
            if provider is None:
                return None
            templates = [getattr(provider, "start_template", ""), getattr(provider, "resume_template", "")]
            return {"name": provider.name, "binary": provider.binary, "mode": provider.mode,
                    "backend": getattr(provider, "backend", ""),
                    "command_hash": hashlib.sha256(json.dumps(templates).encode()).hexdigest()}
        return {"worker": identity(self.config.provider), "reviewer": identity(self.config.reviewer) if not self.config.no_review else None,
                "no_review": self.config.no_review, "review_isolation": self.config.review_isolation,
                "worker_isolation": self.config.worker_isolation, "protect_paths": self.config.protect_paths,
                "worker_args_hash": hashlib.sha256(json.dumps(self.config.extra_args).encode()).hexdigest(),
                "reviewer_args_hash": hashlib.sha256(json.dumps(self.config.reviewer_extra_args).encode()).hexdigest()}

    def _recover_interrupted_run(self) -> None:
        """Quiesce old local and daemon work before starting another turn."""
        try:
            path = self.run_dir / "process.json"
            if path.exists():
                result = recover_process(json.loads(path.read_text(encoding="utf-8")))
                path.unlink()
                self.log(f"resume process recovery: {result}")
            sessions = [("worker", self.config.provider, self.worker_session_id)]
            if self.config.reviewer:
                sessions.append(("reviewer", self.config.reviewer, self._active_reviewer_id or self.reviewer_session_id))
            for role, provider, session_id in sessions:
                if provider.name != "paseo" or not session_id:
                    continue
                self._beat(f"recovering previous {role} agent {session_id}")
                observed = run_command(PreparedCommand([provider.binary, "inspect", "--json", session_id]), self.config.cwd,
                                       timeout_s=15, max_output_bytes=self.config.max_output_bytes)
                if observed.exit_code or observed.interruption:
                    raise RuntimeError(f"Cannot inspect previous {role} agent {session_id}")
                state = json.loads(observed.stdout)
                status = str(state.get("status") or state.get("Status") or "").lower()
                if not status:
                    raise RuntimeError(f"Missing status for previous {role} agent {session_id}")
                if status not in {"stopped", "archived"} and not _paseo_stop(provider.binary, session_id, self.config.cwd):
                    raise RuntimeError(f"Cannot stop previous {role} agent {session_id}")
                if role == "reviewer":
                    if status != "archived" and not _paseo_archive(provider.binary, session_id, self.config.cwd):
                        raise RuntimeError(f"Cannot archive previous reviewer {session_id}")
                    self._active_reviewer_id = self.reviewer_session_id = ""
                elif status == "archived":
                    self.worker_session_id = ""
        except (OSError, ValueError, TypeError, RuntimeError, AttributeError) as exc:
            raise RecoveryError(f"Resume recovery failed: {exc}. No new agent was started.") from exc

    def _track_stall(
        self,
        stall: int,
        previous_key: str,
        previous_tree: str,
        verdict: Verdict,
        tree: str,
    ) -> tuple[int, str, str]:
        key = "\n".join(sorted(verdict.gaps))
        changed = bool(previous_tree) and tree != previous_tree
        progress = changed or self._progress_reset
        self._no_progress = self._no_progress + 1 if previous_tree and not progress else 0
        self._progress_reset = False
        count = stall + 1 if key and key == previous_key and not progress else 0
        recovery_stall = max(count, self._no_progress)
        if recovery_stall:
            threshold = self.config.restart_after_stall
            if threshold and recovery_stall % threshold == 0 and not _limit_reached(self.config.stall_limit, count):
                errors = self._close_delivered_sessions()
                if errors:
                    raise RuntimeError("Cannot restart stalled agents: " + "; ".join(errors))
                self.worker_session_id = ""
                self.reviewer_session_id = ""
                self._active_reviewer_id = ""
                self._recovery_count += 1
                self._event("stagnation_recovery", count=self._recovery_count)
                self._recovery_note = (
                    f"No workspace progress after {recovery_stall} turns. Start a new diagnosis: "
                    "inspect the current failure, identify the cause, and try a different concrete approach. "
                    "Preserve completed work. Do not repeat the previous plan without new evidence."
                )
                self.log(f"stagnation recovery: fresh agent sessions after {recovery_stall} turns without progress")
        if changed:
            self._recovery_note = ""
        return count, key, tree

    def _delivery_checks(self, index: int, verdict: Verdict, *, final: bool = True, expected_tree: str = "",
                         module_ids: set[str] | None = None) -> bool:
        """Execute user-selected checks independently of the agents' claims."""
        if self._pending_directions():
            verdict.status = 'continue'
            verdict.gaps.append('New human instructions arrived after the worker started. Implement and review them before accepting delivery.')
            return False
        before = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        if expected_tree and before != expected_tree:
            self.modules.invalidate(before)
            self.audit.invalidate(before)
            verdict.status = "continue"
            verdict.gaps.append("Workspace changed after the conclusion; acceptance evidence is stale. Review the current source again.")
            self._event("stale_evidence", expected=expected_tree, actual=before)
            return False
        commands = list(self.config.checks) if final else []
        ids = set(verdict.module_ids) if module_ids is None else set(module_ids)
        if final:
            ids.update(key for key, item in self.modules.items.items() if item["status"] == "accepted")
        commands.extend(self.modules.checks_for(ids))
        failures = []
        for number, command in enumerate(dict.fromkeys(commands), 1):
            if self._stop:
                verdict.status = "continue"
                return False
            self._beat(f"round {index}: acceptance check {number}")
            completed = False
            live_check = LiveOutput(self.run_dir, 'check', index, '验收检查')
            live_check.emit({'kind': 'command', 'text': redact(command), 'status': 'running'})
            try:
                result = run_command(
                    PreparedCommand(["sh", "-lc", command]), self.config.cwd,
                    timeout_s=self.config.check_timeout_s,
                    detect_input_requests=False,
                    should_stop=self._should_stop, on_child=self._remember_child,
                    max_output_bytes=self.config.max_output_bytes,
                    memory_bytes=self.config.memory_bytes, file_bytes=self.config.file_bytes,
                    heartbeat=self._heartbeat, heartbeat_note=f"round {index}: acceptance check {number}",
                    on_output=live_check.feed,
                )
                completed = True
            finally:
                live_check.close()
                self._forget_child(completed=completed)
            self._check_budget()
            path = self.run_dir / f"{index:03d}-check-{number:02d}.log"
            path.write_text(
                f"$ {redact(command)}\nexit={result.exit_code} timed_out={result.timed_out} interruption={result.interruption or '-'}\n"
                + clipped(redact(result.stdout[-16000:]), 16000) + "\n" + clipped(redact(result.stderr[-8000:]), 8000), encoding="utf-8",
            )
            if result.exit_code or result.timed_out or result.interruption:
                failures.append(f"Acceptance check failed: {command} (exit={result.exit_code}, interruption={result.interruption or '-'}). See {path}.")
                verdict.evidence.append(clipped((result.stderr or result.stdout)[-2000:], 2000))
            self.log(f"acceptance check {number}: {'failed' if result.exit_code or result.timed_out or result.interruption else 'passed'}")
            self._event("check_finished", command=command, exit=result.exit_code, tree=before)
        after = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        if after != before:
            self.modules.invalidate(after)
            self.audit.invalidate(after)
            failures.append("Acceptance commands changed workspace source content. Previous review and evidence are stale; review the new content before accepting.")
        if failures:
            verdict.status = "continue"
            verdict.gaps.extend(failures)
            verdict.next_actions.append("Fix the failed acceptance checks. Do not weaken or bypass their assertions; the monitor will rerun them before delivery.")
            return False
        self._delivery_tree = before
        if self._pending_directions():
            verdict.status = 'continue'
            verdict.next_actions.append('Read the new human instructions before the next delivery attempt.')
            return False
        return not self._stop

    def _pending_directions(self) -> bool:
        return any(item['id'] not in self._direction_ids['worker'] for item in self.directions.messages())

    def _verdict_from_turn(self, turn: "_CompletedTurn", *, role: str) -> Verdict:
        verdict = parse_verdict(turn.text, structured=turn.structured)
        if turn.interruption == "input request":
            verdict.status = "continue"
            verdict.gaps.append("The agent was waiting for input or confirmation. Continue the authorized task without waiting for a reply.")
            return verdict
        if turn.timed_out:
            return Verdict(
                status="continue",
                summary=f"{role.title()} turn timed out ({turn.interruption or 'turn limit'}).",
                gaps=["The previous turn exceeded the turn timeout before finishing."],
                next_actions=["Continue from the current workspace state and finish the open work."],
                evidence=[f"timeout after {turn.elapsed_s:.0f}s"],
                raw=turn.text,
            )
        if turn.exit_code != 0:
            detail = extract_error(turn.stderr, turn.text) or f"exit {turn.exit_code}"
            return Verdict(
                status="continue",
                summary=f"{role.title()} process exited {turn.exit_code}.",
                gaps=[f"{role} process failed: {detail}"],
                next_actions=["Inspect the failure, then continue the unfinished work in a fresh session."],
                evidence=[detail],
                raw=turn.text,
                parse_error=f"exit {turn.exit_code}",
            )
        if verdict.status == "done" and not verdict.evidence:
            verdict.status = "continue"
            verdict.parse_error = "missing completion evidence"
            verdict.gaps.append("Done was reported without evidence.")
            verdict.next_actions.append("Cite the files, commands, or observations that show the requirement is met.")
        if verdict.status == "blocked":
            verdict.status = "continue"
            verdict.gaps.extend(f"Unresolved dependency: {blocker}" for blocker in verdict.blockers)
            verdict.next_actions.append(
                "Continue all work possible within the authorized scope, investigate alternatives, "
                "and recheck unavailable dependencies. Do not invent credentials or claim incomplete work is deliverable."
            )
        if not verdict.parse_error and not turn.interruption:
            self.directions.receipt(self._direction_ids[role], role, max(1, self._next_round - 1), responded=True)
        return verdict

    def _finish(
        self,
        status: str,
        summary: str,
        records: list[RoundRecord],
        verdict: Verdict | None,
    ) -> RunReport:
        with self.directions.lock():
            return self._finish_locked(status, summary, records, verdict)

    def _finish_locked(self, status: str, summary: str, records: list[RoundRecord],
                       verdict: Verdict | None) -> RunReport:
        if status == 'done' and self._pending_directions():
            status, summary = 'action_required', 'New human instructions arrived during delivery. Resume to implement them.'
        self._prune_history(records)
        if status != "done":
            errors = self._stop_external_agent()
            if errors:
                summary += " Agent shutdown errors: " + "; ".join(errors)
                status = "recovery_failed"
        if status == "done":
            self._check_budget()
            current = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
            if current != self._delivery_tree:
                raise RuntimeError("Workspace changed since final acceptance; refusing stale delivery")
            self._no_progress = 0
            cleanup_errors = self._close_delivered_sessions()
            if cleanup_errors:
                status = "failed"
                summary = "Work accepted, but agent cleanup failed: " + "; ".join(cleanup_errors)
            if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != self._delivery_tree:
                raise RuntimeError("Workspace changed while closing agents; refusing stale delivery")
        report = RunReport(
            status=status,
            rounds=self._pruned_worker_rounds + sum(1 for record in records if record.role == "worker"),
            summary=summary,
            stop_reason=status,
            records=records,
            session_id=self.worker_session_id,
            reviewer_session_id=self.reviewer_session_id,
            git_history=self.git_history.export() if self.git_history else {'enabled': False},
        )
        if verdict and verdict.blockers and status == "blocked":
            report.summary = summary + " Blockers: " + "; ".join(verdict.blockers)
        path = self.run_dir / "report.json"
        self._event("run_finished", status=status, summary=summary)
        if status in {"failed", "recovery_failed", "action_required", "budget_exceeded", "stalled"}:
            write_json(self.run_dir / "alert.json", safe({"status": status, "summary": summary,
                       "at": datetime.now(timezone.utc).isoformat(), "run_dir": str(self.run_dir)}), durable=True)
        write_json(path, safe({**report.to_dict(), "modules": self.modules.export(), "usage": self._usage(),
                                         "no_progress_turns": self._no_progress, "recovery_count": self._recovery_count, "accepted_tree": self._delivery_tree,
                                         "review_schedule": self._review_schedule(),
                                         "audit_issues": self.audit.export(),
                                         "history": self._history_info()}), durable=True)
        self._write_state(records, status, 0, "", records[-1].tree if records else "")
        self._beat(f"finished: {status}")
        self.log(f"finished: {status} after {report.rounds} worker rounds")
        self.log(f"report: {path}")
        return report

    def _close_delivered_sessions(self) -> list[str]:
        """Close task-owned daemon agents before declaring delivery complete."""
        sessions = [("worker", self.config.provider, self.worker_session_id)]
        if self.config.reviewer:
            sessions.append(("reviewer", self.config.reviewer, self._active_reviewer_id or self.reviewer_session_id))
        errors = []
        seen: set[tuple[str, str]] = set()
        for role, provider, session_id in sessions:
            if provider.name != "paseo" or not session_id or (provider.binary, session_id) in seen:
                continue
            seen.add((provider.binary, session_id))
            self._beat(f"closing delivered {role} session {session_id}")
            stopped = _paseo_stop(provider.binary, session_id, self.config.cwd)
            archived = _paseo_archive(provider.binary, session_id, self.config.cwd)
            if not stopped or not archived:
                errors.append(f"{role} {session_id}: stop={stopped} archive={archived}")
        return errors

    def _complete_module(self, verdict: Verdict) -> None:
        self._event("module_accepted", modules=verdict.module_ids, tree=self._delivery_tree)
        errors = self._close_delivered_sessions()
        if errors:
            raise RuntimeError("Module accepted, but agent cleanup failed: " + "; ".join(errors))
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != self._delivery_tree:
            raise RuntimeError("Workspace changed while closing module agents; revalidation required")
        # Each accepted module ends its agent sessions. The monitor and original
        # requirement stay alive, with accepted milestones supplied to the next turn.
        self.worker_session_id = ""
        self.reviewer_session_id = ""
        self._active_reviewer_id = ""
        self._drop_worker_session = False
        self._recovery_note = ""
        self._no_progress = 0
        self._progress_reset = True
        _continue_remaining_modules(verdict)

    def _write_state(
        self,
        records: list[RoundRecord],
        status: str,
        stall: int,
        gap_key: str,
        tree: str,
    ) -> None:
        self._prune_history(records)
        baseline_path = self.run_dir / "baseline.json.gz"
        if not self._baseline_saved:
            baseline_tmp = baseline_path.with_suffix(".gz.tmp")
            with gzip.open(baseline_tmp, "wt", encoding="utf-8") as handle:
                json.dump(getattr(self, "_baseline_files", {}), handle, ensure_ascii=False, separators=(",", ":"))
            with baseline_tmp.open("rb") as handle:
                os.fsync(handle.fileno())
            baseline_tmp.replace(baseline_path)
            self._baseline_saved = True
        last_worker = next((record.verdict for record in reversed(records) if record.role == "worker"), None)
        # Audit feedback outlives individual worker turns and the detail window.
        # Keep one current conclusion, including after pruning and before resume.
        last_review = next((record.verdict for record in reversed(records) if record.role == "reviewer"), self._review_feedback)
        self._review_feedback = last_review
        state = {
            "cwd": str(self.config.cwd),
            "state_version": 1,
            "directions_version": 1,
            "git_history": self.git_history.export() if self.git_history else {'enabled': False},
            "baseline_file": baseline_path.name,
            "baseline_count": len(getattr(self, "_baseline_files", {})),
            "acceptance": self.config.acceptance,
            "checks": self.config.checks,
            "execution_settings": {key: getattr(self.config, key) for key in _EXECUTION_DEFAULTS},
            "protect_paths": self.config.protect_paths,
            "recovery_note": self._recovery_note,
            "no_progress_turns": self._no_progress,
            "progress_pending": self._progress_reset,
            "recovery_count": self._recovery_count,
            "provider_config": self._provider_config(),
            "modules": self.modules.export(),
            "audit_issues": self.audit.export(),
            "usage": self._usage(),
            "status": status,
            "session_id": self.worker_session_id,
            "reviewer_session_id": self.reviewer_session_id,
            "active_reviewer_session_id": self._active_reviewer_id,
            "next_round": self._next_round,
            "stall": stall,
            "gap_key": gap_key,
            "tree": tree,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_worker": safe(last_worker.to_dict()) if last_worker else None,
            "last_review": safe(last_review.to_dict()) if last_review else None,
            "last_audit": safe(self._audit_feedback.to_dict()) if self._audit_feedback else None,
            "review_schedule": self._review_schedule(),
            "rounds": safe(RunReport(status, 0, "", "", records).to_dict()["records"]),
            "history": self._history_info(),
        }
        target = self.run_dir / "state.json"
        write_json(target, state, durable=True)

    def _history_info(self) -> dict:
        return {"keep_rounds": self.config.keep_rounds, "pruned_worker_rounds": self._pruned_worker_rounds,
                "pruned_records": self._pruned_records, "failure_prefix": self._pruned_failures}

    def _prune_history(self, records: list[RoundRecord]) -> None:
        if not self.config.keep_rounds or not records:
            return
        first = max(record.index for record in records) - self.config.keep_rounds + 1
        removed = [record for record in records if record.index < first]
        for record in removed:
            self._pruned_failures[record.role] = self._pruned_failures.get(record.role, 0) + 1 if record.failure else 0
            self._pruned_worker_rounds += record.role == "worker"
        self._pruned_records += len(removed)
        records[:] = [record for record in records if record.index >= first]
        files = prune_files(self.run_dir, first)
        if removed or files:
            self._event("history_pruned", records=len(removed), files=files, first_round=first)

    def _failure_count(self, records: list[RoundRecord], role: str) -> int:
        count = _consecutive_failures(records, role)
        if count == sum(record.role == role for record in records):
            count += self._pruned_failures.get(role, 0)
        return count

    def _load_resume(
        self,
    ) -> tuple[list[RoundRecord], int, str, str, int, int, Verdict | None, Verdict | None] | None:
        path = self.run_dir / "state.json"
        if not path.exists():
            raise ValueError("state.json is missing")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state.json must contain an object")
        if state.get("state_version", 1) != 1:
            raise ValueError("Unsupported state version; use a compatible auto-moniter release")
        if state.get("cwd") and Path(state["cwd"]).resolve() != self.config.cwd:
            raise ValueError("saved workspace does not match --cwd")
        status = str(state.get("status") or "")
        if status and status not in {"running", "stopped", "blocked", "budget_exceeded", "recovery_failed", "action_required"}:
            self.log(f"resume refused: run already finished with status {status}")
            self._resume_refused = status
            return None
        if "provider_config" in state:
            saved_config = dict(state["provider_config"])
            saved_config.setdefault("review_isolation", "provider")
            saved_config.setdefault("worker_isolation", "provider")
            saved_config.setdefault("protect_paths", [])
            if saved_config != self._provider_config():
                raise ValueError("saved worker/reviewer providers, backends, modes, or command templates do not match; resume with the original configuration")
        self.worker_session_id = str(state.get("session_id") or "")
        self.reviewer_session_id = str(state.get("reviewer_session_id") or "")
        self._active_reviewer_id = str(state.get("active_reviewer_session_id") or "")
        self._no_progress = int(state.get("no_progress_turns") or 0)
        self._progress_reset = state.get("progress_pending", False)
        if type(self._progress_reset) is not bool:
            raise ValueError("invalid saved progress marker")
        self._recovery_count = int(state.get("recovery_count") or 0)
        history = state.get("history") or {}
        if self.config.keep_rounds is None:
            self.config.keep_rounds = int(history.get("keep_rounds", 100))
        if self.config.keep_rounds < 0:
            raise ValueError("invalid saved history retention")
        self._pruned_worker_rounds = int(history.get("pruned_worker_rounds") or 0)
        self._pruned_records = int(history.get("pruned_records") or 0)
        self._pruned_failures = {role: int((history.get("failure_prefix") or {}).get(role) or 0) for role in ("worker", "reviewer")}
        if min(self._pruned_worker_rounds, self._pruned_records, *self._pruned_failures.values()) < 0:
            raise ValueError("invalid saved history counters")
        saved_acceptance = str(state.get("acceptance") or "")
        if "acceptance" in state and self.config.acceptance and self.config.acceptance != saved_acceptance:
            raise ValueError("acceptance criteria cannot change on resume; use the saved criteria or a new run")
        if not self.config.acceptance:
            self.config.acceptance = saved_acceptance
        schedule = state.get("review_schedule") or {}
        if not isinstance(schedule, dict):
            raise ValueError("invalid saved review schedule")
        if self.config.review_every is None:
            self.config.review_every = schedule.get("every_worker_rounds", 5)
        self._rounds_since_review = schedule.get("rounds_since_review", 0)
        if (not isinstance(self.config.review_every, int) or self.config.review_every < 0
                or not isinstance(self._rounds_since_review, int) or self._rounds_since_review < 0):
            raise ValueError("invalid saved review schedule")
        saved_checks = state.get("checks") or []
        settings = state.get("execution_settings", {})
        if not isinstance(settings, dict) or set(settings) - _EXECUTION_DEFAULTS.keys():
            raise ValueError("invalid saved execution settings")
        for key, value in settings.items():
            if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                    or (key in {"check_timeout_s", "max_output_bytes"} and value == 0)
                    or (key == "max_output_bytes" and type(value) is not int)):
                raise ValueError(f"invalid saved execution setting: {key}")
            if key not in self._execution_overrides:
                setattr(self.config, key, value)
        if not isinstance(saved_checks, list) or not all(isinstance(item, str) for item in saved_checks):
            raise ValueError("invalid saved acceptance checks")
        if self.config.checks and self.config.checks != saved_checks:
            raise ValueError("acceptance checks cannot change on resume; use the saved checks or a new run")
        self.config.checks = saved_checks
        self._recovery_note = str(state.get("recovery_note") or "")
        usage = state.get("usage") or {}
        self._elapsed_before = float(usage.get("elapsed_s") or 0)
        try:
            heartbeat = json.loads(self._heartbeat.read_text(encoding="utf-8"))
            last_tick = datetime.fromisoformat(heartbeat["at"])
            recorded = datetime.fromisoformat(usage["recorded_at"])
            self._elapsed_before += max(0, (last_tick - recorded).total_seconds())
        except (OSError, ValueError, TypeError, KeyError):
            pass
        self._agent_calls = int(usage.get("agent_calls") or 0)
        for name in ("memory_bytes", "file_bytes", "max_run_bytes"):
            if not getattr(self.config, name):
                setattr(self.config, name, int(usage.get(name) or 0))
            if getattr(self.config, name) < 0:
                raise ValueError(f"Invalid saved {name}")
        if not self.config.max_runtime_s:
            self.config.max_runtime_s = float(usage.get("max_runtime_s") or 0)
        if not self.config.max_agent_calls:
            self.config.max_agent_calls = int(usage.get("max_agent_calls") or 0)
        if (not math.isfinite(self._elapsed_before) or self._elapsed_before < 0 or self._agent_calls < 0
                or not math.isfinite(self.config.max_runtime_s) or self.config.max_runtime_s < 0 or self.config.max_agent_calls < 0):
            raise ValueError("invalid saved resource usage")
        if "modules" in state:
            saved_modules = ModuleLedger.restore(state["modules"])
            for item in self.config.modules:
                saved = saved_modules.items.get(item["id"])
                if saved is None or any(saved.get(key, []) != item.get(key, []) for key in ("title", "acceptance", "checks", "depends_on") if key in item):
                    raise ValueError("module definitions cannot change on resume")
            self.modules = saved_modules
        self._next_round = int(state.get("next_round") or 1)
        if self._next_round < 1:
            raise ValueError("next_round must be positive")
        baseline = state.get("baseline_files")
        if "baseline_file" in state:
            if state["baseline_file"] != "baseline.json.gz":
                raise ValueError("invalid baseline_file in state.json")
            try:
                with gzip.open(self.run_dir / "baseline.json.gz", "rt", encoding="utf-8") as handle:
                    baseline = json.load(handle)
            except (OSError, EOFError, ValueError) as exc:
                raise ValueError(f"Cannot load saved baseline: {exc}") from exc
            if not isinstance(baseline, dict):
                raise ValueError("invalid saved baseline")
            self._baseline_saved = True
        if baseline is not None:
            if not isinstance(baseline, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in baseline.items()):
                raise ValueError("invalid baseline_files in state.json")
            self._baseline_files = baseline
        records = [_record_from_state(item) for item in state.get("rounds") or []]
        latest_worker = next((record for record in reversed(records) if record.role == "worker"), None)
        if latest_worker and latest_worker.failure and self.config.provider.name != "paseo":
            self.worker_session_id = ""
            self._drop_worker_session = True
        latest_review = next((record for record in reversed(records) if record.role == "reviewer"), None)
        if latest_review and latest_review.failure:
            self.reviewer_session_id = ""
        last_worker = _verdict_from_state(state.get("last_worker"))
        last_review = _verdict_from_state(state.get("last_review"))
        self._review_feedback = last_review
        self._audit_feedback = _verdict_from_state(state.get("last_audit", state.get("last_review")))
        if "audit_issues" in state:
            self.audit = AuditLedger.restore(state["audit_issues"])
            if any(set(item["module_ids"]) - self.modules.items.keys() for item in self.audit.items):
                raise ValueError("Saved audit issue names unknown modules")
        elif self._audit_feedback and not any(text.startswith("Module accepted: ") for text in self._audit_feedback.done):
            findings = _audit_findings(self._audit_feedback)
            self.audit.record(findings, max(0, self._next_round - 1))
        return (
            records,
            int(state.get("stall") or 0),
            str(state.get("gap_key") or ""),
            str(state.get("tree") or ""),
            self._failure_count(records, "worker"),
            self._failure_count(records, "reviewer"),
            last_worker,
            last_review,
        )

    def _worker_prompt(self, index: int, last_worker: Verdict | None, last_review: Verdict | None) -> str:
        snapshot = workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        if index == 1 and last_worker is None and last_review is None:
            parts = [
                "You are the implementation agent in an unattended development loop.",
                "A person stated the requirement below. Implement it in the working directory.",
                "Work through the original requirement module by module. A completed module uses scope=module; "
                "only completion of ALL original modules and their integration uses scope=requirement.",
                "Do the work yourself with tools. Do not ask the person to continue, confirm, or choose among reasonable options.",
                "The person authorized continuous work on this requirement. Resolve routine choices yourself within that scope.",
                "If you asked a question or requested confirmation, continue immediately using reasonable defaults; no reply is needed.",
                "A blocker does not end monitoring: keep doing independent work and recheck the dependency without inventing access.",
                "After you finish this turn, keep looking for missing behavior, tests, edge cases, and incomplete wiring.",
                "If anything required is still missing, status must be continue and gaps must name it.",
                "",
                "Requirement:",
                self.config.requirement.strip(),
            ]
            if self.config.acceptance.strip():
                parts.extend(["", "Acceptance checks:", self.config.acceptance.strip()])
            if self.config.checks:
                parts.extend(["", "Monitor acceptance commands (must pass before final delivery):", json.dumps(self.config.checks, ensure_ascii=False)])
            parts.extend(["", "Monitor module ledger (pending modules must not be omitted):", json.dumps(self.modules.export(), ensure_ascii=False)])
            parts.extend(["", "Open audit issues (worker fixes; only reviewer closes IDs):", json.dumps(self.audit.prompt(), ensure_ascii=False)])
            parts.extend(["", "Workspace snapshot:", snapshot, "", verdict_instructions()])
            return "\n".join(parts)

        review_block = "No independent review yet."
        if last_review:
            review_block = json.dumps(last_review.to_dict(), ensure_ascii=False, indent=2)
        own_block = json.dumps(last_worker.to_dict(), ensure_ascii=False, indent=2) if last_worker else "{}"
        return "\n".join(
            [
                "Continue the same task. The supervisor rejected idle completion.",
                "If the previous module passed review, preserve it and implement the next unfinished module "
                "from the original requirement. Recheck the whole original requirement for remaining modules; "
                "a module milestone is not completion of the entire requirement.",
                "This is a fresh session if you do not remember earlier edits. Read the workspace before changing it.",
                "Close every open gap. Add the missing behavior, tests, and checks.",
                "Make concrete progress or verify the deliverable; avoid unnecessary edits to already completed work.",
                "If the review is wrong, fix the evidence and say why in summary. If it is right, do the work.",
                "A person is not available. Decide and proceed.",
                "Continue the authorized task now. Questions or requests for permission to continue do not suspend this task.",
                "If a dependency is unavailable, complete independent work, investigate alternatives, and retry it. Never fabricate credentials or successful checks.",
                "",
                "Original requirement:",
                self.config.requirement.strip(),
                "",
                "Accepted module milestones (preserve this work):",
                self._module_progress(),
                "Monitor module ledger (pending modules must not be omitted):",
                json.dumps(self.modules.export(), ensure_ascii=False),
                "",
                "Acceptance checks:",
                self.config.acceptance.strip() or "(none beyond the requirement)",
                "Monitor acceptance commands:",
                json.dumps(self.config.checks, ensure_ascii=False),
                "Recovery guidance:",
                self._recovery_note or "(none)",
                "",
                "Your previous conclusion:",
                own_block,
                "",
                "Last worker response (may contain an unanswered question):",
                self._last_worker_text(last_worker),
                "",
                "Independent review:",
                review_block,
                "",
                "Last completed business audit (retained across reviewer failures):",
                self._audit_context(last_review),
                "",
                "Open audit issues (worker fixes; only reviewer closes IDs):",
                json.dumps(self.audit.prompt(), ensure_ascii=False),
                "",
                "Workspace snapshot:",
                snapshot,
                "",
                verdict_instructions(),
            ]
        )

    def _audit_context(self, latest: Verdict | None) -> str:
        if self._audit_feedback is None:
            return "(no completed business audit)"
        audit = self._audit_feedback.to_dict()
        if latest is not None and audit == latest.to_dict():
            return "(same as the previous independent review above)"
        return json.dumps(audit, ensure_ascii=False, indent=2)

    def _last_worker_text(self, last_worker: Verdict | None) -> str:
        if last_worker and last_worker.raw:
            return last_worker.raw[-8000:]
        path = self.run_dir / f"{self._next_round - 2:03d}-worker-text.md"
        return path.read_text(encoding="utf-8")[-8000:] if path.exists() else "(no earlier response)"

    def _module_progress(self) -> str:
        role = "worker" if self.config.no_review else "reviewer"
        milestones = [
            {"round": record.index, "summary": record.verdict.summary, "evidence": record.verdict.evidence}
            for record in self._records
            if record.role == role and record.verdict.scope == "module"
            and any(item.startswith("Module accepted: ") for item in record.verdict.done)
        ]
        return clipped(json.dumps(milestones, ensure_ascii=False, indent=2), 12000)

    def _review_prompt(self, index: int, worker: Verdict, output_path: Path, tree: str,
                       last_review: Verdict | None = None) -> str:
        return "\n".join(
            [
                "You are an independent reviewer in an unattended development loop.",
                "You are read-only. Do not edit files, commit, push, or change configuration.",
                "The worker alone implements fixes and runs builds or tests that write files. "
                "Inspect code and existing evidence; return missing checks and fixes to the worker. "
                "Do not run commands that generate files or alter the workspace.",
                "Inspect the working tree yourself. Do not trust the worker's summary.",
                "Recheck previously reported audit gaps and failed monitor checks against the current code and evidence. "
                "Do not drop an unresolved issue because the worker restarted or claimed it was fixed. "
                "The previous conclusion is context, not proof; judge it independently. "
                "Previously accepted module milestones do not imply completion of pending modules.",
                "Accept the work only when the requirement is implemented and you cannot name a concrete missing behavior, broken path, or missing check.",
                "A done conclusion without evidence is rejected. Cite files, commands, and observations in evidence.",
                "Style nits alone are not gaps. A missing requirement, failing check, or unwired feature is a gap.",
                "If you find gaps, status must be continue and next_actions must tell the worker exactly what to change.",
                "Use blocked only when an external secret, credential, or unavailable service stops all progress.",
                "If the worker claims blocked but you can continue without that dependency, status must be continue.",
                "Monitoring continues through questions and blockers. Do not ask the person for permission to continue.",
                "Only return done when the worker explicitly reported done and the deliverable passes your independent checks.",
                "A module completion can be accepted with scope=module and status=done. "
                "The supervisor then continues the original requirement. "
                "Use scope=requirement only after independently checking ALL original modules, integration, "
                "and acceptance checks. Do not treat one deliverable module as completion of the entire requirement.",
                "",
                f"Review round: {index}",
                "Checkpoint: completion candidate" if _accepted(worker) else "Checkpoint: in-progress audit; steer unfinished work without accepting it as delivered",
                f"Workspace fingerprint: {tree}",
                "",
                "Requirement:",
                self.config.requirement.strip(),
                "",
                "Acceptance checks:",
                self.config.acceptance.strip() or "(none beyond the requirement)",
                "Monitor acceptance commands (inspect coverage; the monitor executes them after your review):",
                json.dumps(self.config.checks, ensure_ascii=False),
                "",
                "Worker conclusion:",
                json.dumps(worker.to_dict(), ensure_ascii=False, indent=2),
                "",
                "Previous independent review and monitor acceptance feedback:",
                json.dumps(last_review.to_dict(), ensure_ascii=False, indent=2) if last_review else "(no previous review)",
                "",
                "Last completed business audit (retained across reviewer failures):",
                self._audit_context(last_review),
                "",
                "Open audit issues (must not disappear by omission):",
                json.dumps(self.audit.prompt(), ensure_ascii=False),
                "For each fixed or invalid finding, return issue_resolutions with its exact AUD ID, an explanation and changed-file evidence. "
                "Do not mark an issue resolved based only on a worker claim. Unresolved issues remain open across turns and resume.",
                "When an existing issue remains open, prefix its gaps entry with its exact AUD ID (for example 'AUD-1: assertion still fails'). "
                "This preserves its original description and scope instead of creating a duplicate under new wording.",
                "",
                "Accepted module milestones:",
                self._module_progress(),
                "Monitor module ledger (check every pending module and its acceptance criteria):",
                json.dumps(self.modules.export(), ensure_ascii=False),
                "",
                "Workspace snapshot:",
                workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir),
                "",
                "Workspace diff:",
                workspace_diff(self.config.cwd, ignore_dir=self.run_dir),
                self.git_history.review_context() if self.git_history else '',
                "",
                f"Worker transcript: {output_path}",
                "",
                verdict_instructions(),
            ]
        )

    def _invoke(
        self,
        provider: Provider,
        prompt: str,
        session_id: str,
        *,
        role: str,
        index: int,
        model: str,
        extra_args: list[str],
        read_only: bool,
    ) -> "_CompletedTurn":
        self._event("agent_start", role=role, round=index, provider=provider.name)
        self._check_budget(new_call=True)
        self._agent_calls += 1
        # Persist the call before launch so interruption cannot reset its budget.
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["usage"] = self._usage()
        write_json(state_path, state, durable=True)
        prompt_path = self.run_dir / f"{index:03d}-{role}-prompt.md"
        messages = self.directions.messages()
        if role == 'worker' and self.git_history:
            prompt += ('\n\nThe monitor records your conclusion and actual source changes in the target repository '
                       f'on {self.git_history.ref} after this turn. Keep edits in {self.config.cwd}. '
                       'Do not alter .auto-moniter/history, the audit branch, or previous audit records. '
                       'Report changed files, actual validation results, remaining gaps and failures precisely.\n')
        self._direction_ids[role] = [item['id'] for item in messages]
        if messages:
            prompt += "\n\nLatest instructions from the human managing this task (in chronological order):\n"
            prompt += json.dumps(messages, ensure_ascii=False, indent=2)
            prompt += ("\nApply these instructions to priorities and implementation within the authorized project. "
                       "Explicitly report how you addressed them and any conflict with the saved requirement, module acceptance or checks. "
                       "Do not silently discard requirements, weaken checks, or claim an instruction is satisfied without evidence. "
                       "The reviewer must independently check the amended work.\n")
        prompt_path.write_text(prompt, encoding="utf-8")
        started = time.monotonic()
        self._beat(f"round {index}: {role} pid pending")
        prepared = PreparedCommand([])
        completed = False
        live_output = LiveOutput(self.run_dir, role, index, provider.name)
        def on_started(child):
            self._remember_child(child)
            self.directions.receipt(self._direction_ids[role], role, index)
            if messages:
                live_output.emit({'kind': 'direction', 'text': f'已向本轮 AI 发送 {len(messages)} 条用户补充要求'})
        try:
            if session_id and provider.resume_supported:
                prepared = provider.resume_command(
                    session_id, prompt_path, self.config.cwd, self.schema_path,
                    model=model, extra_args=extra_args, read_only=read_only,
                )
            else:
                prepared = provider.start_command(
                    prompt_path, self.config.cwd, self.schema_path,
                    model=model, extra_args=extra_args, read_only=read_only,
                )
            if read_only and self._isolation:
                prepared = self._isolation.wrap(prepared, provider.name)
            if not read_only and self._worker_isolation:
                prepared = self._worker_isolation.wrap(prepared, provider.name)
            result = run_command(
                prepared, self.config.cwd, timeout_s=self.config.turn_timeout_s,
                idle_timeout_s=self.config.idle_timeout_s,
                on_child=on_started, heartbeat=self._heartbeat,
                heartbeat_note=f"round {index}: {role} running",
                should_stop=self._should_stop,
                max_output_bytes=self.config.max_output_bytes,
                memory_bytes=self.config.memory_bytes, file_bytes=self.config.file_bytes,
                on_output=live_output.feed,
            )
            completed = True
        except (OSError, ValueError, ProviderError) as exc:
            result = TurnResult(text="", stderr=f"Could not invoke {provider.name}: {exc}", exit_code=127)
        finally:
            live_output.close()
            self._forget_child(completed=completed)
        elapsed = time.monotonic() - started
        if self._stop_requested:
            result.interruption = self._stop_reason or "stop requested"
        text, structured = extract_text(result.stdout)
        found = extract_session_id(result.stdout, result.stderr) or session_id
        if provider.name == "paseo" and found:
            if role == "worker":
                self.worker_session_id = found
            else:
                self._active_reviewer_id = found
            self._save_external_session(role, found)
            if result.exit_code == 0 and not result.timed_out and not result.interruption and not self._stop:
                result = self._wait_for_paseo(provider, found, result, started, role, index)
                elapsed = time.monotonic() - started
                text, structured = extract_text(result.stdout)
            else:
                if not _paseo_stop(provider.binary, found, self.config.cwd):
                    raise RecoveryError(f"Cannot stop interrupted Paseo {role} {found}; no new turn will start")
        elif provider.name == "paseo" and result.exit_code == 0:
            result.exit_code = 1
            result.stderr = "Paseo did not return an agentId; cannot monitor the agent"
        if role == "reviewer" and provider.name == "paseo":
            self._active_reviewer_id = found
        if provider.name == "paseo" and found and not self._stop_requested:
            transcript = paseo_transcript(provider.binary, found, self.config.cwd)
            if transcript:
                text = transcript
                structured = False
        self._check_budget()
        output_path = self.run_dir / f"{index:03d}-{role}.log"
        output_path.write_text(
            "\n".join(
                [
                    f"$ {redact_command(prepared.argv[:2])}",
                    f"role={role} provider={provider.name} session={found or '-'}",
                    f"exit={result.exit_code} timed_out={result.timed_out} interruption={result.interruption or '-'} elapsed_s={elapsed:.2f}",
                    "----- conclusion -----",
                    clipped(redact(text)),
                    redact(diagnostic_summary(result.stdout, result.stderr, failed=result.exit_code != 0)),
                ]
            ),
            encoding="utf-8",
        )
        (self.run_dir / f"{index:03d}-{role}-text.md").write_text(clipped(redact(text)), encoding="utf-8")
        return _CompletedTurn(
        text=text,
            session_id=found,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            elapsed_s=elapsed,
            output_path=output_path,
            command=prepared.argv,
            structured=structured or result.structured,
            stderr=extract_error(result.stderr, result.stdout) if result.exit_code else result.stderr,
            interruption=result.interruption,
        )

    def _save_external_session(self, role: str, session_id: str) -> None:
        path = self.run_dir / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["session_id" if role == "worker" else "active_reviewer_session_id"] = session_id
        write_json(path, state, durable=True)

    def _wait_for_paseo(self, provider: Provider, session_id: str, launch: TurnResult, started: float, role: str, index: int) -> TurnResult:
        """Observe the daemon separately so an approval wait cannot trap the CLI."""
        last_activity = time.monotonic()
        previous_text = ""
        latest = launch
        live = LiveOutput(self.run_dir, role, index, provider.name, announce=False)
        while not self._stop:
            if self._should_stop():
                latest = TurnResult("", exit_code=-1, interruption="runtime budget")
                break
            elapsed = time.monotonic() - started
            if self.config.turn_timeout_s and elapsed >= self.config.turn_timeout_s:
                latest = TurnResult("", exit_code=-1, timed_out=True, interruption="turn limit")
                break
            if self.config.idle_timeout_s and time.monotonic() - last_activity >= self.config.idle_timeout_s:
                latest = TurnResult("", exit_code=-1, timed_out=True, interruption="no output")
                break
            self._beat(f"round {index}: {role} observing Paseo agent {session_id}")
            latest = run_command(
                PreparedCommand([provider.binary, "inspect", "--json", session_id]),
                self.config.cwd, timeout_s=15, on_child=self._remember_child,
                should_stop=self._should_stop,
                max_output_bytes=self.config.max_output_bytes,
            )
            self._forget_child()
            if latest.exit_code or latest.timed_out or self._stop:
                break
            try:
                state = json.loads(latest.stdout)
                status = str(state.get("status") or state.get("Status") or "").lower().replace("-", "_")
                self._beat(f"round {index}: {role} Paseo status: {status}")
            except (ValueError, AttributeError):
                latest.exit_code = 1
                latest.stderr = "Paseo inspect returned invalid agent state"
                break
            if status in {"idle", "completed", "done"}:
                break
            if status in {"awaiting_permission", "waiting_for_permission", "requires_permission", "waiting_for_approval", "awaiting_approval", "waiting_for_input", "awaiting_input", "needs_input", "requires_action"}:
                latest.interruption = "input request"
                break
            if status in {"error", "failed", "stopped", "archived"}:
                latest.exit_code = 1
                latest.stderr = f"Paseo agent status: {status}"
                break
            transcript = paseo_transcript(provider.binary, session_id, self.config.cwd)
            if transcript != previous_text:
                last_activity = time.monotonic()
                # Public daemon output is observable while its turn is still active.
                live.emit({'kind': 'message', 'text': redact(transcript[-4000:])})
                previous_text = transcript
            self._stop_event.wait(2)
        if self._stop or latest.timed_out or latest.interruption or latest.exit_code:
            if not _paseo_stop(provider.binary, session_id, self.config.cwd):
                raise RecoveryError(f"Cannot stop interrupted Paseo {role} {session_id}; no new turn will start")
        latest.stdout = launch.stdout + "\n" + latest.stdout
        return latest

    def _stop_external_agent(self) -> list[str]:
        sessions = [(self.config.provider, self.worker_session_id)]
        if self.config.reviewer:
            sessions.append((self.config.reviewer, self._active_reviewer_id or self.reviewer_session_id))
        errors = []
        for provider, session_id in sessions:
            if provider.name != "paseo" or not session_id:
                continue
            if not _paseo_stop(provider.binary, session_id, self.config.cwd):
                errors.append(f"could not stop {session_id}")
        return errors

    def _remember_child(self, child: subprocess.Popen[str]) -> None:
        self._child = child
        identity = process_identity(child.pid)
        if identity:
            write_json(self.run_dir / "process.json", identity, durable=True)
        self._beat(f"child pid {child.pid}")
        if self._stop:
            _terminate(child)

    def _forget_child(self, *, completed: bool = True) -> None:
        if self._child is not None and (not completed or self._child.poll() is None):
            raise RecoveryError("Child shutdown was not confirmed; process identity retained for recovery")
        self._child = None
        (self.run_dir / "process.json").unlink(missing_ok=True)

    def _beat(self, note: str) -> None:
        write_json(self._heartbeat, {
            "at": datetime.now(timezone.utc).isoformat(), "note": note, "pid": os.getpid(),
        })

    def _install_signals(self) -> dict:
        if threading.current_thread() is not threading.main_thread():
            return {}

        def _handle(signum: int, _frame: object) -> None:
            # Only set flags and signal the child here. Stopping the Paseo
            # daemon agent runs in the normal loop, after the handler returns.
            self._stop = True
            self._stop_requested = True
            self._stop_reason = signal.Signals(signum).name
            self._stop_event.set()
            child = self._child
            if child and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    return

        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle)
        return previous


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


def _paseo_archive(binary: str, agent_id: str, cwd: Path) -> bool:
    try:
        result = subprocess.run(
            [binary, "agent", "archive", "--json", agent_id],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _paseo_stop(binary: str, agent_id: str, cwd: Path) -> bool:
    try:
        result = subprocess.run([binary, "agent", "stop", "--json", agent_id], cwd=cwd, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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


def _record_from_state(item: dict) -> RoundRecord:
    return RoundRecord(
        index=int(item.get("index") or 0),
        role=str(item.get("role") or ""),
        verdict=_verdict_from_state(item) or Verdict("continue"),
        exit_code=int(item.get("exit_code") or 0),
        timed_out=bool(item.get("timed_out")),
        elapsed_s=float(item.get("elapsed_s") or 0),
        session_id=str(item.get("session_id") or ""),
        output_path=str(item.get("output_path") or ""),
        tree=str(item.get("tree") or ""),
        failure=str(item.get("failure") or ""),
        interruption=str(item.get("interruption") or ""),
        git_checkpoint=dict(item.get('git_checkpoint') or {}),
    )


def _consecutive_failures(records: list[RoundRecord], role: str) -> int:
    count = 0
    for record in reversed(records):
        if record.role != role:
            continue
        if not record.failure:
            break
        count += 1
    return count


def _verdict_from_state(value: object) -> Verdict | None:
    if not isinstance(value, dict):
        return None
    return Verdict(
        status=str(value.get("status") or "continue"),
        summary=str(value.get("summary") or ""),
        done=list(value.get("done") or []),
        gaps=list(value.get("gaps") or []),
        next_actions=list(value.get("next_actions") or []),
        evidence=list(value.get("evidence") or []),
        blockers=list(value.get("blockers") or []),
        scope=str(value.get("scope") or "requirement"),
        module_ids=list(value.get("module_ids") or []),
        modules=value.get("modules") or [],
        issue_resolutions=value.get("issue_resolutions") or [],
    )


class _InputWatch:
    """Parse complete CLI events before looking for an assistant's question."""

    def __init__(self) -> None:
        self.offset = 0
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.waiting = False

    def feed(self, cumulative: bytes | str) -> None:
        blob = cumulative.encode("utf-8") if isinstance(cumulative, str) else cumulative
        fresh = blob[self.offset:]
        self.offset = len(blob)
        self.feed_chunk(fresh)

    def feed_chunk(self, fresh: bytes) -> None:
        self.pending += self.decoder.decode(fresh)
        while self.pending.strip():
            self.pending = self.pending.lstrip()
            if self.pending[0] in "{[":
                try:
                    value, end = json.JSONDecoder().raw_decode(self.pending)
                except json.JSONDecodeError:
                    # An incomplete tool event is never a plain-text question.
                    self.waiting = False
                    return
                self.pending = self.pending[end:]
                text, _ = extract_text(json.dumps(value, ensure_ascii=False))
                self.waiting = bool(input_request(text))
            elif "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self.waiting = bool(input_request(line))
            else:
                self.waiting = bool(input_request(self.pending))
                return


def run_command(
    prepared: PreparedCommand,
    cwd: Path,
    *,
    timeout_s: float = 0,
    idle_timeout_s: float = 0,
    input_grace_s: float = 2,
    on_child: Callable[[subprocess.Popen[str]], None] | None = None,
    heartbeat: Path | None = None,
    heartbeat_note: str = "",
    should_stop: Callable[[], bool] | None = None,
    detect_input_requests: bool = True,
    max_output_bytes: int = 8 * 1024 * 1024,
    memory_bytes: int = 0,
    file_bytes: int = 0,
    on_output: Callable[[int, bytes], None] | None = None,
) -> TurnResult:
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.pop("PAGER", None)
    argv = prepared.argv
    if memory_bytes or file_bytes:
        argv = [sys.executable, str(Path(__file__).with_name("limits.py")), str(memory_bytes), str(file_bytes), *argv]
    try:
        child = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE if prepared.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return TurnResult(text="", stderr=str(exc), exit_code=127, command=prepared.argv)
    beater = None
    timed_out = False
    interruption = ""
    deadline = time.monotonic() + timeout_s if timeout_s else None
    last_output_at = time.monotonic()
    buffers = [bytearray(), bytearray()]
    watchers = [_InputWatch(), _InputWatch()]
    pending = memoryview((prepared.stdin_text or "").encode("utf-8"))
    output_size = 0
    try:
        if on_child:
            on_child(child)
        beater = _start_heartbeat(heartbeat, heartbeat_note, child.pid) if heartbeat else None
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((child.stdout, child.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if child.stdin:
                os.set_blocking(child.stdin.fileno(), False)
                if pending:
                    selector.register(child.stdin, selectors.EVENT_WRITE, -1)
                else:
                    child.stdin.close()
            while selector.get_map() or child.poll() is None:
                stopped = should_stop is not None and should_stop()
                timed_out = deadline is not None and time.monotonic() >= deadline
                idle_s = time.monotonic() - last_output_at
                if detect_input_requests and any(watcher.waiting for watcher in watchers) and idle_s >= input_grace_s:
                    interruption = "input request"
                elif idle_timeout_s and idle_s >= idle_timeout_s:
                    timed_out = True
                    interruption = "no output"
                if child.poll() is not None and not interruption and reap_background(child.pid):
                    interruption = "background processes"
                if stopped or timed_out or interruption:
                    interruption = interruption or ("stop requested" if stopped else "turn limit")
                    _terminate(child)
                    # Drain only the bounded retained output after terminating the process group.
                    for key in list(selector.get_map().values()):
                        if key.data < 0:
                            continue
                        while True:
                            try:
                                chunk = os.read(key.fd, 65536)
                            except BlockingIOError:
                                break
                            if not chunk:
                                break
                            kept = chunk[:max(0, max_output_bytes - output_size)]
                            buffers[key.data].extend(kept)
                            if kept and on_output:
                                on_output(key.data, kept)
                            output_size += len(kept)
                    break
                for key, _mask in selector.select(0.05):
                    if key.data == -1:
                        try:
                            written = os.write(key.fd, pending[:65536])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[len(pending):]
                        except BlockingIOError:
                            continue
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    last_output_at = time.monotonic()
                    kept = chunk[:max(0, max_output_bytes - output_size)]
                    buffers[key.data].extend(kept)
                    if kept and on_output:
                        on_output(key.data, kept)
                    output_size += len(kept)
                    if detect_input_requests:
                        watchers[key.data].feed_chunk(kept)
                    if len(kept) != len(chunk):
                        interruption = "output limit"
                        break
            child.wait()
            if not interruption and reap_background(child.pid):
                interruption = "background processes"
    except BaseException:
        _terminate(child)
        raise
    finally:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream and not stream.closed:
                stream.close()
        if beater:
            beater.join(timeout=1)
    stdout, stderr = (data.decode("utf-8", errors="replace") for data in buffers)
    if interruption == "background processes":
        stderr += "\nCLI exited but left background processes running. The monitor terminated them; this turn cannot be accepted."
    if child.returncode is not None and child.returncode < 0 and not interruption:
        interruption = signal.Signals(-child.returncode).name
    return TurnResult(
        text=stdout, stdout=stdout, stderr=stderr,
        exit_code=child.returncode or (1 if interruption else 0), timed_out=timed_out, command=prepared.argv,
        interruption=interruption,
    )


class _Heartbeat:
    def __init__(self, path: Path, note: str, child_pid: int) -> None:
        self.path = path
        self.note = note
        self.child_pid = child_pid
        self._stop = threading.Event()
        self._write()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(15):
            self._write()

    def _write(self) -> None:
        write_json(self.path, {
            "at": datetime.now(timezone.utc).isoformat(), "note": self.note,
            "child_pid": self.child_pid, "supervisor_pid": os.getpid(),
        })

    def join(self, timeout: float | None = None) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)


def _start_heartbeat(path: Path, note: str, child_pid: int) -> _Heartbeat:
    return _Heartbeat(path, note, child_pid)


def _terminate(child: subprocess.Popen[str]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can keep stdout/stderr open even after the parent exits.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)
