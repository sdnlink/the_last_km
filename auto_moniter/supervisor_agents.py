"""Invoke model providers and track their sessions."""

from __future__ import annotations

import json
import os
import re
import time

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
from auto_moniter.workspace import LockError, workspace_fingerprint
from auto_moniter.verdict import Verdict, parse_verdict
from auto_moniter.runlog import clipped, diagnostic_summary
from auto_moniter.storage import write_json
from auto_moniter.failures import classify
from auto_moniter.redaction import redact
from auto_moniter.audit import AuditCapacityError
from auto_moniter import candidate_checks
from auto_moniter.live import LiveOutput
from auto_moniter import requirement_context
from auto_moniter import function_plan
from auto_moniter.requirement_queue import prompt_block as queued_requirement_prompt
from auto_moniter import referee
from auto_moniter.supervisor_models import (
    BudgetExceeded,
    RecoveryError,
    RoundRecord,
    _CompletedTurn,
    _audit_findings,
    _failure_reason,
)
from auto_moniter.command_runner import _paseo_stop


class AgentExecution:
    """Invoke model providers and track their sessions."""

    def _invoke_review(self, provider, prompt, session_id, **kwargs):
        """Repair reviewer reports on the saved candidate within the retry budget."""
        self._review_audit_rejection = None
        tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        previous = next((r for r in reversed(self._records) if r.role == 'reviewer' and r.index == kwargs['index']), None)
        worker = next((r for r in reversed(self._records) if r.role=='worker' and r.index==kwargs['index']),None)
        worker_record = worker
        receipt = worker.watcher_result.get('candidate_checks') if worker else None
        behavioral_failure = bool(self.config.function_dispatch and receipt
            and candidate_checks.valid(receipt,self.run_dir,self._candidate_check_context(worker_record))
            and receipt['complete'] and not receipt['passed'] and not receipt['source_changed']
            and not receipt.get('scope_gaps') and receipt['checks']
            and all(item['status'] in {'passed','check_failed'} for item in receipt['checks'])
            and candidate_checks.observed_assertion_failure(receipt,self.run_dir))
        retry = (self._candidate_approval(previous,worker) if receipt else self._check_retry_review(previous)) if previous else None
        if retry and not behavioral_failure:
            context = previous.watcher_result.get('candidate_approval',{}).get('context') if receipt else previous.watcher_result.get('check_retry')
            if isinstance(context, dict):
                self._direction_ids['reviewer'] = list(context['direction_ids'])
            self._event('acceptance_review_reused', round=kwargs['index'], tree=tree)
            return _CompletedTurn(json.dumps(retry.to_dict()), '', 0, False, 0,
                                  self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        if previous and worker and (review := self._previous_handoff_approval(previous, worker, tree)):
            self._event('acceptance_review_reused', round=kwargs['index'], tree=tree)
            return _CompletedTurn(json.dumps(review.to_dict()), '', 0, False, 0,
                                  self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        ready = self._saved_approval(kwargs['index'], tree)
        if behavioral_failure:
            ready = None  # A historical mistaken approval requires actual reassessment.
        if receipt and ready and (ready.get('checks_digest') != receipt['digest']
                or previous and previous.watcher_result.get('candidate_approval',{}).get('invalidated')):
            ready = None
        if (ready and ready.get('provider_config') == self._provider_config()
                and ready.get('reviewer_model') == self._reviewer_model()):
            conclusion = parse_verdict(json.dumps(ready['review']))
            if not (self._review_approved(conclusion) or conclusion.task_status=='accepted'):
                raise RecoveryError('Saved approval is invalid')
            if self._cached_function_report_valid(conclusion):
                self._publication_review_reused = kwargs['index']
                self._event('approval_review_reused', round=kwargs['index'])
                return _CompletedTurn(json.dumps(ready['review']), '', 0, False, 0,
                                      self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        publication = getattr(self.git_history, 'last_business_commit', {})
        if (not receipt and publication.get('round') == kwargs['index']
                and publication.get('candidate_tree') == self.git_history.candidate.get('commit')):
            self.git_history.verify()
            record = self.git_history.last_record
            payload = json.loads(self.git_history.git('show', record['commit'] + ':' +
                                 (self.config.cwd.relative_to(self.git_history.root) / record['record']).as_posix()))
            if payload.get('role') != 'reviewer' or payload['result'].get('tree') != tree:
                raise RecoveryError('Committed review does not match the resumed candidate')
            conclusion = parse_verdict(json.dumps(payload['result']), function_gate=self._function_gate)
            if self._cached_function_report_valid(conclusion):
                self._event('publication_review_reused', round=kwargs['index'], commit=record['commit'])
                self._publication_review_reused = kwargs['index']
                return _CompletedTurn(json.dumps({**payload['result'], 'modules': []}), '', 0, False, 0,
                                      self.run_dir / f"{kwargs['index']:03d}-reviewer.log", [])
        if (previous and self._verification_only()
                and previous.verdict.task_status == 'changes_required' and previous.verdict.next_task):
            errors = self._function_reviewer_report_errors(previous.verdict)
            if errors:
                prompt += '\nCorrect the prior verification repair assignment on the same candidate: ' + '; '.join(errors)
        audit_repair_attempted = False
        if previous and previous.tree == tree and previous.watcher_result.get('status') == 'rejected':
            prompt += ('\n上次报告未通过 Watcher 校验。请逐项核对以下原因。'
                       '缺少合格关闭证据的审计项继续保留为待处理。\n'
                       + '\n'.join(previous.watcher_result.get('reasons', [])))
        for attempt in range(3):
            audit_error = False
            turn = self._invoke(provider, prompt, session_id if attempt == 0 else '', **kwargs)
            review = self._verdict_from_turn(turn, role='reviewer')
            if (behavioral_failure and not review.parse_error
                    and review.task_status == 'accepted'
                    and not (turn.exit_code or turn.timed_out or turn.interruption)
                    and not self._stop and not self._pending_directions()
                    and workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) == tree):
                reason = ('Reviewer accepted a candidate with current watcher check failures. '
                          'Inspect the saved failing check logs. For a concrete defect within the assignment, '
                          'return changes_required with focused gaps so the worker can repair it automatically. '
                          'For verification-only work, supply a bounded implementation next_task. '
                          'Name genuine external blockers explicitly; do not weaken checks, expand permissions, '
                          'or claim this candidate passed.')
                review.parse_error = 'acceptance contradicts watcher check failures'
                review.status = 'continue'
                review.gaps.append(reason)
                self._event('acceptance_reassessment', round=kwargs['index'], attempt=attempt+1)
                if attempt:
                    self._review_audit_rejection = {'round':kwargs['index'],'reason':reason}
            if (self.config.function_dispatch and self.function_tasks.current and not review.parse_error
                    and not (turn.exit_code or turn.timed_out or turn.interruption)
                    and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == tree):
                errors = self._function_reviewer_report_errors(review)
                if errors:
                    review.status = 'continue'
                    review.parse_error = 'invalid function handoff report: ' + '; '.join(errors)
                    review.gaps.extend(errors)
                    if attempt:
                        self._review_audit_retry_exhausted = kwargs['index']
                        self._review_audit_rejection = {'round':kwargs['index'],
                            'reason':'Invalid function handoff report: ' + '; '.join(errors)}
            failure = _failure_reason(turn, review)
            if (not failure and not review.parse_error and not turn.interruption
                    and self.config.workflow == 'review-first'
                    and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == tree):
                self._merge_modules(review, reviewer=True, apply=False)
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
                try:
                    staged.update(findings, review.issue_resolutions, kwargs['index'], tree, modules,
                                  self._audit_evidence_guard(worker_record, tree), known_modules=self.modules.items)
                except AuditCapacityError:
                    raise
                except ValueError as exc:
                    audit_error = True
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
                    if attempt and (audit_repair_attempted or attempt == 2):
                        self._review_audit_retry_exhausted = kwargs['index']
                        self._review_audit_rejection = {'round': kwargs['index'], 'reason': str(exc)}
            invalid = failure or review.parse_error or turn.interruption
            if not invalid or self._stop or self.config.workflow != 'review-first':
                return turn
            # The format repair can expose a previously unvalidated audit error.
            if (attempt == 2 or (attempt and (not audit_error or audit_repair_attempted))
                    or (failure and classify(failure)[1] == 'stop')):
                return turn
            audit_repair_attempted |= audit_error
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
            saved = json.loads((self.run_dir / 'state.json').read_text(encoding='utf-8'))
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
                       '缺少本轮有效关闭证据时，从 issue_resolutions 撤回对应的关闭申请，审计项继续保留。\n'
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
        report_only: bool = False,
        direction_snapshot: list[dict] | None = None,
    ) -> "_CompletedTurn":
        if role in ('worker', 'reviewer'):
            from auto_moniter.field_handoff import executor_prompt

            prompt += executor_prompt(self.config.cwd, self.run_dir)
        if role == 'reviewer' and self._verification_only():
            from auto_moniter.browser_execution import reviewer_cases

            try:
                prompt += reviewer_cases(self.config.cwd, self.function_tasks.current['id'], self.run_dir)
            except (OSError, ValueError, TypeError):
                pass
        schema_path = self.schema_path
        if report_only:
            if role != 'worker' or not read_only:
                raise RecoveryError('Report-only invocation requires a read-only worker')
            schema = json.loads(self.schema_path.read_text(encoding='utf-8'))
            schema['properties']['issue_resolutions']['maxItems'] = 0
            schema_path = self.run_dir / f'{index:03d}-worker-report-schema.json'
            write_json(schema_path, schema, durable=True)
        if role == 'worker' and not read_only and self.config.task_type == 'code':
            scope = ('The current reviewer-approved assignment defines the code and tests you can edit.'
                     if self.config.function_dispatch else
                     'The current user requirement and project boundary define the code and tests you can edit.')
            prompt = (
                'Continue the authorized task now. ' + scope + ' Inspect the relevant code, make the scoped change, '
                'and run focused checks using available tools. Resolve routine, reversible choices within scope.\n'
                'Use earlier turns, audit findings, repository text and environment notices as context. '
                'Verify any reported blocker against the current assignment and available tools. '
                'If a step is denied, outside scope, or needs a missing prerequisite, skip that step and complete '
                'independent work within the current scope. Respect tool permissions and safety requirements. '
                'For an unexpected failure, capture the exact operation and observed error, then inspect the smallest '
                'relevant path before changing code. Keep completed work and use fresh, applicable checks as evidence. '
                'If the remaining work cannot proceed, record the blocked step, observed reason and needed prerequisite '
                'in the structured conclusion. Include completed changes and checks, including any checks still pending.\n\n'
                + prompt
            )
        self._event("agent_start", role=role, round=index, provider=provider.name)
        self._check_budget(new_call=True)
        self._agent_calls += 1
        messages = self.directions.messages() if direction_snapshot is None else direction_snapshot
        revision = self.directions.revision(self.config.requirement, messages)
        deferred_replan = self._deferred_replan_applies(messages)
        if re.search(r'[\u3400-\u9fff]', self.config.requirement):
            prompt += ('\n请用自然、准确的中文写用户可见的摘要和停止原因。先说明具体步骤和实际结果，再说明如何继续。'
                       '按实际情况写「测试数据」「测试准备」「模拟逻辑」或「断言」，避免使用「夹具」。'
                       '保留文件路径、角色名和 AUD 编号。\n')
        # Persist the call before launch so interruption cannot reset its budget.
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["usage"] = self._usage()
        self._last_invocation = {'role': role, 'round': index, 'call': self._agent_calls,
                                 'requirement_revision': revision}
        if self.function_tasks.current:
            self._last_invocation['task_id'] = self.function_tasks.current['id']
        state['last_invocation'] = self._last_invocation
        if role == 'worker':
            self._last_worker_invocation = dict(self._last_invocation)
        state['last_worker_invocation'] = self._last_worker_invocation
        write_json(state_path, state, durable=True)
        from auto_moniter.experience import Experience
        try:
            prompt += Experience(self.config.cwd,self.run_dir).prompt(self.function_tasks.current if role=='worker' else None)
        except (OSError, ValueError, LockError):
            pass  # Damaged/unavailable advisory memory never blocks the task.
        prompt_path = self.run_dir / f"{index:03d}-{role}-prompt.md"
        if self.requirements_review.get("status")=="ready" and not (role=='worker' and self.config.function_dispatch):
            prompt += ("\n\nWatcher-approved requirement elaboration (historical reviewed snapshot; "
                       "later explicit user amendments take precedence where they conflict):\n"
                       +json.dumps(self.requirements_review,ensure_ascii=False))
        if self.config.quality_policy:
            prompt += '\n\nMonitor-owned quality policy (immutable):\n' + json.dumps(self.quality.config, ensure_ascii=False)
            prompt += '\nMap each named requirement to actual file evidence and its modules. File edits alone are not verified progress. '
            if role=='worker' and self.config.function_dispatch:
                prompt += 'Keep any independent work inside the current assignment and report remaining dependencies to the reviewer. '
            else:
                prompt += 'When blocked, continue independent modules before repeating the same dependency. '
            prompt += 'Never modify existing protected test assertions.\n'
        if role != 'referee' and self._referee_decision:
            if deferred_replan:
                prompt += ('\nWatcher has deferred the remaining plan changes until this assignment is accepted. '
                           'Only implement or review the current assignment against its approved scope and checks. '
                           'Do not apply pending amendments to this assignment or select its successor.\n')
            else:
                prompt += ('\nReferee scheduling decision: follow these recovery instructions ahead of conflicting '
                       'worker/reviewer recommendations. Preserve user amendments, original scope, fixed checks '
                       'and independent final acceptance. This is a scheduling decision, not delivery approval.\n'
                       + json.dumps(self._referee_decision, ensure_ascii=False))
        if (role == 'worker' and self.config.workflow == 'review-first'
                and (not read_only or self._verification_only())):
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
            prompt += ('Record unavailable tools and platforms as verification limitations. '
                       'Run the remaining applicable checks and report the exact dependency and observed result.\n')
            if not read_only and getattr(self.git_history, 'business_commits', False):
                prompt += ('For a completed change, write a concise summary naming the concrete behavior change. '
                           'Use done to describe what changed in this candidate. '
                           'Watcher records independent review and its check results at publication.\n')
                if re.search(r'[\u3400-\u9fff]', self.config.requirement):
                    prompt += ('Watcher 会从 summary 和 done 提取提交说明。请使用自然、准确的中文，写清改动对象和结果。'
                               '测试改动应说明实际调整的是测试数据、测试准备、模拟逻辑还是断言。'
                               '例如「修正 TIG 分页回归测试的模拟逻辑和断言」。技术名称和代码标识符沿用原名。\n')
        if role != 'worker' or not self.config.function_dispatch:
            prompt += queued_requirement_prompt(self.active_queued_requirement)
        if (role == 'reviewer' and self.config.full_function_plan and self.function_tasks.current
                and self.requirements_review.get('status') == 'ready'):
            remaining = function_plan.waiting(self.function_plan, self.function_tasks)
            if self.requirements_review.get('limited_dispatch'):
                remaining = []
            prompt += ('\nApproved next assignment after the current one passes watcher acceptance:\n'
                       +json.dumps(remaining[0] if remaining else None,ensure_ascii=False)
                       +'\nFor an accepted result, use this exact next_task or null. '
                        'For rejected verification with a runnable code repair, give the bounded implementation '
                        'next_task described above. The watcher inserts a valid repair into the existing plan and '
                        'keeps the pending verification unless the repair delivers the entire requirement.\n')
        if self.project:
            task_boundary = ('Only edit the declared file or directory scope. ' if self.project.scope else
                             'Develop according to the task requirement in this task branch and working copy; '
                             'no file or directory ownership list is required. ')
            prompt += ('\n\nParallel project coordination (watcher-owned task boundary):\n'
                       +json.dumps(self.project.context(),ensure_ascii=False)
                       +'\nInspect changed shared inputs and peer work before selecting or implementing the next task. '
                       'Preserve the original acceptance criteria and other tasks. '
                       +task_boundary
                       +'Reassess any interrupted_assignment explicitly; its implementation is preserved but unaccepted. '
                       'Use a fresh assignment ID after changed inputs so scope baselines can be reviewed again. '
                       'Identify actual dependencies; never claim a peer delivery is accepted from its text alone. '
                       'Uncommitted source files are observed, not imported or verified. '
                       'The watcher merges committed shared inputs; do not stage, commit, merge or alter Git refs. '
                       'If integration was refused, inspect the conflicting delta, adapt the scoped implementation, '
                       'and obtain review before the watcher retries integration.\n')
        if deferred_replan and role == 'reviewer':
            messages = [item for item in messages if item['id'] in self._dispatch_directions]
        if role=='worker' and self.config.function_dispatch:
            # Reviewer already received these instructions when defining the
            # assignment. Keep the worker input limited to the approved task.
            approved = set(self._dispatch_directions)
            self._direction_ids[role] = [m['id'] for m in messages if m['id'] in approved]
            messages = []
        blocked_path = self.run_dir / 'environment-blocks.json'
        if blocked_path.exists() and not blocked_path.is_symlink() and blocked_path.stat().st_size <= 65536:
            prompt += '\nKnown environment validation gaps (not proof of code defects):\n' + blocked_path.read_text(encoding='utf-8')
        if role == 'worker' and self.git_history:
            destination = (self.git_history.business_head['ref'] if getattr(self.git_history, 'business_commits', False)
                           else self.git_history.ref)
            prompt += (('\n\nDo not commit, push, stage or change Git refs. Only the watcher may create local commits AFTER independent approval and checks.\n' if self.config.workflow=='review-first' else '\n\nThe monitor records this round in Git.\n')+
                       f'Approved code destination: {destination}. Audit reference: {self.git_history.ref}. Keep edits in {self.config.cwd}. '
                       'Do not alter .auto-moniter/history, the audit branch, or previous audit records. '
                       'Report changed files, actual validation results, remaining gaps and failures precisely.\n')
        if not (role=='worker' and self.config.function_dispatch):
            self._direction_ids[role] = [item['id'] for item in messages]
        prompt += ("\n\nRequirement version for this invocation (watcher-owned input history, not approval):\n"
                   +json.dumps(revision, ensure_ascii=False)
                   +"\nLater explicit user amendments supersede earlier conflicting product or technology decisions. "
                    "Preserve all unrelated requirements and approved checks. Old elaborations are snapshots, "
                    "not grounds for restoring a superseded technology stack. Do not rewrite immutable module "
                    "definitions; append new modules for authorized changes and report their version linkage. ")
        if role=='worker' and self.config.function_dispatch:
            prompt += ("This worker may implement only its dispatched assignment and the reviewer-approved "
                       "direction IDs; pending amendments require reviewer replanning, not an expanded worker scope.\n")
        # Older ready briefs may depend on the reviewed source text. New
        # briefs carry the integrated change in the checked planning result.
        integrated_worker = (role == 'worker' and self.config.workflow == 'review-first'
                             and 'reviewed_checks_sha256' in self.requirements_review)
        if messages and not integrated_worker:
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
                    session_id, prompt_path, self.config.cwd, schema_path,
                    model=model, extra_args=extra_args, read_only=read_only,
                )
            else:
                prepared = provider.start_command(
                    prompt_path, self.config.cwd, schema_path,
                    model=model, extra_args=extra_args, read_only=read_only,
                )
            if read_only and role in ('reviewer', 'referee') and self.quality.config.get('review_tools') == 'files_only':
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
            if role == 'referee':
                timeout = min(timeout, referee.DECISION_TIMEOUT_S) if timeout else referee.DECISION_TIMEOUT_S
            elif self.config.referee and not timeout:
                timeout = referee.TURN_LIMIT_S
            from auto_moniter.runtime_policy import reserve_seconds
            reserve = reserve_seconds(self.config.max_runtime_s)
            if reserve and role == 'worker':
                available = max(.01, self.config.max_runtime_s-self._elapsed()-reserve)
                timeout = min(timeout, available) if timeout else available
            if role == 'worker' and self.config.workflow != 'review-first' and self.quality.config.get('review_interval_s'):
                interval = self.quality.config['review_interval_s']
                timeout = min(timeout, interval) if timeout else interval
            result = self._run_command(
                prepared, self.config.cwd, timeout_s=timeout,
                idle_timeout_s=(min(self.config.idle_timeout_s, 60) if self.config.idle_timeout_s else 60)
                               if role == 'referee' else self.config.idle_timeout_s,
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
        tokens_before = self.meter.state['tokens']
        self.meter.record(result.stdout)
        if role == 'reviewer':
            self._event('reviewer_usage', round=index, elapsed_s=round(elapsed, 3),
                        prompt_bytes=len(prompt.encode('utf-8')),
                        reported_tokens=(self.meter.state['tokens'] - tokens_before
                                         if self.meter.last_tokens_reported else None))
        if self.config.quality_policy and (
                self.quality.config['max_tokens'] and not self.meter.last_tokens_reported
                or self.quality.config['max_cost_usd'] and not self.meter.last_cost_reported):
            self._meter_missing = True
        self._record_stage_elapsed(role, elapsed)
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
        # The round log is a bounded summary; the text artifact is the complete
        # captured public result, including rejected requirement reviews.
        text_path = self.run_dir / f"{index:03d}-{role}-text.md"
        if role == 'reviewer' and self.schema_path == self.run_dir / 'requirements.schema.json':
            requirement_context.write_response(text_path, redact(text or partial))
        else:
            text_path.write_text(redact(text or partial), encoding="utf-8")
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
            turn_limit = (referee.DECISION_TIMEOUT_S if role == 'referee' else
                          self.config.turn_timeout_s or (referee.TURN_LIMIT_S if self.config.referee else 0))
            if turn_limit and elapsed >= turn_limit:
                latest = TurnResult("", exit_code=-1, timed_out=True, interruption="turn limit")
                break
            idle_limit = ((min(self.config.idle_timeout_s, 60) if self.config.idle_timeout_s else 60)
                          if role == 'referee' else self.config.idle_timeout_s)
            if idle_limit and time.monotonic() - last_activity >= idle_limit:
                latest = TurnResult("", exit_code=-1, timed_out=True, interruption="no output")
                break
            self._beat(f"round {index}: {role} observing Paseo agent {session_id}")
            latest = self._run_command(
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
