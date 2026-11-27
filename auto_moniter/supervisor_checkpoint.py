"""Save, restore and retain supervision checkpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import gzip
import hashlib
import json
import math
import os
import re

from auto_moniter.verdict import Verdict
from auto_moniter.modules import ModuleLedger
from auto_moniter.storage import write_json
from auto_moniter.history import prune_files
from auto_moniter.redaction import redact, safe
from auto_moniter.audit import AuditLedger
from auto_moniter import check_history, function_gate, function_tasks
from auto_moniter.quality import Quality, validate_check_inputs
from auto_moniter.metering import UsageMeter
from auto_moniter import requirements_review
from auto_moniter import check_environment
from auto_moniter import function_plan
from auto_moniter import referee
from auto_moniter.supervisor_models import (
    RoundRecord,
    RunReport,
    _EXECUTION_DEFAULTS,
    _audit_findings,
    _consecutive_failures,
)


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
        supplemental_checks=value.get('supplemental_checks') or [],
    )


class RunCheckpoint:
    """Save, restore and retain supervision checkpoints."""

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
            "task_type": self.config.task_type,
            "function_dispatch": self.config.function_dispatch,
            "function_gate": self._function_gate,
            "function_tasks": self.function_tasks.export(),
            "function_plan": self.function_plan,
            "full_function_plan": self.config.full_function_plan,
            "referee": self._referee_snapshot(),
            "deferred_replan": '' if self.config.review_saved_round else self._deferred_replan,
            "dispatch_directions": self._dispatch_directions,
            "function_issue_ids": self._function_issue_ids,
            "planning_rejection": self._planning_rejection,
            "last_invocation": self._last_invocation,
            "last_worker_invocation": self._last_worker_invocation,
            "delivery_diagnosis": self._delivery_diagnosis,
            "requirements_review": self.requirements_review,
            "requirement_cycles": self.requirement_cycles,
            "requirement_revision": self.directions.revision(self.config.requirement),
            "active_queued_requirement": self.active_queued_requirement,
            "requirement_queue_version": 1,
            "quality_policy": self.config.quality_policy,
            "quality_state": self.quality.state,
            "acceptance_baseline_paths": list(self.quality.protected(self.config.cwd)),
            "directions_version": 1,
            "git_history": self.git_history.export() if self.git_history else {'enabled': False},
            "baseline_file": baseline_path.name,
            "baseline_count": len(getattr(self, "_baseline_files", {})),
            "acceptance": self.config.acceptance,
            "checks": self.config.checks,
            "check_source_aliases": self.config.check_source_aliases or [],
            "check_changes": self._check_changes,
            "execution_settings": {key: getattr(self.config, key) for key in _EXECUTION_DEFAULTS},
            "protect_paths": self.config.protect_paths,
            "assignment_workspaces": self.config.assignment_workspaces or [],
            "assignment_deliveries": self.config.assignment_deliveries or [],
            "assignment_contracts": list((self.config.assignment_contracts or {}).values()),
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
        protected_rounds = set()
        if self.config.full_function_plan and self.function_plan:
            accepted = {row['task']['id'] for row in self.function_tasks.accepted}
            pending = {task['id'] for task in self.function_plan['tasks']
                       if task.get('kind') == 'verification' and task['id'] not in accepted}
            blocked_reviews = {record.verdict.task_id: record.index for record in records
                               if record.role == 'reviewer' and record.verdict.task_id in pending
                               and record.watcher_result.get('status') == 'blocked' and record.verdict.blockers}
            protected_rounds = set(blocked_reviews.values())
        removed = [record for record in records if record.index < first and record.index not in protected_rounds]
        for record in removed:
            if record.role in {'worker', 'reviewer'} and record.verdict.summary:
                self._event('round_concluded', round=record.index, role=record.role, status=record.verdict.status,
                            summary=record.verdict.summary, task_id=record.verdict.task_id,
                            watcher_status=record.watcher_result.get('status', ''))
            self._pruned_failures[record.role] = self._pruned_failures.get(record.role, 0) + 1 if record.failure else 0
            self._pruned_worker_rounds += record.role == "worker"
        self._pruned_records += len(removed)
        records[:] = [record for record in records if record.index >= first or record.index in protected_rounds]
        files = prune_files(self.run_dir, first, protected_rounds=protected_rounds)
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
            original = json.loads((self.run_dir / 'state.json').read_text(encoding='utf-8'))
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
        from auto_moniter.task_types import TASK_TYPES
        saved_task_type = state.get('task_type', 'code')
        if saved_task_type not in TASK_TYPES or self.config.task_type not in ('auto', saved_task_type):
            raise ValueError('Task type cannot change on resume; create a new task')
        self.config.task_type = saved_task_type
        saved_dispatch = state.get('function_dispatch', False)
        if type(saved_dispatch) is not bool or self.config.function_dispatch and not saved_dispatch:
            raise ValueError('Function dispatch mode cannot change on resume')
        self.config.function_dispatch = saved_dispatch
        self._function_gate = state.get('function_gate', False)
        if type(self._function_gate) is not bool or self._function_gate and not saved_dispatch:
            raise ValueError('Invalid saved function gate')
        self._function_baseline = None
        self.function_tasks = function_tasks.FunctionTasks(state.get('function_tasks'))
        saved_full_plan = state.get('full_function_plan', False)
        if type(saved_full_plan) is not bool:
            raise ValueError('Invalid saved complete function planning mode')
        self.config.full_function_plan = self.config.full_function_plan or saved_full_plan
        if self.config.full_function_plan and not self._function_gate:
            raise ValueError('Complete function planning requires the verified function gate')
        from auto_moniter.preflight import validate_task_type
        validate_task_type(self.config)
        self.function_plan = function_plan.restore(state.get('function_plan'))
        self._local_test_paths = list(dict.fromkeys(name for accepted in self.function_tasks.accepted
                                                   if 'external_delivery' not in accepted
                                                   for name in accepted['task'].get('test_files', [])))
        self._dispatch_directions = state.get('dispatch_directions', [])
        self._function_issue_ids = state.get('function_issue_ids', [])
        self._planning_rejection = state.get('planning_rejection')
        if self._planning_rejection is not None and (
                not isinstance(self._planning_rejection, dict)
                or not {'task_id', 'reason'} <= set(self._planning_rejection)
                or set(self._planning_rejection) - {'task_id','reason','scope_repair'}
                or any(not isinstance(self._planning_rejection[k],str) or len(self._planning_rejection[k])>2000
                       for k in ('task_id','reason'))
                or 'scope_repair' in self._planning_rejection and type(self._planning_rejection['scope_repair']) is not bool):
            raise ValueError('Invalid saved function planning rejection')
        if (not isinstance(self._function_issue_ids, list) or len(self._function_issue_ids)>128
                or any(not isinstance(x, str) or not re.fullmatch(r'AUD-[1-9][0-9]{0,11}', x) for x in self._function_issue_ids)):
            raise ValueError('Invalid saved function audit references')
        self._last_invocation = state.get('last_invocation') or {}
        self._last_worker_invocation = state.get('last_worker_invocation') or {}
        if not isinstance(self._last_worker_invocation, dict) or self._last_worker_invocation and (
                self._last_worker_invocation.get('role') != 'worker'
                or type(self._last_worker_invocation.get('round')) is not int
                or self._last_worker_invocation['round'] < 1
                or type(self._last_worker_invocation.get('call')) is not int
                or self._last_worker_invocation['call'] < 1
                or ('task_id' in self._last_worker_invocation
                    and not function_tasks.identifier(self._last_worker_invocation['task_id']))):
            raise ValueError('Invalid saved worker invocation')
        if self._last_invocation.get('role') == 'worker':
            self._last_worker_invocation = dict(self._last_invocation)
        if not isinstance(self._dispatch_directions, list) or any(not isinstance(x, str) for x in self._dispatch_directions):
            raise ValueError('Invalid saved function assignment directions')
        self.requirements_review=state.get('requirements_review',{})
        if not isinstance(self.requirements_review,dict):
            raise ValueError('Invalid saved requirement elaboration')
        self.requirement_cycles = _restore_requirement_cycles(state.get('requirement_cycles', []))
        saved_referee = state.get('referee', {'enabled': False, 'model': '', 'decision': None, 'replan': False})
        if (not isinstance(saved_referee, dict) or set(saved_referee) != {'enabled', 'model', 'decision', 'replan'}
                or type(saved_referee['enabled']) is not bool or type(saved_referee['replan']) is not bool
                or not isinstance(saved_referee['model'], str)):
            raise ValueError('Invalid saved referee configuration')
        if self._referee_override is None:
            self.config.referee = saved_referee['enabled']
        if not self.config.referee_model:
            self.config.referee_model = saved_referee['model']
        decision = saved_referee['decision']
        if decision is not None:
            if (not isinstance(decision, dict)
                    or set(decision) - {'decision_key'} != set(referee.SCHEMA['required']) | {'id','cycle','incident','task_id'}
                    or ('decision_key' in decision and not re.fullmatch(r'[a-f0-9]{64}', str(decision['decision_key'])))
                    or type(decision['id']) is not int or decision['id'] < 1
                    or type(decision['cycle']) is not int or decision['cycle'] < 1):
                raise ValueError('Invalid saved referee decision')
            referee.parse(json.dumps({key: decision[key] for key in referee.SCHEMA['required']}))
        if saved_referee['replan'] and not decision:
            raise ValueError('Referee replanning lacks a saved decision')
        self._referee_decision = decision
        self._referee_replan = bool(self.config.referee and saved_referee['replan'])
        self._deferred_replan = state.get('deferred_replan', '')
        if not isinstance(self._deferred_replan, str) or (self._deferred_replan
                and not re.fullmatch(r'[a-f0-9]{64}', self._deferred_replan)):
            raise ValueError('保存的派单恢复记录无效')
        self.active_queued_requirement = state.get('active_queued_requirement') or None
        self.requirement_queue.snapshot(self._requirement_queue_state())
        # read_text normalizes CRLF, which would invalidate legacy hashes made
        # from the original Web form text. Validate exact saved bytes as well as
        # the canonical newline representation; never trust incoming replacement text.
        original=(self.run_dir/'requirement.md').read_bytes().decode('utf-8').strip()
        if self.requirements_review.get('status')=='ready':
            requirements_review.parse(json.dumps({k:v for k,v in self.requirements_review.items() if k in requirements_review.SCHEMA['required']}))
            hashes = {hashlib.sha256(original.encode()).hexdigest(), requirements_review.requirement_hash(original)}
            if self.requirements_review.get('original_sha256') not in hashes:
                raise ValueError('Requirement elaboration belongs to another original request')
        queue_ids = {item['id'] for item in self.requirement_queue.items()}
        if (self.requirement_cycles and not requirements_review.requests_follow_up(original)
                and any(cycle['handoff'].get('next_queued_requirement') not in queue_ids
                        for cycle in self.requirement_cycles)):
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
        next_round = int(state.get("next_round") or 1)
        if next_round < 1:
            raise ValueError("next_round must be positive")
        has_additional_rounds = status == "max_rounds" and self.config.max_rounds >= next_round
        if (status and status not in {"running", "stopped", "blocked", "budget_exceeded", "recovery_failed", "action_required"}
                and not has_additional_rounds):
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
        from auto_moniter.project_workspace import assignment_workspaces
        saved_workspaces = state.get('assignment_workspaces', [])
        assignment_workspaces(saved_workspaces)
        if self.config.assignment_workspaces is None:
            self.config.assignment_workspaces = saved_workspaces
        from auto_moniter import assignment_delivery
        saved_deliveries = state.get('assignment_deliveries', [])
        assignment_delivery.links(saved_deliveries)
        if self.config.assignment_deliveries is None:
            self.config.assignment_deliveries = saved_deliveries
        saved_contracts = assignment_delivery.contracts(state.get('assignment_contracts', []))
        if self.config.assignment_contracts not in (None, saved_contracts):
            raise ValueError('恢复任务时须保留原任务提供的派单合同。')
        self.config.assignment_contracts = saved_contracts
        self._check_changes = state.get('check_changes', [])
        check_history.configurations(saved_checks, self._check_changes)
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
        if self.config.checks and self.config.checks != saved_checks:
            raise ValueError("acceptance checks cannot change on resume; use the saved checks or a new run")
        self.config.checks = saved_checks
        validate_check_inputs(self.config.quality_policy, saved_checks)
        saved_aliases = state.get('check_source_aliases', [])
        check_environment.source_aliases(saved_aliases, saved_checks)
        if self.config.check_source_aliases is None:
            self.config.check_source_aliases = saved_aliases
        check_environment.source_aliases(self.config.check_source_aliases, saved_checks)
        self._recovery_note = str(state.get("recovery_note") or "")
        usage = state.get("usage") or {}
        self.meter = UsageMeter(usage.get('provider_reported'))
        stage_elapsed = usage.get('stage_elapsed_s')
        if isinstance(stage_elapsed, dict):
            for stage in ('worker', 'reviewer', 'checks'):
                seconds = stage_elapsed.get(stage)
                if type(seconds) in (int, float) and 0 <= seconds <= 1e15:
                    self._stage_elapsed[stage] = float(seconds)
        from auto_moniter.run_timing import restore
        self._elapsed_before = restore(usage, self._heartbeat)
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
        self._next_round = next_round
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
