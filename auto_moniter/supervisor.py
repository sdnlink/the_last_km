"""Keep a coding agent working until an independent review accepts the result."""

from __future__ import annotations

import codecs
import gzip
import json
import os
import re
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
    evidence_paths,
    file_lock,
    file_snapshot,
    snapshot_fingerprint,
    workspace_diff,
    workspace_fingerprint,
    workspace_snapshot,
)
from auto_moniter.verdict import VERDICT_SCHEMA, Verdict, input_request, parse_verdict, verdict_instructions
from auto_moniter.runlog import clipped, diagnostic_summary
from auto_moniter.modules import ModuleLedger
from auto_moniter.storage import write_json
from auto_moniter.paths import default_run_dir, private_directory, state_root
from auto_moniter.processes import process_identity, recover_process, reap_background
from auto_moniter.events import record as record_event
from auto_moniter.failures import classify
from auto_moniter.isolation import ReviewIsolation, WorkerIsolation
from auto_moniter.history import prune_files
from auto_moniter.preflight import validate
from auto_moniter.redaction import redact, safe
from auto_moniter.audit import AuditLedger, AuditCapacityError
from auto_moniter import function_gate, function_tasks
from auto_moniter.live import LiveOutput
from auto_moniter.directions import Directions
from auto_moniter.git_history import GitHistory, GitHistoryError
from auto_moniter.business_recovery import BusinessHeadChanged
from auto_moniter.locking import workspace_lease
from auto_moniter.quality import Quality
from auto_moniter.metering import UsageMeter
from auto_moniter.verification import outcome as check_outcome
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter import requirements_review


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
    max_history_bytes: int = 0
    max_agent_state_bytes: int = 0
    review_every: int | None = None
    git_history: str = 'auto'
    quality_policy: dict = field(default_factory=dict)
    workflow: str = 'legacy'
    function_dispatch: bool = False


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
                    "watcher_result": record.watcher_result,
                    "reported_status": record.reported_status or record.verdict.status,
                    **({"delivery": record.delivery} if record.delivery else {}),
                    **({key: record.verdict.to_dict()[key] for key in ("task_id", "task_status", "next_task")}
                       if record.verdict.task_id else {}),
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
        self._resume_loaded = not config.resume
        self._stop_requested = False
        self._active_reviewer_id = ""
        self._records: list[RoundRecord] = []
        self._review_feedback: Verdict | None = None
        self._audit_feedback: Verdict | None = None
        self._rounds_since_review = 0
        self._worker_seconds_since_review = 0.0
        self._stop_reason = ""
        self._baseline_saved = False
        self._recovery_note = ""
        self.modules = ModuleLedger(config.modules)
        self.audit = AuditLedger()
        self.function_tasks = function_tasks.FunctionTasks()
        self._function_gate = self.config.function_dispatch
        self._function_baseline = None
        self._passed_function_checks = {}
        self._dispatch_directions = []
        self._function_issue_ids = []
        self._last_invocation = {}
        self._delivery_diagnosis = {}
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
        self.quality = Quality({}, {})
        self.meter = UsageMeter()
        self.requirements_review = {}
        self.requirement_cycles = []
        self._business_update_count = 0
        self._business_input = {}

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
            with workspace_lease(self.config.cwd), file_lock(self.config.cwd / ".auto-moniter/workspace.lock"), file_lock(self.run_dir / "supervisor.lock"):
                loaded = self._load_resume()
                if self._resume_refused or loaded is None:
                    return RunReport("invalid", 0, "Run is not resumable; no cleanup attempted.", "invalid")
                self._records = loaded[0]
                if (self.run_dir / 'git-history.json').exists():
                    history_type = ApprovedHistory if self.config.workflow=='review-first' else GitHistory
                    self.git_history = history_type.open(self.config.cwd, self.run_dir, self.config.git_history)
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
            with workspace_lease(self.config.cwd), file_lock(self.config.cwd / ".auto-moniter" / "workspace.lock"), file_lock(self.run_dir / "supervisor.lock"):
                try:
                    while True:
                        try:
                            return self._run_locked()
                        except BusinessHeadChanged:
                            self._check_budget()
                            if self._stop_requested or not self._sync_business_head(self._records):
                                raise
                            self.config.resume = True
                            self._started = time.monotonic()
                except Exception as exc:
                    if self.config.resume and not self._resume_loaded:
                        return self._resume_failure(exc)
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
            if (getattr(self, '_finished_status', '') == 'done'
                    and getattr(self.git_history, 'business_commits', False)):
                from auto_moniter.git_maintenance import maintenance
                try:
                    maintenance(self.config.cwd, runs_dirs=[self.run_dir.parent], operation='cleanup', apply=True)
                except (OSError, ValueError, GitHistoryError, LockError) as exc:
                    self.log('Completed task retained; audit retention deferred: ' + str(exc))

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
            self._resume_loaded = False
            try:
                loaded = self._load_resume()
            except Exception as exc:
                return self._resume_failure(exc)
            if self._resume_refused:
                return RunReport(self._resume_refused, 0, f"Run already finished with status {self._resume_refused}. Start a new --run-dir to run again.", "invalid")
            if not requirement_path.is_file():
                return RunReport("invalid", 0, "Cannot resume run: requirement.md is missing.", "invalid")
            self._records = loaded[0]
        verdict_schema = function_tasks.schema(VERDICT_SCHEMA, gate=self._function_gate,
            integration=bool(self._integration_scopes())) if self.config.function_dispatch else VERDICT_SCHEMA
        self.schema_path.write_text(json.dumps(verdict_schema, indent=2), encoding="utf-8")
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
        self.quality = Quality(self.config.quality_policy, self._baseline_files, getattr(self, '_saved_quality_state', None))
        self.quality.bind_checks(self.config.cwd, [*self.config.checks, *self.modules.checks_for(set(self.modules.items))])
        self._meter_missing = bool(self.quality.config.get('max_tokens') and self.meter.state['unknown_token_turns']
                                   or self.quality.config.get('max_cost_usd') and self.meter.state['unknown_cost_turns'])
        history_type = ApprovedHistory if self.config.workflow=='review-first' else GitHistory
        self.git_history = history_type.open(self.config.cwd, self.run_dir, self.config.git_history)
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
        if self._sync_business_head(records):
            last_worker = None
            last_review = self._audit_feedback
            stall, previous_gap_key, previous_tree = 0, '', ''
        # Git history, quality policy and process recovery belong to startup,
        # too. Until all have succeeded, preserve the original durable state.
        self._write_state(records, "running", stall, previous_gap_key, previous_tree)
        self._resume_loaded = True
        (self.run_dir / 'resume-error.json').unlink(missing_ok=True)
        (self.run_dir / "report.json").unlink(missing_ok=True)
        if len(self.requirement_cycles) >= requirements_review.MAX_CYCLES:
            return self._finish('action_required',
                'Requirement cycle limit reached; committed work saved. Start a new run to continue.', records, last_worker)
        if self._function_gate and not function_gate.catalog(self.config.checks, self.modules.items):
            return self._finish('action_required', 'Function dispatch requires an approved executable acceptance check. Configure checks and start a new run.', records, None)
        if self.config.workflow=='review-first' and not self._stop and (
                self.requirements_review.get('status')!='ready'
                or self.requirements_review.get('cycle', 1) != len(self.requirement_cycles) + 1):
            if not self._elaborate_requirement(records, next_function=bool(self.config.function_dispatch and
                    (self.function_tasks.current or self.function_tasks.accepted))):
                return self._finish('stopped' if self._stop_requested else 'action_required',
                                    self.requirements_review.get('summary','Requirement review did not complete; worker was not started.'), records, None)

        if (self.requirement_cycles
                and self._next_round == self.requirement_cycles[-1]['handoff']['round'] + 1):
            last_worker = last_review = None

        while not self._stop:
            repair_review = False
            self._process_control()
            if self._stop:
                break
            self._check_budget()
            from auto_moniter.runtime_policy import reserve_seconds
            reserve = reserve_seconds(self.config.max_runtime_s)
            if reserve and self.config.max_runtime_s - self._elapsed() <= reserve:
                return self._finish('budget_exceeded', 'Runtime budget reserved for handoff; no new model call started.', records, last_worker)
            if self._sync_business_head(records):
                last_worker = None
                last_review = self._audit_feedback
                stall, previous_gap_key, previous_tree = 0, '', ''
            if self.config.quality_policy:
                protected_errors = self.quality.baseline_errors(self.config.cwd)
                if protected_errors:
                    return self._finish('action_required', 'Protected acceptance baseline changed: ' + ', '.join(protected_errors), records, last_worker)
            pending = self._pending_review(records)
            if pending is not None:
                record = pending
                round_index, verdict, tree = record.index, record.verdict, record.tree
                turn = _CompletedTurn(verdict.raw, record.session_id, 0, False, record.elapsed_s,
                                      Path(record.output_path), [])
                if self.git_history:
                    self._git_before = self.git_history.candidate['base']
                self.log(f"round {round_index}: reusing worker candidate; only the unfinished reviewer will run")
                last_review = next((r.verdict for r in reversed(records)
                                    if r.role == 'reviewer' and r.index == round_index), last_review)
                self._event('worker_candidate_reused', round=round_index, tree=tree)
            else:
                round_index = self._next_round
                if self.config.max_rounds and round_index > self.config.max_rounds:
                    return self._finish(
                        "max_rounds",
                        f"Stopped after {self.config.max_rounds} worker rounds.",
                        records,
                        last_worker,
                    )
                if self.config.function_dispatch and (not self.function_tasks.current or
                        any(m['id'] not in self._dispatch_directions for m in self.directions.messages())):
                    if not self._elaborate_requirement(records, next_function=True):
                        return self._finish('stopped' if self._stop_requested else 'action_required',
                            self.requirements_review.get('summary', 'Reviewer did not supply a valid next function.'), records, last_worker)
                self._check_budget(new_call=True)
                self._next_round = round_index + 1
                self._beat(f"round {round_index}: starting worker")
                if self.git_history:
                    self._git_before = self.git_history.begin_round(round_index)

                track_files = bool(self._function_gate or self.config.quality_policy or any(m.get('files') for m in self.modules.items.values()))
                before_files = file_snapshot(self.config.cwd, ignore_dir=self.run_dir) if track_files else {}
                if self._function_gate:
                    try:
                        self._prepare_function_baseline(before_files)
                    except (OSError, ValueError) as exc:
                        return self._finish('action_required', 'Function scope could not be initialized: ' + str(exc), records, last_worker)
                worker_start_tree = (snapshot_fingerprint(self.config.cwd, before_files) if track_files else
                                     workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir))
                worker_prompt = self._worker_prompt(round_index, last_worker, last_review)
                # Compare to the last verified workspace, including changes made
                # between turns; comparing only to turn-start would miss those edits.
                impact_before = getattr(self, '_impact_snapshot', None)
                if impact_before is None and worker_start_tree == previous_tree:
                    impact_before = before_files
                self.log(f"round {round_index}: worker running")
                if self._stop:
                    self._next_round = round_index
                    break
                verification_only = self._verification_only()
                reuse_worker = bool(self.worker_session_id) and not self._drop_worker_session and not verification_only
                if verification_only:
                    try:
                        self.config.provider.validate_review(self.config.extra_args)
                    except ProviderError as exc:
                        return self._finish('action_required', 'Verification-only worker requires a supported read-only provider: '+str(exc), records, last_worker)
                turn = self._invoke(
                    self.config.provider,
                    worker_prompt,
                    self.worker_session_id if reuse_worker else "",
                    role="worker",
                    index=round_index,
                    model=self.config.model,
                    extra_args=self.config.extra_args,
                    read_only=verification_only,
                )
                if verification_only:
                    self.worker_session_id = ''
                elif turn.session_id:
                    self.worker_session_id = turn.session_id
                elif not self.worker_session_id:
                    self.log(f"round {round_index}: no worker session id; the next round starts a new session")
                verdict = self._verdict_from_turn(turn, role="worker")
                if self.config.function_dispatch and not _failure_reason(turn, verdict) and not turn.interruption:
                    self._validate_function_worker(verdict)
                self._rounds_since_review += 1
                self._worker_seconds_since_review += turn.elapsed_s
                after_files = file_snapshot(self.config.cwd, ignore_dir=self.run_dir) if track_files else {}
                tree = (snapshot_fingerprint(self.config.cwd, after_files) if track_files else
                        workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir))
                protected_errors = self.quality.baseline_errors(self.config.cwd, after_files) if self.config.quality_policy else []
                changed_count = sum(before_files.get(k) != after_files.get(k) for k in before_files.keys() | after_files.keys())
                checkpoint_due = bool(self.config.quality_policy and turn.timed_out
                                      and self.quality.config['review_interval_s'] and changed_count)
                changed_files = [str(Path(k).relative_to(self.config.cwd)) for k in impact_before.keys() | after_files.keys()
                                 if impact_before.get(k) != after_files.get(k)] if impact_before is not None and track_files else None
                reopened = self.audit.invalidate(tree, self.modules.affected(changed_files), changed_files)
                if reopened:
                    self._event("audit_issues_reopened", issues=reopened, tree=tree)
                invalidated = self.modules.invalidate(tree, changed_files)
                self._impact_snapshot = after_files if track_files else None
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
                if self._sync_business_head(records):
                    last_worker = None
                    last_review = self._audit_feedback
                    stall, previous_gap_key, previous_tree = 0, '', ''
                    continue
                if protected_errors:
                    return self._finish('action_required', 'Protected acceptance baseline changed; restore original assertions before resuming. Files: '
                                        + ', '.join(protected_errors), records, verdict)
                if self.git_history and self.config.workflow!='review-first':
                    self._checkpoint(record, turn.text, self._git_before)
                elif self.git_history:
                    self.git_history.prepare_review(round_index,self._git_before)
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

                if failure and not checkpoint_due:
                    if turn.interruption == 'network retry budget':
                        return self._finish('action_required', 'Continuous model connection failures exceeded the transport retry budget; work saved. Resolve service availability before resuming.', records, verdict)
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
                    if self.quality.config.get('verified_progress'):
                        stall, previous_gap_key, previous_tree = self._track_stall(stall, previous_gap_key, previous_tree, verdict, tree)
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

                # Source edits are normal implementation progress, not a reason
                # to interrupt every worker turn with another model audit.
                periodic_review = bool(self.config.review_every
                                       and self._rounds_since_review >= self.config.review_every)
                if self.config.workflow == 'review-first':
                    interval = self.quality.config.get('review_interval_s', 0)
                    periodic_review |= bool(interval and self._worker_seconds_since_review >= interval)
                if self.config.quality_policy:
                    threshold = self.quality.config['review_changed_files']
                    periodic_review |= checkpoint_due or bool(threshold and changed_count >= threshold)
                    if checkpoint_due:
                        self.worker_session_id = ''
                # A scoped repair with changed source and evidence should get a
                # bounded audit now, rather than wait behind unrelated modules.
                # This requests review only; it never promotes continue to done.
                repair_review = bool(self.config.workflow == 'review-first' and tree != worker_start_tree
                                     and verdict.scope == 'module' and verdict.module_ids
                                     and verdict.done and verdict.evidence
                                     and self.audit.pending(set(verdict.module_ids)))
                periodic_review |= repair_review or bool(self.config.function_dispatch and verdict.task_status == 'done')
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

            if self._sync_business_head(records):
                last_worker = None
                last_review = self._audit_feedback
                stall, previous_gap_key, previous_tree = 0, '', ''
                continue
            review_prompt = self._review_prompt(round_index, verdict, turn.output_path, tree, last_review)
            self._process_control()
            if self._stop:
                break
            review_tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
            if tree != review_tree:
                raise RuntimeError("Workspace changed after worker conclusion; worker evidence is stale")
            self._beat(f"round {round_index}: starting reviewer")
            self.log(f"round {round_index}: reviewer running")
            self._event("review_checkpoint", reason="completion" if _accepted(verdict) else ("blocker" if verdict.blockers else ("audit_repair" if repair_review else "periodic")),
                        rounds_since_review=self._rounds_since_review)
            review_session = "" if self.config.fresh_review else self.reviewer_session_id
            try:
                review_turn = self._invoke_review(
                    reviewer,
                    review_prompt,
                    review_session,
                    role="reviewer",
                    index=round_index,
                    model=self._reviewer_model(),
                    extra_args=self.config.reviewer_extra_args,
                    read_only=True,
                )
            finally:
                self._beat(f"round {round_index}: checking reviewer writes")
                if (workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != review_tree
                        and not self._business_head_changed()):
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
            reported_review_status = review.status
            if self._business_head_changed():
                records.append(RoundRecord(round_index, 'reviewer', review, review_turn.exit_code,
                    review_turn.timed_out, review_turn.elapsed_s, review_turn.session_id,
                    str(review_turn.output_path), tree=tree,
                    failure='Business HEAD changed during review; this conclusion requires revalidation'))
                self._sync_business_head(records)
                last_worker = None
                last_review = self._audit_feedback
                stall, previous_gap_key, previous_tree = 0, '', ''
                continue
            if self.git_history and _accepted(review):
                git_problems = self.git_history.review_problems(review.evidence)
                if git_problems:
                    review.status = 'continue'
                    review.gaps.extend(git_problems)
                    review.next_actions.append('Review the exact worker Git checkpoint and cite its full hash in evidence.')
                    if self.config.workflow == 'review-first':
                        review.parse_error = 'invalid review checkpoint evidence'
            if not review_turn.exit_code and not review_turn.timed_out and not review_turn.interruption and not review.parse_error:
                self._merge_modules(review, reviewer=True)
            review_failure = _failure_reason(review_turn, review)
            if self.config.workflow == 'review-first' and review.parse_error and not review_failure:
                review_failure = 'invalid conclusion: ' + review.parse_error
            watcher_result = {'status': 'not_applied', 'closed': [], 'reasons': [],
                              'issues': {item['id']: item['status'] for item in self.audit.items}}
            if (not review_failure and not review.parse_error and not review_turn.interruption
                    and (_accepted(review) or review.gaps or review.next_actions or review.blockers or review.issue_resolutions)):
                # Provider/format failures must not replace the last business audit.
                try:
                    rejected = getattr(self, '_review_audit_rejection', None)
                    if rejected and rejected['round'] == round_index:
                        raise ValueError(rejected['reason'])
                    watcher_result = self._record_audit(review, verdict, round_index, review_tree)
                except AuditCapacityError as exc:
                    self._audit_feedback = review
                    capacity_record = RoundRecord(round_index, "reviewer", review, review_turn.exit_code,
                                                  review_turn.timed_out, review_turn.elapsed_s, review_turn.session_id,
                                                  str(review_turn.output_path), tree=tree)
                    records.append(capacity_record)
                    capacity_record.watcher_result = {'status': 'rejected', 'closed': [], 'reasons': [str(exc)],
                                                       'issues': {item['id']: item['status'] for item in self.audit.items}}
                    if self.git_history and self.config.workflow!='review-first':
                        self._checkpoint(capacity_record, review_turn.text, self.git_history.last_worker['commit'])
                    raise BudgetExceeded(str(exc)) from exc
                except ValueError as exc:
                    watcher_result = {'status': 'rejected', 'closed': [], 'reasons': [str(exc)],
                                      'issues': {item['id']: item['status'] for item in self.audit.items}}
                    review.status = "continue"
                    review.parse_error = "invalid audit closure"
                    review.gaps.append(f"Audit issue update rejected: {exc}")
                    review.next_actions.append("Preserve open audit IDs; the reviewer must close each issue with valid file evidence.")
                else:
                    self._audit_feedback = review
                    if isinstance(self.git_history, ApprovedHistory):
                        self.git_history.remember_review(round_index)
                    self._rounds_since_review = 0
                    self._worker_seconds_since_review = 0.0
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
                watcher_result=watcher_result,
                reported_status=reported_review_status,
            )
            records.append(review_record)
            if self.git_history and self.config.workflow!='review-first':
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
                if self.config.workflow == 'review-first':
                    return self._finish('action_required', 'Reviewer did not finish; worker candidate saved. ' + review_failure,
                                        records, verdict)
                self._retry_pause(reviewer_failures + (3 if recovery == "backoff" else 0), category + " reviewer failure")
                continue
            if review.parse_error and self.config.workflow == 'review-first':
                retried = getattr(self, '_review_audit_retry_exhausted', None) == round_index
                return self._finish('action_required',
                                    ('Reviewer audit update rejected after reviewer-only retry; worker candidate saved. ' if retried
                                     else 'Reviewer audit update rejected; worker candidate saved. ')
                                    + '; '.join(watcher_result.get('reasons') or [review.parse_error]), records, verdict)
            next_functions = None
            if self.config.function_dispatch:
                if (self._verification_only() and review.task_status == 'changes_required'
                        and review.next_task is not None):
                    try:
                        staged = function_tasks.FunctionTasks(self.function_tasks.export())
                        if not (review.gaps or review.blockers) or review.next_task.get('kind') == 'verification':
                            raise ValueError('Failed verification requires concrete findings and an implementation assignment')
                        previous_id = staged.current['id']
                        if review.next_task.get('id') == previous_id:
                            raise ValueError('Verification repair must use a new implementation task ID')
                        staged.current = None
                        staged.assign(review.next_task, self.modules.items)
                        self._validate_function_plan(staged, self.modules.items, self.requirements_review)
                        self.function_tasks = staged
                        self._dispatch_directions = list(self._direction_ids['reviewer'])
                        self._record_assignment(staged.current)
                        self._event('verification_repair_assigned', verification_task=previous_id,
                                    task_id=staged.current['id'])
                    except ValueError as exc:
                        return self._finish('action_required', 'Verification repair assignment rejected: '+str(exc), records, review)
                    last_worker = last_review = None
                    self._write_state(records, 'running', 0, '', review_tree)
                    continue
                try:
                    next_functions, scope_gaps = self._review_function(verdict, review, round_index, review_tree)
                except ValueError as exc:
                    review.status = 'continue'
                    review_record.watcher_result.update(status='rejected', reasons=[str(exc)])
                    return self._finish('action_required', 'Function handoff rejected: ' + str(exc), records, review)
                if scope_gaps:
                    self._reject_function_candidate(review_record, scope_gaps)
                if next_functions is not None and self._function_gate:
                    task = self.function_tasks.current
                    self._passed_function_checks = {}
                    gaps = []
                    if task['delivery_scope'] != 'function':
                        missing = function_gate.coverage(self.function_tasks, self.modules.items, task,
                            self.requirements_review, self.config.cwd)
                        if missing:
                            review_record.watcher_result.update(status='rejected', reasons=['Missing criteria: ' + ', '.join(missing)])
                            return self._finish('action_required',
                                'Reviewer must dispatch implementation tasks for uncovered criteria before declaring delivery: ' + ', '.join(missing),
                                records, review)
                    else:
                        selected = function_gate.catalog(self.config.checks, self.modules.items, task['module_id'])
                        if not self._delivery_checks(round_index, review, final=False, expected_tree=review_tree,
                                function_commands=[selected[key] for key in task['check_ids']]):
                            gaps = review.gaps or ['Function regression checks did not complete']
                    if gaps:
                        self._reject_function_candidate(review_record, gaps)
                        next_functions = None
                if next_functions is not None and self.function_tasks.current['delivery_scope'] == 'function':
                    self._check_budget()
                    self._advance_function(next_functions, review_record)
                    last_worker, last_review = verdict, review
                    stall, previous_gap_key, previous_tree = 0, '', tree
                    self._write_state(records, 'running', stall, previous_gap_key, tree)
                    continue
            reviewer_failures = 0
            if review.blockers:
                write_json(self.run_dir / 'environment-blocks.json', safe({
                    'blockers': review.blockers, 'round': round_index, 'tree': review_tree,
                    'action': 'Do runnable independent work. Retry these validations only when the named environment changes; do not recode correct behavior to bypass them.'}), durable=True)
            if self.config.quality_policy and self.quality.review_blockers(review.blockers):
                write_json(self.run_dir / 'dependency-block.json', {'blockers': review.blockers,
                           'review_count': self.quality.state['blocker_count'], 'round': round_index,
                           'action': 'Resolve the named external dependency, then resume; no automatic model retries.'}, durable=True)
                return self._finish('action_required', 'Repeated independently confirmed external dependency; work saved for dependency resolution.', records, review)

            if _accepted(review) and not _accepted(verdict):
                review.status = "continue"
                review.gaps.append("Worker has not explicitly confirmed completion and readiness for delivery.")
                review.next_actions.append("Resolve the worker's open questions or gaps, verify the deliverable, and emit a done conclusion with evidence.")
            problems = acceptance_problems(
                self.config.cwd,
                review.evidence,
                baseline=self._baseline_files,
                ignore_dir=self.run_dir,
                git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else '',
            ) if _accepted(review) and not self._verification_only() else []
            if _accepted(review):
                problems.extend(f"Worker completion evidence: {problem}" for problem in ([] if self._verification_only() else acceptance_problems(
                    self.config.cwd, verdict.evidence, baseline=self._baseline_files, ignore_dir=self.run_dir,
                    git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else '',
                )))
                problems.extend(self.modules.coverage(verdict, review))
                pending = self.audit.pending(None if verdict.scope == review.scope == "requirement"
                                             else set(verdict.module_ids) & set(review.module_ids))
                if pending:
                    problems.append("Unresolved audit issues: " + ", ".join(item["id"] for item in pending)
                                    + ". The reviewer must explicitly close each issue with evidence.")
            if _accepted(review) and not problems:
                review_record.delivery = {'status': 'validating', 'reasons': []}
                if verdict.scope == "requirement" and review.scope == "requirement":
                    accepted = self._delivery_checks(round_index, review, expected_tree=review_tree) and self._commit_approved(record,review_record,turn.text,review_turn.text,review_tree)
                    if accepted:
                        review_record.delivery = {'status': 'accepted', 'reasons': []}
                        self.modules.accept(verdict, review, round_index, review_tree)
                        if next_functions is not None:
                            self._advance_function(next_functions, review_record)
                        if self._begin_follow_up(records, review, review_record):
                            self.log(f"round {round_index}: requirement accepted and committed; continuing next requirement elaboration")
                            last_worker = last_review = None
                            stall, previous_gap_key, previous_tree = 0, "", review_tree
                            continue
                        return self._finish(
                            "done",
                            review.summary or verdict.summary or "Reviewer accepted the work.",
                            records,
                            review,
                        )
                    review_record.verdict = review
                else:
                    joint_ids = set(verdict.module_ids) & set(review.module_ids)
                    if self._delivery_checks(round_index, review, final=False, expected_tree=review_tree, module_ids=joint_ids) and self._commit_approved(record,review_record,turn.text,review_turn.text,review_tree):
                        review_record.delivery = {'status': 'accepted', 'reasons': []}
                        self.modules.accept(verdict, review, round_index, review_tree)
                        if next_functions is not None:
                            self._advance_function(next_functions, review_record)
                        review.scope = "module"
                        review.module_ids = sorted(joint_ids)
                        self._complete_module(review)
                        self.log(f"round {round_index}: module accepted; continuing original requirement")
                    else:
                        review_record.verdict = review
                if review_record.delivery['status'] == 'validating':
                    review_record.delivery = {'status': 'rejected', 'reasons': list(review.gaps)}
            if _accepted(review) and problems:
                review_record.delivery = {'status': 'rejected', 'reasons': problems}
                review.status = "continue"
                review.gaps.extend(problems)
                review.next_actions.append("Make the required workspace change and cite a real file in evidence.")
                review_record.verdict = review
                self.log(f"round {round_index}: reviewer done rejected: {'; '.join(problems)}")
                self._event("review_rejected", reasons="; ".join(problems))

            if (getattr(self, '_publication_review_reused', None) == round_index
                    and any(self.modules.items[k]['status'] != 'accepted' for k in verdict.module_ids)):
                return self._finish('action_required', 'Committed candidate failed resumed validation; inspect the saved checks before continuing.',
                                    records, review)

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

        follow_up_stopped = self._stop_reason.startswith('Next requirement not ready')
        return self._finish('action_required' if follow_up_stopped or self._stop_reason.startswith(('Verified progress','Acceptance environment')) else 'stopped',
                            self._stop_reason if follow_up_stopped else f"Stopped: {self._stop_reason or 'stop requested'}.", records, last_worker)

    def _business_head_changed(self):
        return (isinstance(self.git_history, ApprovedHistory)
                and self.git_history.business_commits
                and self.git_history._business_identity() != self.git_history.business_head)

    def _sync_business_head(self, records):
        history = self.git_history
        if not isinstance(history, ApprovedHistory) or not history.business_commits:
            return False
        history.reconcile_business_head()
        if history.business_update_count < self._business_update_count:
            raise RecoveryError('Business input history is behind the monitor checkpoint')
        if history.business_update_count == self._business_update_count:
            return False
        from auto_moniter.business_recovery import update_baseline
        before = self._business_input.get('after') or history.business_updates[0]['before']
        current = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        baseline = update_baseline(history, self.quality, before, current)
        self._baseline_saved &= baseline == self._baseline_files
        self._baseline_files = baseline
        self.quality.baseline = baseline
        self.quality.bind_checks(self.config.cwd, [*self.config.checks, *self.modules.checks_for(set(self.modules.items))])
        errors = self._close_delivered_sessions()
        if errors:
            raise RecoveryError('Business input changed, but previous sessions could not close: ' + '; '.join(errors))
        identity = 'business:' + history.business_head['commit']
        invalidated = self.modules.invalidate(identity)
        reopened = self.audit.invalidate(identity)
        self.quality.state['checks'] = {}
        self._delivery_tree = ''
        self.worker_session_id = self.reviewer_session_id = self._active_reviewer_id = ''
        self._drop_worker_session = True
        self._impact_snapshot = current
        self._rounds_since_review = self.config.review_every or 1
        self._worker_seconds_since_review = 0.0
        self._business_update_count = history.business_update_count
        self._business_input = {'before': before, 'after': dict(history.business_head), 'count': self._business_update_count}
        self._recovery_count += 1
        self._no_progress = 0
        self._recovery_note = (
            'Business HEAD advanced from ' + (before.get('commit') or '(unborn)') + ' to '
            + history.business_head['commit'] + '. Continue the original requirement on the current source. '
            'Preserve existing work and open AUD IDs. Prior completion, review and check results need revalidation. '
            'Inspect the external commit delta, complete remaining modules, rerun checks and obtain a fresh read-only review. '
            'Committed acceptance assets are the new input; do not weaken uncommitted test assertions.')
        (self.run_dir / 'approval-ready.json').unlink(missing_ok=True)
        for name in ('acceptance-results.json', 'requirement-coverage.json'):
            path = self.run_dir / name
            if path.is_file() and not path.is_symlink():
                value = json.loads(path.read_text())
                value.update(invalidated=True, invalidated_reason='Business HEAD changed; revalidation required', complete=False)
                write_json(path, value, durable=True)
        self._event('business_head_adopted', **self._business_input, modules=invalidated, reopened_issues=reopened)
        self.log('Business HEAD advanced; automatically continuing on ' + history.business_head['commit'][:12]
                 + ' with fresh implementation, review and acceptance checks')
        self._write_state(records, 'running', 0, '', snapshot_fingerprint(self.config.cwd, current))
        return True

    def _begin_follow_up(self, records, review, review_record=None):
        """Start another reviewed requirement only when the saved request asks for one.

        A jointly accepted requirement still finishes the run unless the original
        request asks to continue with the next requirement elaboration. Repair
        rounds are not follow-ups: open audit issues stay with the worker.
        """
        if not requirements_review.requests_follow_up(self.config.requirement):
            return False
        if self.audit.pending():
            return False
        if self._pending_directions():
            return False
        self._close_follow_up_sessions()
        handoff = {
            'cycle': len(self.requirement_cycles) + 1,
            'round': review_record.index if review_record is not None else next((record.index for record in reversed(records) if record.role == 'reviewer'), 0),
            'tree': self._delivery_tree,
            'commit': (self.git_history.last_business_commit or {}).get('commit', '') if self.git_history else '',
            'ref': (self.git_history.business_head or {}).get('ref', '') if getattr(self.git_history, 'business_commits', False) else (self.git_history.ref if self.git_history else ''),
            'summary': review.summary,
            'evidence': list(review.evidence[:4]),
            'acceptance': self.config.acceptance,
            'modules': self.modules.export(),
        }
        self.requirement_cycles.append({
            'index': handoff['cycle'],
            'status': 'committed',
            'requirements_review': self.requirements_review,
            'handoff': handoff,
        })
        self._event('requirement_follow_up', cycle=handoff['cycle'], tree=self._delivery_tree, commit=handoff['commit'])
        self.requirements_review = {}
        self.worker_session_id = ''
        self.reviewer_session_id = ''
        self._active_reviewer_id = ''
        self._drop_worker_session = False
        self._audit_feedback = None
        self._review_feedback = None
        self._write_state(records, 'running', 0, '', self._delivery_tree)
        if len(self.requirement_cycles) >= requirements_review.MAX_CYCLES:
            self._stop = True
            self._stop_reason = 'Next requirement not ready: requirement cycle limit reached; committed work saved. Start a new run to continue.'
            return True
        if not self._elaborate_requirement(records):
            self._stop = True
            if not self._stop_requested and not self._stop_reason.startswith('Next requirement not ready'):
                self._stop_reason = 'Next requirement not ready: ' + self.requirements_review.get(
                    'summary', 'Next requirement elaboration did not become ready; worker was not started.')
            return True
        if self.requirements_review.get('status') != 'ready':
            self._stop = True
            self._stop_reason = self._stop_reason or 'Next requirement review stopped before it was ready'
            return True
        return True

    def _close_follow_up_sessions(self):
        errors = self._close_delivered_sessions()
        if errors:
            raise RuntimeError('Requirement accepted, but agent cleanup failed: ' + '; '.join(errors))
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != self._delivery_tree:
            raise RuntimeError('Workspace changed while closing delivered agents; revalidation required')

    def _elaborate_requirement(self,records, *, next_function=False):
        follow_up = [item['handoff'] for item in self.requirement_cycles]
        previous_brief = self.requirements_review
        self.requirements_review={'status':'reviewing','summary':'Reviewer is selecting the next function.' if next_function else 'Reviewer is elaborating the next requirement after local commit.' if follow_up else 'Reviewer is elaborating the original requirement before implementation.',
                                  'cycle': len(self.requirement_cycles) + 1}
        before=workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir)
        self._write_state(records,'running',0,'',before)
        original_schema=self.schema_path
        schema=self.run_dir/'requirements.schema.json'
        plan_schema = function_tasks.schema(requirements_review.SCHEMA, planning=True, gate=self._function_gate,
            integration=bool(self._integration_scopes())) if self.config.function_dispatch else requirements_review.SCHEMA
        schema.write_text(json.dumps(plan_schema))
        self.schema_path=schema
        self.log('requirements: reviewer elaborating next requirement; worker has not started' if follow_up else 'requirements: reviewer elaborating original request; worker has not started')
        try:
            if self._stop:
                return False
            prompt = requirements_review.prompt(self.config,self.modules.export(),follow_up=follow_up or None)
            if self.config.function_dispatch:
                prompt += ('\n\nYou own function decomposition. Propose exactly one next_task, normally for a single named function, '
                           'with objective, inputs, outputs, boundaries, focused verification and only already accepted dependencies. '
                           'Use delivery_scope=function for intermediate work; module or requirement only for a final function plus the corresponding verification. '
                           'Declare original modules once in modules (files=[]; no commands); use modules=[] when the ledger is already populated. '
                           'For needs_input use next_task=null. Do not assign the full backlog. '
                           'Accepted function records: ' + json.dumps(self.function_tasks.accepted, ensure_ascii=False))
                if self._function_gate:
                    prompt += self._function_planning_contract()
                if next_function:
                    prompt += '\nReuse the existing brief; choose only the next unaccepted function. Brief: ' + json.dumps(previous_brief, ensure_ascii=False)
            turn=self._invoke(self.config.reviewer,prompt,'',
                              role='reviewer',index=910000 + self._next_round if next_function else 900000 + len(self.requirement_cycles) if follow_up else 0,model=self._reviewer_model(),
                              extra_args=self.config.reviewer_extra_args,read_only=True)
            if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir)!=before:
                raise ValueError('Requirement reviewer changed the read-only workspace')
            if turn.session_id:self._active_reviewer_id=turn.session_id
            if turn.exit_code or turn.timed_out or turn.interruption:
                raise ValueError('Requirement reviewer failed: '+(turn.stderr or turn.interruption or str(turn.exit_code)))
            if self.config.function_dispatch:
                plan = json.loads(turn.text)
                if not isinstance(plan, dict) or not {'next_task','modules'} <= plan.keys():
                    raise ValueError('Reviewer must supply a structured function assignment and module plan')
                assignment, modules = plan.pop('next_task'), plan.pop('modules')
                brief = requirements_review.parse(json.dumps(plan))
                brief['cycle'] = len(self.requirement_cycles) + 1
                if self._function_gate and next_function and previous_brief.get('status') == 'ready':
                    previous = previous_brief['acceptance_criteria']
                    if brief['acceptance_criteria'][:len(previous)] != previous:
                        raise ValueError('Reviewer must preserve the existing requirement criterion IDs and text')
                if brief['status']=='ready':
                    from auto_moniter.modules import definitions
                    proposed = definitions(modules)
                    if any(m.get('files') or m.get('checks') for m in proposed):
                        raise ValueError('Planning cannot supply executable checks or override fixed file scopes')
                    if self.modules.items and any(m['id'] not in self.modules.items for m in proposed):
                        raise ValueError('Function planning cannot expand fixed module scope')
                    staged = ModuleLedger.restore(self.modules.export())
                    staged.merge(proposed)
                    dispatch = function_tasks.FunctionTasks(self.function_tasks.export())
                    if next_function and dispatch.current:
                        revised = function_tasks.validate(assignment, staged.items)
                        if function_tasks.identity(revised) != function_tasks.identity(dispatch.current):
                            raise ValueError('Updated instructions must preserve the current assignment identity and target scope')
                        dispatch.current = None
                    dispatch.assign(assignment, staged.items)
                    if self._function_gate:
                        self._validate_function_plan(dispatch, staged.items, brief)
                    elif dispatch.current.get('kind') in ('integration','verification'):
                        raise ValueError('Integration and verification assignments require the verified function gate')
                    self.function_tasks = dispatch
                    self.modules = staged
                    self._dispatch_directions = list(self._direction_ids['reviewer'])
                    self._record_assignment(dispatch.current)
                elif assignment is not None:
                    raise ValueError('Blocked planning cannot dispatch a function')
            else:
                brief=requirements_review.parse(turn.text)
            brief['cycle']=len(self.requirement_cycles) + 1
            brief['session_id']=turn.session_id
            brief['original_sha256']=requirements_review.requirement_hash(self.config.requirement)
            self.requirements_review=brief
            self.directions.receipt(self._direction_ids['reviewer'],'reviewer',1,responded=True)
            write_json(self.run_dir/'reviewed-requirement.json',safe(brief),durable=True)
            self._event('requirements_reviewed',status=brief['status'])
            return brief['status']=='ready'
        except ValueError as exc:
            self.requirements_review={'status':'failed','summary':redact(str(exc)),
                                      'cycle': len(self.requirement_cycles) + 1}
            if follow_up and not self._stop_requested:
                self._stop = True
                self._stop_reason = 'Next requirement not ready: ' + self.requirements_review['summary']
            return False
        finally:
            self.schema_path=original_schema
            if self.config.reviewer.name=='paseo' and self._active_reviewer_id:
                stopped=_paseo_stop(self.config.reviewer.binary,self._active_reviewer_id,self.config.cwd)
                archived=_paseo_archive(self.config.reviewer.binary,self._active_reviewer_id,self.config.cwd)
                if not stopped or not archived:raise RecoveryError('Requirement reviewer cleanup failed')
            self._active_reviewer_id=''
            self.reviewer_session_id=''
            self._write_state(records,'running',0,'',workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir))

    def _commit_approved(self,worker,reviewer,worker_text,review_text,tree):
        if self.config.workflow!='review-first' or not self.git_history:return True
        with self.directions.lock():
            return self._commit_approved_locked(worker,reviewer,worker_text,review_text,tree)

    def _publication_boundary(self, reviewer) -> bool:
        """Refuse a new local publication once stop or the cumulative budget is known.

        An already atomic publication is left intact. This gate runs at the commit
        entry and again immediately before irreversible ref updates.
        """
        self._process_control(active=True)
        if self._stop or self._stop_requested:
            reviewer.verdict.status = 'continue'
            reviewer.verdict.gaps.append(
                'Publication refused: ' + (self._stop_reason or 'stop requested') + '. Committed work is unchanged.')
            reviewer.verdict.next_actions.append('Do not start another publication or requirement round after stop.')
            self._event('publication_refused', reason=self._stop_reason or 'stop requested')
            return False
        try:
            self._check_budget()
        except BudgetExceeded as exc:
            self._stop = True
            self._stop_reason = str(exc)
            reviewer.verdict.status = 'continue'
            reviewer.verdict.gaps.append('Publication refused: ' + str(exc) + '. Committed work is unchanged.')
            reviewer.verdict.next_actions.append('Do not start another publication after the runtime budget is exhausted.')
            self._event('publication_refused', reason=str(exc))
            return False
        return True

    def _commit_approved_locked(self,worker,reviewer,worker_text,review_text,tree):
        if self._sync_business_head(self._records):
            return False
        if not self._publication_boundary(reviewer):
            return False
        if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir)!=tree:
            raise RuntimeError('Workspace changed after review; refusing local commit')
        if self._pending_directions():
            reviewer.verdict.status='continue'
            reviewer.verdict.gaps.append('New instructions arrived before commit; implement and review them first.')
            return False
        if not self._publication_boundary(reviewer):
            return False
        if self.git_history.business_commits:
            from auto_moniter.publication import PublicationRefused, publish
            write_json(self.run_dir / 'approval-ready.json', {'round': worker.index, 'tree': tree,
                'candidate_tree': self.git_history.candidate['commit'], 'review': reviewer.verdict.to_dict(),
                'business_head': self.git_history.business_head, 'provider_config': self._provider_config(),
                'reviewer_model': self._reviewer_model()}, durable=True)
            def payload(record):
                data = RunReport('', 0, '', '', [record]).to_dict()['records'][0]
                data.update(requirement=self.config.requirement, acceptance=self.config.acceptance,
                            directions=self.directions.messages())
                if record.role == 'reviewer':
                    data['audit_issues'] = self.audit.export()
                return data
            try:
                worker.git_checkpoint, reviewer.git_checkpoint = publish(
                    self.git_history, worker.index, payload(worker), payload(reviewer), worker_text, review_text,
                    boundary=lambda: self._publication_boundary(reviewer))
            except PublicationRefused:
                return False
            except BusinessHeadChanged:
                raise
            except (OSError, GitHistoryError) as exc:
                if self._business_head_changed() and not (self.run_dir/'publication.json').exists():
                    raise BusinessHeadChanged('Business HEAD advanced before publication; revalidation required') from exc
                raise RecoveryError('Business publication incomplete; preserve staging and resume after resolving: ' + str(exc)) from exc
            for record in (worker, reviewer):
                self._event('git_checkpoint', role=record.role, round=record.index,
                            commit=record.git_checkpoint['commit'], ref=self.git_history.ref)
            self._event('approved_local_commit', commit=self.git_history.last_business_commit['commit'],
                        ref=self.git_history.business_head['ref'], round=worker.index)
        else:
            self._checkpoint(worker,worker_text,self._git_before)
            self._checkpoint(reviewer,review_text,worker.git_checkpoint['commit'])
            self._event('approved_local_commit',commit=reviewer.git_checkpoint['commit'],round=worker.index)
        self._write_state(self._records,'running',0,'',tree)
        return True

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
                # No agent is running during backoff; pause at this boundary now.
                self._process_control()
            self._check_budget()
            self._process_control()

    def _elapsed(self) -> float:
        return self._elapsed_before + time.monotonic() - self._started

    def _should_stop(self) -> bool:
        self._process_control(active=True)
        self._check_storage_quota()
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
        return self._stop or self._space_exceeded or bool(getattr(self, '_quota_exceeded', '')) or bool(self.config.max_runtime_s and self._elapsed() >= self.config.max_runtime_s)

    def _check_storage_quota(self) -> None:
        if not (self.config.max_history_bytes or self.config.max_agent_state_bytes):
            return
        if time.monotonic() - getattr(self, '_last_quota_check', 0) < 10:
            return
        from auto_moniter.quota import account
        self._last_quota_check = time.monotonic()
        self._storage = account(self.config.cwd, history=bool(self.config.max_history_bytes),
                                agents=bool(self.config.max_agent_state_bytes))
        self._quota_exceeded = next((name for name in ('history_bytes', 'agent_state_bytes')
                                    if getattr(self.config, 'max_' + name)
                                    and self._storage[name] >= getattr(self.config, 'max_' + name)), '')
        if self._quota_exceeded:
            self._space_exceeded = True

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
        if getattr(self, '_meter_missing', False):
            raise BudgetExceeded('Configured token/cost budget cannot be verified: provider did not report usage')
        if self.meter.exhausted(self.quality.config):
            raise BudgetExceeded('Provider-reported token/cost budget exhausted')
        self._check_storage_quota()
        if getattr(self, '_quota_exceeded', ''):
            raise BudgetExceeded('Storage quota exhausted: ' + self._quota_exceeded)
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
                "memory_bytes": self.config.memory_bytes, "file_bytes": self.config.file_bytes, "max_run_bytes": self.config.max_run_bytes,
                "max_history_bytes": self.config.max_history_bytes, "max_agent_state_bytes": self.config.max_agent_state_bytes,
                "storage": getattr(self, '_storage', {}),
                "provider_reported": self.meter.state,
                "metering_scope": 'Terminal provider reports only; cumulative reports conservatively summed; unreported internal/subagent usage is unknown.'}

    def _review_schedule(self) -> dict:
        return {"enabled": not self.config.no_review, "every_worker_rounds": self.config.review_every,
                "rounds_since_review": self._rounds_since_review,
                "worker_seconds_since_review": self._worker_seconds_since_review}

    def _record_audit(self, review: Verdict, worker: Verdict, index: int, tree: str) -> dict:
        modules = sorted(set(worker.module_ids) & self.modules.items.keys()) if worker.scope == "module" else []
        corrections = self.audit.reconcile_modules(self.modules.items)
        if corrections:
            self._event('audit_issue_scope_corrected', changes=corrections)
        findings, references = self.audit.local_findings(_audit_findings(review))
        open_before = {item["id"] for item in self.audit.pending()}
        def check_evidence(evidence):
            return acceptance_problems(self.config.cwd, evidence, baseline=self._baseline_files, ignore_dir=self.run_dir,
                                       git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else '')
        reported, closed = self.audit.update(findings, review.issue_resolutions, index, tree, modules, check_evidence,
                                            known_modules=self.modules.items)
        if references:
            for reference in references:
                reference['local_ids'] = [item['id'] for item in self.audit.items if item['summary'] == reference['finding']]
            self._event('audit_external_references_registered', references=references)
        if reported:
            if self.config.function_dispatch:
                self._function_issue_ids = sorted(set(self._function_issue_ids) | set(reported))
            self._event("audit_issues_reported", issues=reported)
        if closed:
            self._event("audit_issues_resolved", issues=closed, tree=tree)
            if open_before.intersection(closed) - {item["id"] for item in self.audit.pending()}:
                self._progress_reset |= any([self.quality.milestone('audit:' + ident) for ident in closed])
        return {'status': 'applied', 'reported': reported, 'closed': closed, 'reasons': [],
                'issues': {item['id']: item['status'] for item in self.audit.items}}

    def _checkpoint(self, record: RoundRecord, raw: str, before: str) -> None:
        data = RunReport('', 0, '', '', [record]).to_dict()['records'][0]
        data['requirement'] = self.config.requirement
        data['acceptance'] = self.config.acceptance
        if record.role == 'reviewer':
            data['audit_issues'] = self.audit.export()
        data['directions'] = [item for item in self.directions.messages()
                              if item['id'] in self._direction_ids[record.role]]
        record.git_checkpoint = self.git_history.record(record.index, record.role, data, before=before, raw=raw)
        self._event('git_checkpoint', role=record.role, round=record.index,
                    commit=record.git_checkpoint['commit'], ref=self.git_history.ref)

    def _merge_modules(self, verdict: Verdict, *, reviewer=False) -> None:
        try:
            proposals = []
            scopes = []
            for item in verdict.modules:
                if reviewer and isinstance(item, dict) and item.get('files'):
                    from auto_moniter.modules import definitions
                    definitions([item])
                    scopes.append((item['id'], item['files']))
                    item = {k:v for k,v in item.items() if k != 'files'}
                if isinstance(item, dict) and item.get('files') and (item.get('id') not in self.modules.items or item['files'] != self.modules.items[item['id']].get('files')):
                    raise ValueError('Module impact files must be supplied by the user, not by an agent')
                if isinstance(item, dict) and item.get("checks") and (item.get("id") not in self.modules.items or item["checks"] != self.modules.items[item["id"]].get("checks")):
                    raise ValueError("Executable module checks must be supplied by the user in --modules, not by an agent")
                proposals.append(item)
            self.modules.merge(proposals)
            for key, paths in scopes:
                self.modules.review_scope(key, paths)
        except ValueError as exc:
            verdict.status = "continue"
            verdict.parse_error = "invalid module plan"
            verdict.gaps.append(f"Module plan rejected: {exc}")

    def _reviewer_model(self):
        # Different providers have different model namespaces.
        same_backend = (self.config.reviewer and self.config.reviewer.name == self.config.provider.name
                        and getattr(self.config.reviewer, 'backend', None) == getattr(self.config.provider, 'backend', None))
        return self.config.reviewer_model or (self.config.model if same_backend else '')

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
        progress = self._progress_reset or (changed and not self.quality.config.get('verified_progress'))
        self._no_progress = self._no_progress + 1 if previous_tree and not progress else 0
        limit = self.quality.config.get('no_progress_limit', 0)
        if limit and self._no_progress >= limit:
            self._stop = True
            self._stop_reason = 'Verified progress limit reached; source edits alone do not establish progress.'
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
        if changed and (progress or not self.quality.config.get('verified_progress')):
            self._recovery_note = ""
        return count, key, tree

    def _delivery_checks(self, index: int, verdict: Verdict, *, final: bool = True, expected_tree: str = "",
                         module_ids: set[str] | None = None, function_commands: list[str] | None = None) -> bool:
        """Execute user-selected checks independently of the agents' claims."""
        if self._pending_directions():
            verdict.status = 'continue'
            verdict.gaps.append('New human instructions arrived after the worker started. Implement and review them before accepting delivery.')
            return False
        before = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        original_review = verdict.to_dict()
        baseline_errors = self.quality.baseline_errors(self.config.cwd) if self.config.quality_policy else []
        if baseline_errors:
            verdict.status = 'continue'
            verdict.gaps.append('Protected acceptance baseline changed: ' + ', '.join(baseline_errors))
            return False
        if final and self.quality.config.get('requirements'):
            worker = next((r.verdict for r in reversed(self._records) if r.role == 'worker'), verdict)
            rows, coverage_errors = self.quality.coverage(self.config.cwd, self.modules, worker, verdict)
            write_json(self.run_dir / 'requirement-coverage.json', {'tree': before, 'requirements': rows,
                       'errors': coverage_errors, 'complete': not coverage_errors}, durable=True)
            if coverage_errors:
                verdict.status = 'continue'
                verdict.gaps.extend(coverage_errors)
                return False
        # Candidate verification itself consumes the cumulative budget. Expire
        # here, before the check loop, so a late fingerprint cannot launch work.
        self._process_control(active=True)
        if not self._stop and not self._stop_requested:
            try:
                self._check_budget()
            except BudgetExceeded as exc:
                self._stop = True
                self._stop_reason = str(exc)
                verdict.status = "continue"
                verdict.gaps.append("Acceptance not started: " + str(exc) + ".")
                self._write_acceptance_snapshot(index, before, final, [], verdict.gaps, True, False)
                raise
        if self._stop or self._stop_requested:
            verdict.status = "continue"
            reason = self._stop_reason or "stop requested"
            verdict.gaps.append("Acceptance not started: " + reason + ".")
            self._write_acceptance_snapshot(index, before, final, [], verdict.gaps, True, False)
            return False
        if expected_tree and before != expected_tree:
            self.modules.invalidate(before)
            self.audit.invalidate(before)
            verdict.status = "continue"
            verdict.gaps.append("Workspace changed after the conclusion; acceptance evidence is stale. Review the current source again.")
            self._event("stale_evidence", expected=expected_tree, actual=before)
            return False
        # Module acceptance uses the same fixed commands as final delivery.
        # Agent-supplied module checks stay forbidden; only the saved user plan is appended.
        # Review-first module delivery must run the saved fixed checks. Legacy
        # module milestones keep the previous contract: user checks run only on
        # final requirement delivery, never because a module was accepted.
        commands = list(self.config.checks) if final or self.config.workflow == "review-first" else []
        ids = set(verdict.module_ids) if module_ids is None else set(module_ids)
        if final:
            ids.update(key for key, item in self.modules.items.items() if item["status"] == "accepted")
        commands.extend(self.modules.checks_for(ids))
        if function_commands is not None:
            commands = function_commands
        failures = []
        check_results = []
        budget_stop = None
        for number, command in enumerate(dict.fromkeys(commands), 1):
            # Read stop and the cumulative budget before Popen. A fingerprint
            # wait or a control.json stop must not launch a check that can finish
            # before the in-process should_stop poll notices.
            self._process_control(active=True)
            if not self._stop and not self._stop_requested:
                try:
                    self._check_budget()
                except BudgetExceeded as exc:
                    self._stop = True
                    self._stop_reason = str(exc)
                    budget_stop = exc
            if self._stop or self._stop_requested or budget_stop:
                verdict.status = "continue"
                reason = self._stop_reason or 'stop requested'
                failures.append('Acceptance not started: ' + reason + '.')
                self._write_acceptance_snapshot(index, before, final, check_results, failures, True, False)
                if budget_stop:
                    verdict.gaps.extend(failures)
                    raise budget_stop
                return False
            self._beat(f"round {index}: acceptance check {number}")
            completed = False
            live_check = LiveOutput(self.run_dir, 'check', index, '验收检查')
            live_check.emit({'kind': 'command', 'text': redact(command), 'status': 'running'})
            result = None
            try:
                result = run_command(
                    PreparedCommand(["sh", "-c", command]), self.config.cwd,
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
                if result is not None:
                    path = self._record_check_result(index, number, command, result, before, final, check_results, failures, verdict)
            if result is None:
                self._write_acceptance_snapshot(index, before, final, check_results, failures, True, False)
                raise
            try:
                self._check_budget()
            except BudgetExceeded as exc:
                budget_stop = exc
                failures.append('Acceptance interrupted: ' + str(exc) + '. See ' + path.name + '.')
                break
            if check_results[-1]['status'] == 'environment_unavailable':
                self._stop = True
                self._stop_reason = 'Acceptance environment unavailable; prepare dependencies before resuming. See ' + path.name
                failures.append(self._stop_reason)
                break
            if check_results[-1]['status'] == 'passed' and self._function_gate:
                self._passed_function_checks[command] = path.name
            self._progress_reset |= self.quality.check_result(command, not (result.exit_code or result.timed_out or result.interruption))
        after = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        published_source_changed = after == before and self._checks_changed_published_source(before)
        if published_source_changed:
            # A fixed check can restore tracked bytes to the last published
            # content. The workspace hash is then unchanged, but the reviewed
            # candidate is no longer what publication would commit.
            after = before + ':check-source'
        if after != before:
            self.modules.invalidate(after)
            self.audit.invalidate(after)
            failures.append("Acceptance commands changed workspace source content. Previous review and evidence are stale; review the new content before accepting.")
        if (check_results and check_results[-1]['status'] == 'environment_unavailable'
                and not any(c['status'] not in ('passed', 'environment_unavailable') for c in check_results)
                and after == before and not self._pending_directions()):
            record = next((r for r in reversed(self._records) if r.role == 'reviewer' and r.index == index), None)
            if record is not None:
                record.watcher_result['check_retry'] = {
                    'tree': before, 'review': original_review, 'provider_config': self._provider_config(),
                    'reviewer_model': self._reviewer_model(), 'task': self.function_tasks.current,
                    'direction_ids': list(self._direction_ids['reviewer'])}
        self._write_acceptance_snapshot(index, before, final, check_results, failures, bool(self._stop or budget_stop), after != before)
        if budget_stop:
            verdict.status = "continue"
            verdict.gaps.extend(failures)
            raise budget_stop
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

    def _checks_changed_published_source(self, before: str) -> bool:
        history = getattr(self, 'git_history', None)
        candidate = (getattr(history, 'candidate', {}) or {}).get('commit')
        if not history or not getattr(history, 'business_commits', False) or not candidate or candidate == before:
            return False
        try:
            current = history._tree()
        except (OSError, GitHistoryError, ValueError):
            return True
        # Compare content trees. An unchanged candidate hash means the check
        # did not rewrite tracked source, even when the index still holds an
        # unrelated staged path.
        return current != candidate

    def _record_check_result(self, index, number, command, result, before, final, check_results, failures, verdict):
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
        status = check_outcome(result)
        check_results.append({'command':redact(command),'status':status,'exit_code':result.exit_code,
                              'timed_out':bool(result.timed_out),'interruption':result.interruption or '','log':path.name})
        self._write_acceptance_snapshot(index, before, final, check_results, failures, True, False)
        return path

    def _write_acceptance_snapshot(self, index, tree, final, check_results, failures, incomplete, source_changed):
        write_json(self.run_dir/'acceptance-results.json', {
            'round':index,'tree':tree,'scope':'requirement' if final else 'module',
            'checks':check_results,
            'complete':bool(check_results) and not failures and not incomplete and not source_changed and all(item.get('status') == 'passed' for item in check_results),
            'source_changed':source_changed,
            'note':'This is a check snapshot, not final delivery approval. Unexecuted behavior remains unverified.'}, durable=True)

    def _pending_directions(self) -> bool:
        return any(item['id'] not in self._direction_ids['worker'] for item in self.directions.messages())

    def _integration_scopes(self):
        return self.config.quality_policy.get('integration_scopes', []) if self._function_gate else []

    def _verification_only(self):
        return bool(self._function_gate and self.function_tasks.current
                    and self.function_tasks.current.get('kind') == 'verification')

    def _record_assignment(self, task):
        fields = ({'task_kind': 'integration', 'targets': task['targets'], 'integration_scope': task['integration_scope']}
                  if task.get('kind') == 'integration' else {'file': task['file'], 'symbol': task['symbol']})
        if task.get('kind') == 'verification':
            fields['task_kind'] = 'verification'
        self._event('function_assigned', task_id=task['id'], **fields)

    def _function_planning_contract(self):
        from auto_moniter.function_languages import availability as language_availability
        modules = self.modules.items
        cycle = len(self.requirement_cycles) + 1
        scopes = self._integration_scopes()
        integration = (
            '\nUse kind=integration only when the targets must change together to preserve working behavior. '
            'Use integration_scope, the exact targets (file and symbol), and exactly the check_ids from one approved scope; '
            'give an atomic_reason explaining why separate function tasks cannot work. Omit top-level file and symbol for '
            'integration. Every target must change, and each role must cite every target file. Approved integration scopes: '
            + json.dumps(scopes, ensure_ascii=False)) if scopes else ''
        return ('\nFunction gate: use a supported source language and identify one exact qualified symbol. '
                'When implementation is already correct and only module/final acceptance remains, use kind=verification, '
                'delivery_scope=module or requirement, test_files=[] and an existing function; no source edits are required '
                'or allowed. The worker and reviewer inspect read-only, and the watcher runs the fixed checks. '
                'Python, JavaScript and HTML inline JavaScript work without optional packages. '
                'Other registered languages use the optional tree-sitter grammar pack; the monitor checks availability before dispatch. '
                'Include criterion_ids from the assigned module (<module ID>:<one-based acceptance index>) '
                f'and applicable requirement criteria (req:{cycle}:<one-based acceptance_criteria index>). '
                'Use the same indices for new module definitions or a new brief returned in this response. Select check_ids from the '
                'approved catalog; never write an executable command in a task. Include test_files=[] or exact paths '
                'for new focused test files. Preserve existing tests and all code outside the assigned targets. '
                'Only declare module or requirement delivery once accepted tasks plus the current task map to every '
                'criterion in the delivery scope. Approved check IDs: '
                + json.dumps(function_gate.catalog(self.config.checks, modules), ensure_ascii=False)
                + '\nCriteria by ID: ' + json.dumps({**function_gate.requirement_criteria(self.requirements_review),
                    **{k: v for ident in modules for k, v in function_gate.criteria(modules, ident).items()}}, ensure_ascii=False)
                + '\nOptional language parser availability: ' + json.dumps(language_availability(), ensure_ascii=False)
                + integration)

    def _prepare_function_baseline(self, snapshot=None):
        task = self.function_tasks.current
        if self._function_baseline is not None and self._function_baseline['id'] == task['id']:
            return self._function_baseline
        if snapshot is None:
            snapshot = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        previous = self._last_invocation or {}
        self._function_baseline = function_gate.baseline(
            self.run_dir / 'function-baseline.json.gz', self.config.cwd, task, snapshot,
            missing_ok=previous.get('task_id') != task['id'])
        return self._function_baseline

    def _validate_function_plan(self, ledger, modules, brief, *, provisional=None):
        task = ledger.current
        function_gate.validate_task(task, self.config.cwd, modules, self.config.checks, brief,
                                    approvals=self._integration_scopes())
        if task['delivery_scope'] != 'function':
            missing = function_gate.coverage(ledger, modules, task, brief, self.config.cwd,
                                              provisional=provisional)
            if missing:
                raise ValueError('Assign remaining implementation before declaring delivery; uncovered criteria: ' + ', '.join(missing))

    def _function_scope_problems(self):
        try:
            gaps, self._function_target_hashes = function_gate.scope_problems(self.config.cwd, self.function_tasks.current,
                self._prepare_function_baseline(), file_snapshot(self.config.cwd, ignore_dir=self.run_dir))
            return gaps
        except (OSError, ValueError) as exc:
            return [str(exc)]

    def _reject_function_candidate(self, record, gaps):
        review = record.verdict
        review.status = 'continue'
        review.task_status = 'changes_required'
        review.next_task = None
        review.gaps = list(dict.fromkeys([*review.gaps, *gaps]))
        record.watcher_result.update(status='rejected', reasons=list(gaps), function_gate='repair')
        record.delivery = {'status': 'rejected', 'reasons': list(gaps)}
        self._event('function_gate_rejected', task_id=review.task_id, reasons=list(gaps))

    def _validate_function_worker(self, verdict):
        task = self.function_tasks.current
        expected = task['delivery_scope']
        invalid = (verdict.parse_error or verdict.task_id != task['id'] or
                   verdict.task_status not in ('done', 'incomplete') or
                   verdict.next_task is not None or verdict.modules or verdict.issue_resolutions or
                   set(verdict.module_ids) - {task['module_id']} or
                   (_accepted(verdict) and (expected == 'function' or verdict.scope != expected
                    or verdict.module_ids != [task['module_id']] or verdict.task_status != 'done')) or
                   (verdict.task_status == 'done' and (not verdict.evidence or verdict.gaps or
                    verdict.blockers or verdict.next_actions or (expected != 'function' and not _accepted(verdict)))))
        if invalid:
            verdict.status = 'continue'
            verdict.task_status = 'incomplete'
            verdict.task_id = task['id']
            verdict.modules = []
            verdict.issue_resolutions = []
            verdict.next_task = None
            verdict.module_ids = []
            verdict.gaps.append('Report only the dispatched function with its exact task ID and verified completion evidence.')

    def _review_function(self, worker, review, index, tree):
        task = self.function_tasks.current
        if not task or review.task_id != task['id'] or review.task_status not in ('accepted', 'changes_required'):
            raise ValueError('Reviewer must conclude on the dispatched function ID')
        if review.task_status == 'changes_required':
            if _accepted(review) or review.next_task is not None or not (review.gaps or review.blockers):
                raise ValueError('Rejected function requires focused gaps and cannot dispatch another task')
            return None, []
        expected = task['delivery_scope']
        if (worker.task_id != task['id'] or worker.task_status != 'done' or review.gaps or review.blockers or review.next_actions
                or (expected == 'function' and _accepted(review))
                or (expected != 'function' and (not _accepted(review) or review.scope != expected
                    or review.module_ids != [task['module_id']]))):
            raise ValueError('Function approval requires matching worker completion and delivery scope')
        if self._pending_directions():
            review.status = 'continue'
            review.task_status = 'changes_required'
            review.gaps.append('New user instructions require reviewer planning before this function can advance.')
            return None, []
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree:
            raise ValueError('Function review is stale because the workspace changed')
        if self.git_history:
            problems = self.git_history.review_problems(review.evidence)
            if problems:
                raise ValueError('; '.join(problems))
        unresolved = [i['id'] for i in self.audit.pending() if i['id'] in self._function_issue_ids]
        if unresolved:
            raise ValueError('Dispatched function has unresolved audit issues: ' + ', '.join(unresolved))
        if self._function_gate:
            gaps = self._function_scope_problems()
            if gaps:
                return None, gaps
        for role, conclusion in [('worker', worker), ('reviewer', review)]:
            problems = self._function_evidence_problems(conclusion, role)
            if problems:
                raise ValueError('; '.join(problems))
        staged = function_tasks.FunctionTasks(self.function_tasks.export())
        staged.accept(worker, review, index, tree)
        if expected == 'requirement' and review.next_task is not None:
            raise ValueError('Final delivery cannot include another implementation assignment')
        if review.next_task is not None:
            if self._function_gate:
                try:
                    staged.assign(review.next_task, self.modules.items)
                    self._validate_function_plan(staged, self.modules.items, self.requirements_review,
                                                 provisional=task)
                except ValueError as exc:
                    staged.current = None
                    self._event('function_next_assignment_rejected', task_id=task['id'], reason=str(exc))
            else:
                if isinstance(review.next_task, dict) and review.next_task.get('kind') in ('integration','verification'):
                    raise ValueError('Integration and verification assignments require the verified function gate')
                staged.assign(review.next_task, self.modules.items)
        return staged, []

    def _function_evidence_problems(self, conclusion, role):
        verification = self._verification_only()
        problems = acceptance_problems(self.config.cwd, conclusion.evidence, baseline=self._baseline_files,
            ignore_dir=self.run_dir, git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else '') if not verification else []
        cited = set(evidence_paths(self.config.cwd, conclusion.evidence,
            baseline=None if verification else self._baseline_files, ignore_dir=self.run_dir))
        missing = [file for file, _ in function_tasks.targets(self.function_tasks.current)
                   if (self.config.cwd / file).resolve() not in cited]
        errors = [role + ': ' + problem for problem in problems]
        if missing:
            errors.append(role + ' must cite every changed assignment file; missing: ' + ', '.join(missing))
        return errors

    def _advance_function(self, staged, review_record):
        task = self.function_tasks.current
        if self._function_gate:
            selected = function_gate.catalog(self.config.checks, self.modules.items, task['module_id'])
            staged.accepted[-1]['verification'] = {key: self._passed_function_checks[selected[key]]
                                                   for key in task['check_ids']}
            if task.get('kind') == 'integration':
                staged.accepted[-1]['target_hashes'] = self._function_target_hashes
            else:
                staged.accepted[-1]['symbol_hash'] = self._function_target_hashes[task['file']]
        self.function_tasks = staged
        self._function_issue_ids = []
        self._dispatch_directions = list(self._direction_ids['reviewer'])
        review_record.watcher_result['function'] = {'status': 'accepted', 'task_id': task['id'],
                                                     'tree': review_record.tree}
        self._progress_reset |= self.quality.milestone('function:' + task['id'])
        self._event('function_accepted', task_id=task['id'], tree=review_record.tree)
        if staged.current:
            self._record_assignment(staged.current)

    def _verdict_from_turn(self, turn: "_CompletedTurn", *, role: str) -> Verdict:
        verdict = parse_verdict(turn.text, structured=turn.structured, function_gate=self._function_gate)
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
            write_json(self.run_dir / 'handoff.json', safe({
                'status': status, 'reason': summary, 'usage': self._usage(),
                'workspace': workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir),
                'candidate': getattr(self.git_history, 'candidate', {}) if self.git_history else {},
                'last_worker': next((r.verdict.to_dict() for r in reversed(records) if r.role=='worker'), None),
                'last_review': next((r.verdict.to_dict() for r in reversed(records) if r.role=='reviewer'), None),
                'modules': self.modules.export(), 'audit_issues': self.audit.export(),
                'note': 'Unaccepted work and interrupted output require independent review; no delivery implied.'}), durable=True)
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
        if status == 'budget_exceeded' and 'runtime budget' in summary.lower():
            from auto_moniter.delivery_diagnosis import diagnose
            check_path = self.run_dir / 'acceptance-results.json'
            checks = {}
            if check_path.is_file() and not check_path.is_symlink() and check_path.stat().st_size <= 512*1024:
                try:
                    checks = json.loads(check_path.read_text())
                except (OSError, ValueError):
                    pass
            self._delivery_diagnosis = diagnose(report.to_dict()['records'], last_call=self._last_invocation,
                acceptance=checks if isinstance(checks, dict) else {}, current=self.function_tasks.current)
        path = self.run_dir / "report.json"
        self._event("run_finished", status=status, summary=summary)
        self._finished_status = status
        if status in {"failed", "recovery_failed", "action_required", "budget_exceeded", "stalled"}:
            write_json(self.run_dir / "alert.json", safe({"status": status, "summary": summary,
                       "at": datetime.now(timezone.utc).isoformat(), "run_dir": str(self.run_dir)}), durable=True)
        write_json(path, safe({**report.to_dict(), "modules": self.modules.export(), "usage": self._usage(),
                                         "quality_policy": self.config.quality_policy, "quality_state": self.quality.state,
                                         "workflow":self.config.workflow,"requirements_review":self.requirements_review,
                                         "requirement_cycles": self.requirement_cycles,
                                         "no_progress_turns": self._no_progress, "recovery_count": self._recovery_count, "accepted_tree": self._delivery_tree,
                                         "review_schedule": self._review_schedule(),
                                         "audit_issues": self.audit.export(),
                                         "delivery_diagnosis": self._delivery_diagnosis,
                                         "function_tasks": self.function_tasks.export(),
                                         "history": self._history_info()}), durable=True)
        self._write_state(records, status, 0, "", records[-1].tree if records else "", result_summary=report.summary)
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
        new_progress = any([self.quality.milestone('module:' + key) for key in verdict.module_ids])
        if new_progress or not self.quality.config.get('verified_progress'):
            self._no_progress = 0
        self._progress_reset |= new_progress
        _continue_remaining_modules(verdict)

    def _write_state(
        self,
        records: list[RoundRecord],
        status: str,
        stall: int,
        gap_key: str,
        tree: str,
        result_summary: str | None = None,
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
            "workflow": self.config.workflow,
            "function_dispatch": self.config.function_dispatch,
            "function_gate": self._function_gate,
            "function_tasks": self.function_tasks.export(),
            "dispatch_directions": self._dispatch_directions,
            "function_issue_ids": self._function_issue_ids,
            "last_invocation": self._last_invocation,
            "delivery_diagnosis": self._delivery_diagnosis,
            "requirements_review": self.requirements_review,
            "requirement_cycles": self.requirement_cycles,
            "quality_policy": self.config.quality_policy,
            "quality_state": self.quality.state,
            "acceptance_baseline_paths": list(self.quality.protected(self.config.cwd)),
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
            "business_input": self._business_input,
            "business_update_count": self._business_update_count,
            "provider_config": self._provider_config(),
            "role_assignment_id": getattr(self, "_role_assignment_id", ""),
            "worker_direction_ids": self._direction_ids['worker'],
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
        if result_summary is not None:
            state.update(summary=redact(result_summary), stop_reason=status)
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

    def _resume_failure(self, exc):
        # Loading mutates fields in stages. Never finalize a partially restored
        # object over the original checkpoint, report, handoff or baseline.
        summary = redact(f"Cannot resume checkpoint: {type(exc).__name__}: {exc}")
        self.log(summary)
        status = 'invalid' if isinstance(exc, (OSError, ValueError, TypeError, AttributeError)) else 'recovery_failed'
        try:
            original = json.loads((self.run_dir / 'state.json').read_text())
        except (OSError, ValueError):
            original = None
        if not isinstance(original, dict):
            return RunReport(status, 0, summary, status)
        try:
            write_json(self.run_dir / 'resume-error.json', {
                'status': 'recovery_failed', 'summary': summary,
                'at': datetime.now(timezone.utc).isoformat()}, durable=True)
        except OSError as error:
            self.log('Could not save recovery diagnostic: ' + str(error))
        return RunReport(status, 0, summary, status)

    def _load_resume(
        self,
    ) -> tuple[list[RoundRecord], int, str, str, int, int, Verdict | None, Verdict | None] | None:
        path = self.run_dir / "state.json"
        if not path.exists():
            raise ValueError("state.json is missing")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state.json must contain an object")
        saved_policy = state.get('quality_policy', {})
        saved_workflow=state.get('workflow','legacy')
        if self.config.workflow not in ('auto',saved_workflow):
            raise ValueError('Workflow cannot change on resume')
        self.config.workflow=saved_workflow
        saved_dispatch = state.get('function_dispatch', False)
        if type(saved_dispatch) is not bool or self.config.function_dispatch and not saved_dispatch:
            raise ValueError('Function dispatch mode cannot change on resume')
        self.config.function_dispatch = saved_dispatch
        self._function_gate = state.get('function_gate', False)
        if type(self._function_gate) is not bool or self._function_gate and not saved_dispatch:
            raise ValueError('Invalid saved function gate')
        self._function_baseline = None
        self.function_tasks = function_tasks.FunctionTasks(state.get('function_tasks'))
        self._dispatch_directions = state.get('dispatch_directions', [])
        self._function_issue_ids = state.get('function_issue_ids', [])
        if (not isinstance(self._function_issue_ids, list) or len(self._function_issue_ids)>128
                or any(not isinstance(x, str) or not re.fullmatch(r'AUD-[1-9][0-9]{0,11}', x) for x in self._function_issue_ids)):
            raise ValueError('Invalid saved function audit references')
        self._last_invocation = state.get('last_invocation') or {}
        if not isinstance(self._dispatch_directions, list) or any(not isinstance(x, str) for x in self._dispatch_directions):
            raise ValueError('Invalid saved function assignment directions')
        self.requirements_review=state.get('requirements_review',{})
        if not isinstance(self.requirements_review,dict):
            raise ValueError('Invalid saved requirement elaboration')
        self.requirement_cycles = _restore_requirement_cycles(state.get('requirement_cycles', []))
        # read_text normalizes CRLF, which would invalidate legacy hashes made
        # from the original Web form text. Validate exact saved bytes as well as
        # the canonical newline representation; never trust incoming replacement text.
        original=(self.run_dir/'requirement.md').read_bytes().decode('utf-8').strip()
        if self.requirements_review.get('status')=='ready':
            requirements_review.parse(json.dumps({k:v for k,v in self.requirements_review.items() if k in requirements_review.SCHEMA['required']}))
            hashes = {hashlib.sha256(original.encode()).hexdigest(), requirements_review.requirement_hash(original)}
            if self.requirements_review.get('original_sha256') not in hashes:
                raise ValueError('Requirement elaboration belongs to another original request')
        if self.requirement_cycles and not requirements_review.requests_follow_up(original):
            raise ValueError('Saved follow-up cycles do not belong to this requirement')
        if not isinstance(saved_policy, dict):
            raise ValueError('Invalid saved quality policy')
        if saved_policy:
            from auto_moniter.quality import policy
            saved_policy = policy(saved_policy)
        Quality(saved_policy, {}, state.get('quality_state'))
        if self.config.quality_policy and self.config.quality_policy != saved_policy:
            raise ValueError('Quality policy cannot change on resume; create a follow-up task')
        self.config.quality_policy = saved_policy
        self._saved_quality_state = state.get('quality_state')
        if state.get("state_version", 1) != 1:
            raise ValueError("Unsupported state version; use a compatible auto-moniter release")
        if state.get("cwd") and Path(state["cwd"]).resolve() != self.config.cwd:
            raise ValueError("saved workspace does not match --cwd")
        status = str(state.get("status") or "")
        if status and status not in {"running", "stopped", "blocked", "budget_exceeded", "recovery_failed", "action_required"}:
            self.log(f"resume refused: run already finished with status {status}")
            self._resume_refused = status
            return None
        reassignment = None
        if "provider_config" in state:
            from .role_changes import authorized_change, normalize_provider_config
            saved_config = normalize_provider_config(state["provider_config"])
            reassignment = authorized_change(self.run_dir, saved_config, self._provider_config())
            if saved_config != self._provider_config() and not reassignment:
                raise ValueError("saved worker/reviewer providers, backends, modes, or command templates do not match; resume with the original configuration")
        self.worker_session_id = str(state.get("session_id") or "")
        self.reviewer_session_id = str(state.get("reviewer_session_id") or "")
        self._active_reviewer_id = str(state.get("active_reviewer_session_id") or "")
        self._role_assignment_id = state.get("role_assignment_id", "")
        if reassignment and reassignment['id'] != self._role_assignment_id:
            if reassignment.get('models') != {'worker': self.config.model or '', 'reviewer': self.config.reviewer_model or ''}:
                raise ValueError('Role assignment models do not match the saved task override')
            self.worker_session_id = self.reviewer_session_id = self._active_reviewer_id = ''
            self._role_assignment_id = reassignment['id']
            self._event('roles_reassigned', assignment_id=self._role_assignment_id)
        self._no_progress = int(state.get("no_progress_turns") or 0)
        self._progress_reset = state.get("progress_pending", False)
        if type(self._progress_reset) is not bool:
            raise ValueError("invalid saved progress marker")
        self._recovery_count = int(state.get("recovery_count") or 0)
        self._business_update_count = int(state.get('business_update_count') or 0)
        self._business_input = state.get('business_input') or {}
        if not isinstance(self._business_input, dict) or self._business_update_count < 0:
            raise ValueError('Invalid business input recovery state')
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
        self._worker_seconds_since_review = schedule.get('worker_seconds_since_review', 0.0)
        if (type(self._worker_seconds_since_review) not in (int, float)
                or not math.isfinite(self._worker_seconds_since_review) or self._worker_seconds_since_review < 0):
            raise ValueError('invalid saved review elapsed time')
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
        self.meter = UsageMeter(usage.get('provider_reported'))
        self._elapsed_before = float(usage.get("elapsed_s") or 0)
        try:
            heartbeat = json.loads(self._heartbeat.read_text(encoding="utf-8"))
            last_tick = datetime.fromisoformat(heartbeat["at"])
            recorded = datetime.fromisoformat(usage["recorded_at"])
            self._elapsed_before += max(0, (last_tick - recorded).total_seconds())
        except (OSError, ValueError, TypeError, KeyError):
            pass
        self._agent_calls = int(usage.get("agent_calls") or 0)
        for name in ("memory_bytes", "file_bytes", "max_run_bytes", "max_history_bytes", "max_agent_state_bytes"):
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
                if saved is None or any(saved.get(key, []) != item.get(key, []) for key in ("title", "acceptance", "checks", "depends_on", "files") if key in item):
                    raise ValueError("module definitions cannot change on resume")
            self.modules = saved_modules
        current = self.function_tasks.current
        if current and current.get('kind') == 'integration':
            function_gate.verify_integration_approval(current, self._integration_scopes())
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
        covered_directions = state.get('worker_direction_ids', [])
        if (not isinstance(covered_directions, list) or len(covered_directions) > 64
                or any(not isinstance(v, str) for v in covered_directions)):
            raise ValueError('Invalid saved worker direction coverage')
        self._direction_ids['worker'] = covered_directions
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
        if self.config.function_dispatch:
            task = self.function_tasks.current
            return '\n'.join([
                'Verify one reviewer-owned assignment without editing the workspace.' if self._verification_only()
                else 'Implement one reviewer-owned assignment in the working directory.',
                json.dumps(task, ensure_ascii=False),
                function_tasks.verdict_contract('worker', task),
                'Latest review of this assignment:',
                json.dumps({k: getattr(last_review, k) for k in ('summary','gaps','next_actions','blockers')},
                           ensure_ascii=False) if last_review and last_review.task_id == task['id'] else '(none)',
                'Preserve earlier accepted functions and existing test assertions. Do not execute prose as watcher checks.',
            ])
        snapshot = workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        if (index == 1 or self.requirement_cycles) and last_worker is None and last_review is None:
            parts = [
                "You are the implementation agent in an unattended development loop.",
                "A person stated the requirement below. Implement it in the working directory.",
                "Work through the original requirement module by module. A completed module uses scope=module; "
                "only completion of ALL original modules and their integration uses scope=requirement.",
                "Do the work yourself with tools. Do not ask the person to continue, confirm, or choose among reasonable options.",
                "The person authorized continuous work on this requirement. Resolve routine choices yourself within that scope.",
                "If you asked a question or requested confirmation, continue immediately using reasonable defaults; no reply is needed.",
                "A blocker does not end monitoring: keep doing independent work and recheck the dependency without inventing access.",
                "Within this turn's declared scope, check missing behavior, tests, edge cases, and incomplete wiring.",
                "When a module is implemented and verified, return status=done, scope=module and its module_ids. "
                "Other pending modules and waiting for independent review are not gaps in that completed module. "
                "The watcher reviews, verifies and commits it before continuing the original requirement.",
                "Keep capability inventory bounded to what the next implementation needs; use real data dependencies, "
                "not a complete audit of the whole repository as a prerequisite for every user-visible change.",
                "",
                "Requirement:",
                self.config.requirement.strip(),
            ]
            if self.requirement_cycles:
                parts.extend([
                    "",
                    "Committed requirement cycles (preserve this accepted work; do not reopen it as a new requirement):",
                    json.dumps(self.requirement_cycles, ensure_ascii=False),
                    "",
                    "The reviewer elaborated the next requirement after the previous local commit. "
                    "Implement that elaboration. A repair of an already accepted defect is not a new requirement cycle.",
                ])
            if self.config.acceptance.strip():
                parts.extend(["", "Acceptance checks:", self.config.acceptance.strip()])
            if self.config.checks:
                parts.extend(["", "Monitor acceptance commands (must pass before final delivery):", json.dumps(self.config.checks, ensure_ascii=False)])
            if self._recovery_note:
                parts.extend(['', 'Recovery guidance:', self._recovery_note])
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
                "Continue the same task from the saved checkpoint and independent review.",
                "If the previous module passed review, preserve it and implement the next unfinished module "
                "from the original requirement. Recheck the whole original requirement for remaining modules; "
                "a module milestone is not completion of the entire requirement.",
                "This is a fresh session if you do not remember earlier edits. Read the workspace before changing it.",
                "Close every open gap. Add the missing behavior, tests, and checks.",
                "Address the named blocking findings as a bounded repair. Preserve verified behavior and unchanged "
                "evidence; do not rewrite it simply because another round started. Run focused checks for the changed "
                "behavior, then one relevant regression suite. Do not run equivalent full discovery commands twice "
                "without a changed input or observed failure. The watcher executes the fixed delivery checks itself. "
                "Keep evidence concise: real command, exit status, source version, remaining limitations. "
                "A reviewer transport or conclusion-format failure requires reviewer repair, not implementation edits.",
                "If this module's repairs and focused checks are complete, submit status=done, scope=module "
                "and the repaired module_ids now. Pending unrelated modules or reviewer approval are not "
                "implementation gaps in this module. Do not expand to other modules while this handoff is ready.",
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
                *(["Dispatched assignment: " + json.dumps(self.function_tasks.current, ensure_ascii=False),
                   function_tasks.verdict_contract('reviewer', self.function_tasks.current),
                   "Previously accepted function IDs: " + json.dumps([x['task']['id'] for x in self.function_tasks.accepted])]
                  if self.config.function_dispatch else []),
                *([self._function_planning_contract()] if self._function_gate else []),
                "You are an independent reviewer in an unattended development loop.",
                "You are read-only. Do not edit files, commit, push, or change configuration.",
                ("This is verification-only work: neither model may edit files. Return a bounded implementation assignment for concrete defects. "
                 if self._verification_only() else "The worker alone implements fixes and runs builds or tests that write files. ")+
                "Inspect code and existing evidence; the watcher runs approved acceptance checks. "
                "Do not run commands that generate files or alter the workspace.",
                "Inspect the working tree yourself. Do not trust the worker's summary.",
                "Recheck previously reported audit gaps and failed monitor checks against the current code and evidence. "
                "Do not drop an unresolved issue because the worker restarted or claimed it was fixed. "
                "The previous conclusion is context, not proof; judge it independently. "
                "Previously accepted module milestones do not imply completion of pending modules.",
                "Provide conservative files impact scopes in modules, including implementation, regression assets, "
                "shared configuration and dependencies you inspected. A missing scope requires broad revalidation. "
                "needs_review audit issues retain an earlier closure: check affected behavior and reuse unchanged "
                "evidence, do not demand recoding unless a concrete regression is found. Reconfirm their AUD IDs "
                "with current evidence. Distinguish environment-unavailable checks from implementation defects; "
                "put actual unavailable tools/platforms in blockers, and continue independent work.",
                "Accept the work only when the requirement is implemented and you cannot name a concrete missing behavior, broken path, or missing check.",
                "A done conclusion without evidence is rejected. Cite files, commands, and observations in evidence.",
                "Style nits alone are not gaps. A missing requirement, failing check, or unwired feature is a gap.",
                "If you find gaps, status must be continue and next_actions must tell the worker exactly what to change.",
                "For each blocking gap, give its AUD ID (if assigned), requirement/criterion, exact file or symbol, "
                "reproducible trigger, expected versus observed behavior, minimal fix boundary, and a precise verification "
                "command or assertion. Distinguish a reproduced failure from a code inference. "
                "Prefix each new scoped finding with its actual known module ID and /, e.g. M2/summary. "
                "Keep existing AUD IDs. Findings for future modules remain pending under those modules; "
                "do not assign all findings to the current worker module or block it on unrelated unfinished work. "
                "Inspect ALL execution entry points and the actual caller/submission path in the first audit; "
                "return all currently known blockers together, not one new condition on each round. "
                "Do not invent acceptance criteria beyond the original request and human amendments.",
                "Reuse unchanged verified findings. Recheck the new delta, affected dependencies, open AUD fixes "
                "and integration. A wording/log-format preference is not a blocking product defect unless it "
                "contradicts acceptance or makes evidence materially false. Do not demand both unittest discover "
                "variants when they collect the same suite. The watcher independently executes fixed checks; "
                "do not require the worker to copy future watcher results into source documents before they exist.",
                "Use blocked only when an external secret, credential, or unavailable service stops all progress.",
                "If the worker claims blocked but you can continue without that dependency, status must be continue.",
                "Monitoring continues through questions and blockers. Do not ask the person for permission to continue.",
                "Only return done when the worker explicitly reported done and the deliverable passes your independent checks.",
                "A module completion can be accepted with scope=module and status=done. "
                "The supervisor then continues the original requirement. "
                "Use scope=requirement only after independently checking ALL original modules, integration, "
                "and acceptance checks. Do not treat one deliverable module as completion of the entire requirement.",
                "When the saved request asks for another requirement after delivery, accept only the current elaborated requirement. "
                "The supervisor starts the next read-only elaboration after the local commit. "
                "Do not invent that next requirement, and do not treat a repair of the same defect as a new requirement.",
                "",
                f"Review round: {index}",
                ("Checkpoint: assess only the assigned targets, except for the explicit module/final gate."
                 if self.config.function_dispatch else "Checkpoint: completion candidate" if _accepted(worker)
                 else "Checkpoint: in-progress audit; steer unfinished work without accepting it as delivered"),
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
                "AUD IDs belong only to THIS run's ledger above. IDs quoted in predecessor tasks, documents, "
                "requirements or transcripts are historical references, not current issue identifiers. "
                "Report newly found or inherited defects as plain findings prefixed by their current module ID. "
                "Only IDs present in this ledger may be used in issue_resolutions; never invent or transplant closures.",
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
                ('(Use the immutable review delta below; request the cumulative diff only for affected integration.)'
                 if isinstance(self.git_history, ApprovedHistory) else workspace_diff(self.config.cwd, ignore_dir=self.run_dir)),
                self.git_history.review_context() if self.git_history else '',
                "",
                f"Worker transcript: {output_path}",
                "",
                verdict_instructions(),
            ]
        )

    def _pending_review(self, records):
        """Resume an exact submitted candidate after review transport/format failure."""
        if self.config.workflow != 'review-first' or self.config.no_review or self._pending_directions():
            return None
        if isinstance(self.git_history, ApprovedHistory) and not self.git_history.candidate:
            return None
        worker = next((r for r in reversed(records) if r.role == 'worker'), None)
        if not worker or worker.failure or worker.interruption:
            return None
        review = next((r for r in reversed(records) if r.role == 'reviewer' and r.index == worker.index), None)
        if review and review.watcher_result.get('function_gate') == 'repair' and not self._check_retry_review(review):
            return None
        rejected_audit = bool(review and (review.watcher_result.get('status') == 'rejected'
                                         or review.failure or review.interruption or review.verdict.parse_error
                                         or self._check_retry_review(review)))
        function_ready = bool(self.config.function_dispatch and self.function_tasks.current
                              and self.function_tasks.current['id'] == worker.verdict.task_id
                              and worker.verdict.task_status == 'done')
        if self.config.function_dispatch and (not self.function_tasks.current
                or worker.verdict.task_id != self.function_tasks.current['id']):
            return None
        if not _accepted(worker.verdict) and not rejected_audit and not function_ready:
            return None
        publication = getattr(self.git_history, 'last_business_commit', {})
        published = publication.get('round') == worker.index and publication.get('candidate_tree') == self.git_history.candidate.get('commit') if publication else False
        ready = self._saved_approval(worker.index, worker.tree)
        if published and worker.verdict.module_ids and all(self.modules.items.get(k, {}).get('status') == 'accepted' for k in worker.verdict.module_ids):
            return None
        if review and not (review.failure or review.interruption or review.verdict.parse_error or rejected_audit or published or ready):
            return None
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != worker.tree:
            return None
        return worker

    def _check_retry_review(self, record):
        retry = record.watcher_result.get('check_retry')
        if (not isinstance(retry, dict) or self._pending_directions()
                or retry.get('tree') != record.tree
                or retry.get('tree') != workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
                or retry.get('provider_config') != self._provider_config()
                or retry.get('reviewer_model') != self._reviewer_model()
                or retry.get('task') != self.function_tasks.current
                or retry.get('direction_ids') != [m['id'] for m in self.directions.messages()]):
            return None
        value = retry.get('review')
        if not isinstance(value, dict):
            return None
        review = parse_verdict(json.dumps(value), function_gate=self._function_gate)
        if review.parse_error or not (_accepted(review) or review.task_status == 'accepted'):
            return None
        return review

    def _saved_approval(self, index, tree):
        path = self.run_dir / 'approval-ready.json'
        if (not getattr(self.git_history, 'business_commits', False) or not path.is_file()
                or path.is_symlink() or path.stat().st_size > 512*1024):
            return None
        value = json.loads(path.read_text())
        if (value.get('round') != index or value.get('tree') != tree
                or value.get('candidate_tree') != self.git_history.candidate.get('commit')
                or value.get('business_head') != self.git_history.business_head):
            return None
        return value

    def _invoke_review(self, provider, prompt, session_id, **kwargs):
        """One bounded repair belongs to the reviewer, never to the worker."""
        self._review_audit_rejection = None
        tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        previous = next((r for r in reversed(self._records) if r.role == 'reviewer' and r.index == kwargs['index']), None)
        retry = self._check_retry_review(previous) if previous else None
        if retry:
            self._event('acceptance_review_reused', round=kwargs['index'], tree=tree)
            return _CompletedTurn(json.dumps(retry.to_dict()), '', 0, False, 0,
                                  self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        ready = self._saved_approval(kwargs['index'], tree)
        if (ready and ready.get('provider_config') == self._provider_config()
                and ready.get('reviewer_model') == self._reviewer_model()):
            conclusion = parse_verdict(json.dumps(ready['review']))
            if not _accepted(conclusion):
                raise RecoveryError('Saved approval is invalid')
            self._publication_review_reused = kwargs['index']
            self._event('approval_review_reused', round=kwargs['index'])
            return _CompletedTurn(json.dumps(ready['review']), '', 0, False, 0,
                                  self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        publication = getattr(self.git_history, 'last_business_commit', {})
        if (publication.get('round') == kwargs['index']
                and publication.get('candidate_tree') == self.git_history.candidate.get('commit')):
            self.git_history.verify()
            record = self.git_history.last_record
            payload = json.loads(self.git_history.git('show', record['commit'] + ':' +
                                 (self.config.cwd.relative_to(self.git_history.root) / record['record']).as_posix()))
            if payload.get('role') != 'reviewer' or payload['result'].get('tree') != tree:
                raise RecoveryError('Committed review does not match the resumed candidate')
            self._event('publication_review_reused', round=kwargs['index'], commit=record['commit'])
            self._publication_review_reused = kwargs['index']
            return _CompletedTurn(json.dumps({**payload['result'], 'modules': []}), '', 0, False, 0,
                                  self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        for attempt in range(2):
            turn = self._invoke(provider, prompt, session_id if attempt == 0 else '', **kwargs)
            review = self._verdict_from_turn(turn, role='reviewer')
            if self.git_history and (_accepted(review) or self.config.function_dispatch and review.task_status == 'accepted'):
                problems = self.git_history.review_problems(review.evidence)
                if problems:
                    review.status = 'continue'
                    review.parse_error = 'invalid review checkpoint evidence'
                    review.gaps.extend(problems)
            failure = _failure_reason(turn, review)
            if (not failure and not review.parse_error and not turn.interruption
                    and self.config.workflow == 'review-first'
                    and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == tree):
                # Validate ledger operations inside the existing reviewer-only
                # repair loop, not after its retry opportunity has already passed.
                import copy
                original_findings = _audit_findings(review)
                local, references = self.audit.local_findings(original_findings)
                if references:
                    converted = dict(zip(original_findings, local))
                    for field in ('gaps', 'blockers', 'next_actions'):
                        setattr(review, field, [converted.get(text, text) for text in getattr(review, field)])
                    # Keep the provider transcript unchanged; the structured
                    # result used by the watcher carries explicit provenance.
                    turn.text = json.dumps(review.to_dict(), ensure_ascii=False)
                    self._event('audit_external_references_normalized', round=kwargs['index'], references=references)
                staged = copy.deepcopy(self.audit)
                findings, _ = staged.local_findings(_audit_findings(review))
                worker = next((r.verdict for r in reversed(self._records) if r.role=='worker'), Verdict('continue'))
                modules = sorted(set(worker.module_ids) & self.modules.items.keys()) if worker.scope=='module' else []
                def evidence_errors(evidence):
                    return acceptance_problems(self.config.cwd, evidence, baseline=self._baseline_files,
                        ignore_dir=self.run_dir, git_base=self.git_history.initial if getattr(self.git_history, 'business_commits', False) else '')
                try:
                    staged.update(findings, review.issue_resolutions, kwargs['index'], tree, modules,
                                  evidence_errors, known_modules=self.modules.items)
                except AuditCapacityError:
                    raise
                except ValueError as exc:
                    # Retain substantive findings even if a closure is invalid.
                    # No proposed closure is applied until the whole payload is valid.
                    reported = self.audit.record(findings, kwargs['index'], modules, known_modules=self.modules.items)
                    if reported:
                        if self.config.function_dispatch:
                            self._function_issue_ids = sorted(set(self._function_issue_ids) | set(reported))
                        self._event('audit_findings_preserved', issues=reported, round=kwargs['index'])
                    review.parse_error = 'invalid audit update: ' + str(exc)
                    review.status = 'continue'
                    review.gaps.append(review.parse_error)
                    if attempt:
                        self._review_audit_retry_exhausted = kwargs['index']
                        self._review_audit_rejection = {'round': kwargs['index'], 'reason': str(exc)}
                if (self.config.function_dispatch and self.function_tasks.current
                        and review.task_status == 'accepted' and not review.parse_error
                        and (not self._function_gate or not self._function_scope_problems())):
                    problems = self._function_evidence_problems(review, 'reviewer')
                    if problems:
                        review.parse_error = 'invalid function evidence'
                        review.status = 'continue'
                        review.gaps.extend(problems)
            invalid = failure or review.parse_error or turn.interruption
            if not invalid or self._stop or self.config.workflow != 'review-first':
                return turn
            if attempt or (failure and classify(failure)[1] == 'stop'):
                return turn
            if (self._pending_directions()
                    or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree):
                return turn
            self._event('reviewer_repair', round=kwargs['index'], reason=str(invalid)[:1000])
            repair_id = self._agent_calls
            failed_log = turn.output_path.with_name(f"{kwargs['index']:03d}-reviewer-repair-{repair_id}.log")
            turn.output_path.replace(failed_log)
            failed_text = self.run_dir / f"{kwargs['index']:03d}-reviewer-text.md"
            if failed_text.exists():
                failed_text.replace(self.run_dir / f"{kwargs['index']:03d}-reviewer-repair-{repair_id}-text.md")
            write_json(self.run_dir / 'review-repair.json',
                       {'round': kwargs['index'], 'tree': tree, 'attempt': attempt + 1,
                        'reason': str(invalid)[:2000], 'feedback': review.to_dict()}, durable=True)
            rejected = RoundRecord(kwargs['index'], 'reviewer', review, turn.exit_code,
                                   turn.timed_out, turn.elapsed_s, turn.session_id, str(failed_log),
                                   tree=tree, failure=str(invalid), interruption=turn.interruption,
                                   watcher_result={'status': 'not_applied', 'closed': [], 'reasons': [str(invalid)],
                                                   'issues': {v['id']: v['status'] for v in self.audit.items}})
            self._records.append(rejected)
            saved = json.loads((self.run_dir / 'state.json').read_text())
            self._write_state(self._records, 'running', saved.get('stall', 0), saved.get('gap_key', ''), tree)
            # Keep the read-only tool and provider boundary. The next invocation is
            # charged to the existing cumulative call/time budget.
            marker = 'Previous independent review and monitor acceptance feedback:\n'
            if marker in prompt:
                start = prompt.index(marker) + len(marker)
                end = prompt.index('\n\nLast completed business audit', start)
                prompt = prompt[:start] + json.dumps(review.to_dict(), ensure_ascii=False, indent=2) + prompt[end:]
            prompt += ('\n\nReviewer-only repair of the SAME candidate. Worker has not changed it. '
                       'Retain already verified substantive findings; correct only the failed conclusion '
                       'or complete the interrupted audit. Do not invent implementation work.\n'
                       + json.dumps(review.to_dict(), ensure_ascii=False) + '\nValidation error: ' + str(invalid)
                       + '\nAuthoritative current-run audit ledger after preserving findings:\n'
                       + json.dumps(self.audit.prompt(), ensure_ascii=False))
        return turn

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
        self._last_invocation = {'role': role, 'round': index, 'call': self._agent_calls}
        if self.function_tasks.current:
            self._last_invocation['task_id'] = self.function_tasks.current['id']
        state['last_invocation'] = self._last_invocation
        write_json(state_path, state, durable=True)
        prompt_path = self.run_dir / f"{index:03d}-{role}-prompt.md"
        if self.requirements_review.get("status")=="ready" and not (role=='worker' and self.config.function_dispatch):
            prompt += "\n\nWatcher-approved requirement elaboration (supplements, never replaces, the original request):\n"+json.dumps(self.requirements_review,ensure_ascii=False)
        if self.config.quality_policy:
            prompt += '\n\nMonitor-owned quality policy (immutable):\n' + json.dumps(self.quality.config, ensure_ascii=False)
            prompt += '\nMap each named requirement to actual file evidence and its modules. File edits alone are not verified progress. '
            if role=='worker' and self.config.function_dispatch:
                prompt += 'When blocked, report the dependency to the reviewer; do not take work outside this assignment. '
            else:
                prompt += 'When blocked, continue independent modules before repeating the same dependency. '
            prompt += 'Never modify existing protected test assertions.\n'
        if role == 'worker' and self.config.workflow == 'review-first':
            prompt += (('\nVerification time management: inspect the current assignment without edits; report remaining behavior gaps. '
                        if self._verification_only() else '\nWorker time management: implement one reviewer-approved item or bounded repair at a time. ')+
                       'Complete a coherent change with focused verification and return the required JSON conclusion. '
                       'Do not bundle all remaining items into one long coding turn or repeat equivalent full suites. '
                       'The review interval schedules an audit after you return; it does not interrupt active work. '
                       f'Turn hard limit: {self.config.turn_timeout_s:g}s (0 means no turn limit). '
                       f'Idle-output limit: {self.config.idle_timeout_s:g}s. ')
            if self.config.max_runtime_s:
                prompt += f'Remaining total budget now: {max(0, self.config.max_runtime_s - self._elapsed()):.0f}s. '
            prompt += 'Preserve time to report evidence, remaining gaps and a safe handoff before these hard limits.\n'
            prompt += ('Environment failures are not coding defects: preserve known missing-tool/platform evidence. '
                       'Do not repeat an unavailable build or create substitute platforms to claim full validation. '
                       + ('Continue only work inside the dispatched scope and focused runnable checks; ' if self.config.function_dispatch
                          else 'Continue independent implementation and focused runnable checks; ') +
                       'if only external validation '
                       'remains, return blocked with the exact dependency and evidence.\n')
        messages = self.directions.messages()
        if role=='worker' and self.config.function_dispatch:
            # Reviewer already received these instructions when defining the
            # assignment. Keep the worker input limited to the approved task.
            approved = set(self._dispatch_directions)
            self._direction_ids[role] = [m['id'] for m in messages if m['id'] in approved]
            messages = []
        blocked_path = self.run_dir / 'environment-blocks.json'
        if blocked_path.exists() and not blocked_path.is_symlink() and blocked_path.stat().st_size <= 65536:
            prompt += '\nKnown environment validation gaps (not proof of code defects):\n' + blocked_path.read_text()
        if role == 'worker' and self.git_history:
            destination = (self.git_history.business_head['ref'] if getattr(self.git_history, 'business_commits', False)
                           else self.git_history.ref)
            prompt += (('\n\nDo not commit, push, stage or change Git refs. Only the watcher may create local commits AFTER independent approval and checks.\n' if self.config.workflow=='review-first' else '\n\nThe monitor records this round in Git.\n')+
                       f'Approved code destination: {destination}. Audit reference: {self.git_history.ref}. Keep edits in {self.config.cwd}. '
                       'Do not alter .auto-moniter/history, the audit branch, or previous audit records. '
                       'Report changed files, actual validation results, remaining gaps and failures precisely.\n')
        if not (role=='worker' and self.config.function_dispatch):
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
            self.directions.receipt(self._direction_ids[role], role, max(1,index))
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
            if read_only and role == 'reviewer' and self.quality.config.get('review_tools') == 'files_only':
                prepared.argv += ['--tools', 'Read,Glob,Grep', '--restricted', '--safe-mode', '--strict-mcp-config', '--mcp-config', '{}']
                # Keep only the model's own authentication; exclude common
                # deployment/database credentials inherited by the service.
                secret_prefixes = ('AWS_', 'AZURE_', 'GOOGLE_APPLICATION_', 'DATABASE_', 'PGPASSWORD',
                                   'MYSQL_', 'REDIS_', 'KUBECONFIG', 'SSH_AUTH_SOCK', 'DOCKER_')
                scrub = [key for key in os.environ if key.startswith(secret_prefixes)]
                prepared.argv = ['env', *[part for key in scrub for part in ('-u', key)], *prepared.argv]
            if read_only and self._isolation:
                prepared = self._isolation.wrap(prepared, provider.name)
            if not read_only and self._worker_isolation:
                prepared = self._worker_isolation.wrap(prepared, provider.name)
            timeout = self.config.turn_timeout_s
            from auto_moniter.runtime_policy import reserve_seconds
            reserve = reserve_seconds(self.config.max_runtime_s)
            if reserve and role == 'worker':
                available = max(.01, self.config.max_runtime_s-self._elapsed()-reserve)
                timeout = min(timeout, available) if timeout else available
            if role == 'worker' and self.config.workflow != 'review-first' and self.quality.config.get('review_interval_s'):
                interval = self.quality.config['review_interval_s']
                timeout = min(timeout, interval) if timeout else interval
            result = run_command(
                prepared, self.config.cwd, timeout_s=timeout,
                idle_timeout_s=self.config.idle_timeout_s,
                on_child=on_started, heartbeat=self._heartbeat,
                heartbeat_note=f"round {index}: {role} running",
                should_stop=self._should_stop,
                max_output_bytes=self.config.max_output_bytes,
                memory_bytes=self.config.memory_bytes, file_bytes=self.config.file_bytes,
                on_output=live_output.feed,
                network_retry_s=self.quality.config.get('network_retry_s', 120),
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
        # Persist captured output before session cleanup or the post-turn budget
        # check. A forced stop must not erase the interrupted round.
        output_path = self._write_turn_log(provider, prepared, role, index, found, result, text, elapsed)
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
                # Waiting puts an already captured transcript in text. A later
                # empty transcript read must not discard it.
                if result.text and result.text not in (text or ""):
                    text, structured = result.text, False
            else:
                if not _paseo_stop(provider.binary, found, self.config.cwd):
                    result.exit_code = result.exit_code or 1
                    result.interruption = result.interruption or self._stop_reason or "paseo stop failed"
                    result.stderr = (result.stderr + "\n" if result.stderr else "") + f"Cannot stop interrupted Paseo {role} {found}; no new turn will start"
                    self._write_turn_log(provider, prepared, role, index, found, result, text, elapsed)
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
            elif result.text:
                text, structured = result.text, False
        if provider.name == "paseo" and found:
            # Launch output is not the turn. Rewrite the round log with the
            # waited result and transcript before any budget or stop exception.
            output_path = self._write_turn_log(provider, prepared, role, index, found, result, text, elapsed)
        if "Cannot stop interrupted Paseo" in (result.stderr or ""):
            raise RecoveryError(next(line for line in result.stderr.splitlines() if "Cannot stop interrupted Paseo" in line))
        self.meter.record(result.stdout)
        if self.config.quality_policy and (
                self.quality.config['max_tokens'] and not self.meter.last_tokens_reported
                or self.quality.config['max_cost_usd'] and not self.meter.last_cost_reported):
            self._meter_missing = True
        current_state = json.loads(state_path.read_text())
        current_state['usage'] = self._usage()
        write_json(state_path, current_state, durable=True)
        # Captured output is already on disk. A budget stop must not discard it.
        self._check_budget()
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

    def _write_turn_log(self, provider, prepared, role, index, found, result, text, elapsed):
        output_path = self.run_dir / f"{index:03d}-{role}.log"
        # Successful command listings stay out of the durable round log. Keep
        # the conclusion, the exit line, and failed-command diagnostics only.
        diagnosis = diagnostic_summary(result.stdout, result.stderr, failed=result.exit_code != 0)
        # A stopped turn has no structured conclusion yet. Keep a short plain
        # prefix of what was already captured, never a successful command dump.
        partial = ""
        if result.interruption and not text:
            plain = []
            for line in (result.stdout or "").splitlines():
                stripped = line.strip()
                if not stripped or stripped[:1] in "{[":
                    continue
                plain.append(stripped)
                if len(plain) == 4:
                    break
            partial = "\n".join(plain)[:1000]
        output_path.write_text(
            "\n".join(
                [
                    f"$ {redact_command(prepared.argv[:2])}",
                    f"role={role} provider={provider.name} session={found or '-'}",
                    f"exit={result.exit_code} timed_out={result.timed_out} interruption={result.interruption or '-'} elapsed_s={elapsed:.2f}",
                    "----- conclusion -----",
                    clipped(redact(text or partial)),
                    redact(diagnosis),
                ]
            ),
            encoding="utf-8",
        )
        (self.run_dir / f"{index:03d}-{role}-text.md").write_text(clipped(redact(text or partial)), encoding="utf-8")
        return output_path

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
            try:
                self._check_budget()
            except BudgetExceeded:
                self._stop = True
                self._stop_reason = self._stop_reason or "runtime budget"
            if self._should_stop() or self._stop:
                latest = TurnResult(previous_text, stdout=previous_text, exit_code=-1, interruption=self._stop_reason or "runtime budget")
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
            captured = previous_text or latest.text or ""
            if captured:
                # Keep the text in stdout. The caller re-reads stdout, not text.
                latest.text = captured
                latest.stdout = captured
            if not _paseo_stop(provider.binary, session_id, self.config.cwd):
                # Save happens in the caller before this failure leaves _invoke.
                latest.exit_code = latest.exit_code if latest.exit_code not in (0, None) else 1
                latest.interruption = latest.interruption or self._stop_reason or "paseo stop failed"
                latest.stderr = (latest.stderr + "\n" if latest.stderr else "") + f"Cannot stop interrupted Paseo {role} {session_id}; no new turn will start"
            return latest
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


def _restore_requirement_cycles(value):
    if not isinstance(value, list) or len(value) > requirements_review.MAX_CYCLES:
        raise ValueError('Invalid saved requirement cycles')
    cycles = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'index', 'status', 'requirements_review', 'handoff'}:
            raise ValueError('Invalid saved requirement cycle')
        if type(item['index']) is not int or item['index'] < 1 or item['status'] != 'committed':
            raise ValueError('Invalid saved requirement cycle index')
        review = item['requirements_review']
        handoff = item['handoff']
        if not isinstance(review, dict) or not isinstance(handoff, dict):
            raise ValueError('Invalid saved requirement cycle payload')
        requirements_review.parse(json.dumps({k: v for k, v in review.items() if k in requirements_review.SCHEMA['required']}))
        for key in ('cycle', 'round', 'tree', 'commit', 'ref', 'summary', 'evidence', 'acceptance', 'modules'):
            if key not in handoff:
                raise ValueError('Saved requirement handoff is incomplete')
        if not isinstance(handoff['evidence'], list) or not isinstance(handoff['modules'], list):
            raise ValueError('Saved requirement handoff evidence is invalid')
        cycles.append(item)
    return cycles


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
        watcher_result=dict(item.get('watcher_result') or {}),
        reported_status=str(item.get('reported_status') or ''),
        delivery=dict(item.get('delivery') or {}),
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
        task_id=value.get("task_id", ""), task_status=value.get("task_status", ""), next_task=value.get("next_task"),
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
    network_retry_s: float = 0,
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
    from auto_moniter.runtime_policy import RetryWatch
    retries = RetryWatch(network_retry_s)
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
                if retries.exhausted():
                    interruption = 'network retry budget'
                elif detect_input_requests and any(watcher.waiting for watcher in watchers) and idle_s >= input_grace_s:
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
                    retries.feed(key.data, chunk)
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
