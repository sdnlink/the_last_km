"""Run acceptance checks and bind their receipts to candidates."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
import time

from auto_moniter.providers import PreparedCommand
from auto_moniter.workspace import LockError, evidence_paths, snapshot_fingerprint, workspace_fingerprint
from auto_moniter.verdict import Verdict, parse_verdict
from auto_moniter.runlog import clipped
from auto_moniter.storage import write_json
from auto_moniter.redaction import redact, safe
from auto_moniter import candidate_checks, function_tasks
from auto_moniter.live import LiveOutput
from auto_moniter.git_history import GitHistoryError
from auto_moniter.business_recovery import BusinessHeadChanged
from auto_moniter.quality import test_asset
from auto_moniter.verification import outcome as check_outcome
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter import requirements_review, requirement_context
from auto_moniter import check_environment
from auto_moniter import deployment
from auto_moniter.supervisor_models import (
    BudgetExceeded,
    RecoveryError,
    _accepted,
    _audit_findings,
    _failure_reason,
)


class AcceptanceChecks:
    """Run acceptance checks and bind their receipts to candidates."""

    def _uses_candidate_checks(self, record):
        return (self.config.workflow == 'review-first' and not self.config.no_review
                and not record.failure and not record.interruption and not record.verdict.parse_error)

    def _review_approved(self, verdict):
        if self.config.workflow != 'review-first':
            return _accepted(verdict)
        return (verdict.status == 'done' and verdict.scope in {'module', 'requirement'}
                and not (verdict.gaps or verdict.next_actions or verdict.blockers))

    def _function_check_blocker(self, worker, reviewer, tree):
        """Bind a jointly reported blocker to the current assignment and actual failed checks."""
        task = self.function_tasks.current
        review = reviewer.verdict
        if (not self._function_gate or not task or self.config.task_type != 'code'
                or not self._uses_candidate_checks(worker) or not worker.verdict.blockers
                or worker.verdict.task_id != task['id'] or review.task_status != 'changes_required'
                or worker.tree != tree or self._pending_directions()
                or reviewer.watcher_result.get('status') != 'applied'):
            return None
        # A regular repair finding is insufficient. Both roles must identify
        # the same existing issue, and it must be the only review finding.
        findings = _audit_findings(review)
        ids = set(reviewer.watcher_result.get('reported', []))
        if len(ids) != 1 or not findings or not review.next_actions:
            return None
        issue = next(iter(ids))
        marker = re.escape(issue) + r'(?=$|[:：\s])'
        if (not all(re.match(marker, finding) for finding in findings)
                or not any(re.search(r'(?<![\w-])' + marker, blocker) for blocker in worker.verdict.blockers)):
            return None
        receipt = worker.watcher_result.get('candidate_checks')
        if not receipt:
            return None
        context = self._candidate_check_context(worker)
        if (not candidate_checks.valid(receipt, self.run_dir, context) or receipt['passed']
                or not receipt['complete'] or receipt['source_changed'] or receipt.get('scope_gaps')
                or len(receipt['checks']) != len(context['commands'])
                or [item['command'] for item in receipt['checks']] != context['commands']
                or any(item['status'] not in {'passed', 'check_failed'} for item in receipt['checks'])):
            return None
        root = self.config.cwd
        protected = self.quality.protected(root)
        # A traceback cites tests for product bugs too. Require the reviewer
        # to identify the test itself as the primary repair location.
        cited = set(evidence_paths(root, findings, ignore_dir=self.run_dir))
        repair = set(evidence_paths(root, review.next_actions[:1], ignore_dir=self.run_dir))
        if not repair or not repair <= cited:
            return None
        paths = {str(path.relative_to(root)) for path in repair
                 if test_asset(str(path.relative_to(root))) or str(path) in protected}
        if len(paths) != len(repair):
            return None
        failures = candidate_checks.failed_assertion_locations(receipt, self.run_dir, root, paths)
        if not failures:
            return None
        paths = sorted({path for _, locations in failures for path, _ in locations})
        writable = self._writable_function_tests(task)
        scopes = {path in writable for path in paths}
        if len(scopes) != 1:
            return None
        basis = {'context': {k: v for k, v in context.items() if k != 'round'},
                 'issue': issue, 'failures': failures, 'writable': True in scopes}
        return {'key': candidate_checks.digest(basis), 'task_id': task['id'], 'tree': tree,
                'issue': issue, 'paths': paths, 'writable': basis['writable'],
                'logs': [item['log'] for item in receipt['checks'] if item['status'] == 'check_failed']}

    def _check_review_blockers(self, worker, reviewer, tree):
        review = reviewer.verdict
        evidence = self._function_check_blocker(worker, reviewer, tree)
        blockers = review.blockers
        key = ''
        consecutive = True
        if evidence:
            key = 'assignment-check:' + evidence['key']
            previous = next((item for item in reversed(self._records)
                             if item.role == 'reviewer' and item.index < reviewer.index), None)
            consecutive = bool(previous and previous.index == reviewer.index - 1
                               and previous.watcher_result.get('check_blocker', {}).get('key') == evidence['key'])
            reviewer.watcher_result['check_blocker'] = evidence
            blockers = _audit_findings(review)
        if not self.quality.review_blockers(blockers, key=key, consecutive=consecutive):
            return ''
        action = 'Resolve the named external dependency, then resume; no automatic model retries.'
        summary = 'Repeated independently confirmed external dependency; work saved for dependency resolution.'
        if evidence:
            label = f"{evidence['task_id']} / {evidence['issue']}: " + ', '.join(evidence['paths'])
            if evidence['writable']:
                action = 'Repair the focused test within this assignment, preserving its behavior assertions, then resume.'
                summary = 'Repeated failure in an editable assignment test; repair remains outstanding. ' + label
            else:
                action = 'Review the proposed fixture repair with the check owner, then resume with the fixed checks.'
                summary = 'Repeated independently confirmed check failure outside the assignment; candidate preserved. ' + label
            self._event('assignment_check_blocked', round=reviewer.index, **evidence)
        write_json(self.run_dir / 'dependency-block.json', safe({'blockers': blockers,
                   'review_count': self.quality.state['blocker_count'], 'round': reviewer.index,
                   'action': action, **(evidence or {})}), durable=True)
        return summary

    def _assignment_whitespace_preflight(self):
        """Identify a fixed Git check the current assignment cannot repair."""
        task = self.function_tasks.current
        git_check = 'git diff --check'
        if (not self._function_gate or not task or self.config.task_type != 'code'
                or git_check not in self.config.checks or not isinstance(self.git_history, ApprovedHistory)
                or self.git_history.root != self.config.cwd):
            return ''
        if task['delivery_scope'] == 'function':
            catalog = self._function_check_catalog(task)
            if not any(catalog.get(key) == git_check for key in task['check_ids']):
                return ''
        excluded = [':(exclude,literal)' + name for name in function_tasks.writable_files(task)]
        command = ['git', '-c', 'core.quotePath=false', 'diff', '--no-ext-diff', '--check', '--', '.', *excluded]
        self._beat('checking fixed Git acceptance outside the assignment')
        completed = False
        try:
            result = self._run_command(PreparedCommand(command), self.config.cwd,
                                 timeout_s=min(10, self.config.check_timeout_s),
                                 should_stop=self._should_stop, detect_input_requests=False,
                                 on_child=self._remember_child, max_output_bytes=self.config.max_output_bytes)
            completed = True
        finally:
            self._forget_child(completed=completed)
        if result.exit_code != 2 or result.timed_out or result.interruption or not result.stdout.strip():
            return ''
        log = self.run_dir / 'assignment-preflight.log'
        log.write_text('Out-of-assignment Git whitespace diagnostic\n' + clipped(redact(result.stdout)),
                       encoding='utf-8')
        reason = ('Fixed Git check blocked outside assignment ' + task['id']
                  + '. Correct the reported paths or approve a repair assignment before resuming. '
                  + 'See ' + log.name + '.')
        self._event('assignment_preflight_blocked', task_id=task['id'], command=git_check,
                    log=log.name, reason=reason)
        return reason

    def _candidate_check_context(self, record):
        task = self.function_tasks.current if self.config.function_dispatch else None
        final = task['delivery_scope'] == 'requirement' if task else record.verdict.scope == 'requirement'
        ids = [task['module_id']] if task else record.verdict.module_ids
        supplemental = record.watcher_result.get('supplemental_checks', [])
        commands = (self._assignment_check_commands(task) if self._function_gate and task
                    else self._acceptance_commands(final, ids))
        publication = getattr(self.git_history, 'candidate', {})
        return {'round':record.index, 'tree':record.tree, 'task':task, 'final':final,
                **({'publication_tree': publication['business_tree']} if publication.get('round') == record.index
                   and 'publication_paths' in publication else {}),
                'requirement_cycle':len(self.requirement_cycles)+1,
                'business_update_count':self._business_update_count,
                **({'deployment_evidence': deployment.fingerprint(self.config.cwd, self.run_dir, self.config.requirement)}
                   if self.config.task_type == 'deployment' else {}),
                'module_ids':sorted(ids),
                'commands':list(dict.fromkeys([*commands, *(item['command'] for item in supplemental)])),
                **({'supplemental_checks': supplemental} if supplemental else {}),
                'configured_checks':list(self.config.checks),
                **({'check_source_aliases': self.config.check_source_aliases} if self.config.check_source_aliases else {}),
                **({'external_deliveries': {
                    item['task']['id']: item['external_delivery'] for item in self.function_tasks.accepted
                    if 'external_delivery' in item}}
                   if any('external_delivery' in item for item in self.function_tasks.accepted) else {}),
                'check_timeout_s':self.config.check_timeout_s,
                'provider_config':self._provider_config(), 'reviewer_model':self._reviewer_model(),
                'worker_model':self.config.model, 'requirement':self.config.requirement,
                'acceptance':self.config.acceptance, 'quality_policy':self.config.quality_policy,
                'direction_ids':[message['id'] for message in self.directions.messages()]}

    def _approved_supplemental_test(self, path, name, module_id):
        from auto_moniter.workspace import _file_token
        protected = self.quality.protected(self.config.cwd)
        if str(path) in protected and _file_token(path) == protected[str(path)]:
            return True
        for record in reversed(self._records):
            receipt = record.watcher_result.get('candidate_checks') or {}
            context = receipt.get('context') or {}
            task = context.get('task') or {}
            authored = task.get('review_test') or {}
            if (record.role == 'worker' and task.get('module_id') == module_id
                    and authored.get('path') == name
                    and path.stat().st_size <= 128 * 1024
                    and path.read_bytes() == authored['source'].encode('utf-8')
                    and candidate_checks.valid(receipt, self.run_dir, context)):
                return True
        return False

    def _queue_supplemental_checks(self, record, review):
        from auto_moniter.local_tests import command
        task = self.function_tasks.current
        context = self._candidate_check_context(record)
        receipt = record.watcher_result.get('candidate_checks')
        if (self._pending_directions() or self._business_head_changed()
                or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree
                or not candidate_checks.valid(receipt, self.run_dir, context)
                or not receipt['passed']):
            raise ValueError('补跑前需要与当前候选一致的有效检查记录，且现有检查须先通过。')
        related = {item['id'] for item in self.audit.items if task['module_id'] in item['module_ids']}
        related.update(item['id'] for item in self.audit.pending({task['module_id']}))
        additions = []
        commands = set(context['commands'])
        for item in review.supplemental_checks:
            name = item['path']
            if item['criterion_id'] not in task['criterion_ids'] or item['audit_id'] not in related:
                raise ValueError('补跑请求必须关联当前派单的验收条目和本模块的审核记录。')
            try:
                executable = command(self.config.cwd, name)
                path = self.config.cwd / name
                if not executable or not path.is_file() or not self._approved_supplemental_test(path, name, task['module_id']):
                    raise ValueError('Test lacks approval')
            except (OSError, ValueError) as exc:
                raise ValueError('这个测试尚未获准由 Watcher 补跑：' + name) from exc
            if executable not in commands:
                additions.append({**item, 'command': executable})
                commands.add(executable)
        saved = record.watcher_result.get('supplemental_checks', [])
        if not additions or len(saved) + len(additions) > 3:
            raise ValueError('补跑没有新增检查，或已达到本轮的补跑上限。请核对现有结果。')
        record.watcher_result['supplemental_checks'] = [*saved, *additions]
        self._event('supplemental_checks_queued', round=record.index, task_id=task['id'],
                    paths=[item['path'] for item in additions])

    def _repair_preflight(self, record, reviewer, last_review):
        """Reject an incomplete repair before repeating costly delivery checks.

        Readiness is never approval: it still requires checks and a fresh review.
        Keep function dispatch on its existing check/review contract.
        """
        if (not self._uses_candidate_checks(record) or self.config.function_dispatch or reviewer.name == 'paseo'
                or self.config.task_type != 'code' or not self.audit.pending(set(record.verdict.module_ids))
                or record.watcher_result.get('candidate_checks')):
            return None
        submitted = {item.index:item for item in self._records if item.role == 'worker'}
        rejected = set()
        for previous in reversed(self._records):
            if previous.role != 'reviewer':
                continue
            if previous.delivery.get('status') == 'accepted':
                break
            # Rejected reviews may intentionally accept no module_ids. Scope
            # those findings to their actual worker handoff, never all modules.
            prior_worker = submitted.get(previous.index)
            ids = previous.verdict.module_ids or (prior_worker.verdict.module_ids if prior_worker else [])
            if (not previous.failure and not previous.interruption and not previous.verdict.parse_error
                    and previous.verdict.status == 'continue' and previous.verdict.gaps
                    and set(ids) & set(record.verdict.module_ids)):
                rejected.add(previous.index)  # Report retries are one candidate.
        if len(rejected) < 2:
            return None
        context_digest = candidate_checks.digest(self._candidate_check_context(record))
        saved = record.watcher_result.get('repair_preflight', {})
        if saved.get('status') == 'ready' and saved.get('context_digest') == context_digest:
            return None  # Only skip another preliminary screen, never the final gate.
        self._process_control()
        if self._stop:
            return None  # The normal check boundary handles the saved stop.
        prompt = self._review_prompt(record.index, record.verdict, Path(record.output_path), record.tree, last_review)
        prompt += (
            '\n\nREPAIR PREFLIGHT ONLY: previous candidates repeatedly failed code review. '
            'Check all named repairs, entry points, callers and failure paths together BEFORE expensive checks. '
            'List every currently known concrete blocker for the submitted modules in one response; '
            'do not expand the original requirements or block these modules on unrelated future work. '
            'Use status=continue with gaps for incomplete code, and preserve existing AUD IDs. '
            'Use status=done only to indicate code is ready for watcher checks, never delivery approval. '
            'No fixed checks have run for this candidate yet: their absence is not a preflight gap. '
            'Return issue_resolutions=[]; audit closures require the subsequent check-backed review. '
            'Do not run tests, edit files, publish, or request identical worker resubmission if ready. '
            'Watcher must run the mandatory checks and obtain a fresh independent review before acceptance.')
        self._event('repair_preflight_started', round=record.index, role='reviewer')
        turn = self._invoke(reviewer, prompt, '', role='reviewer', index=record.index,
            model=self._reviewer_model(), extra_args=self.config.reviewer_extra_args, read_only=True)
        # Preserve the preliminary transcript when the formal review uses the
        # normal reviewer filenames later in this same round.
        for suffix in ('.log', '-text.md', '-prompt.md'):
            source = self.run_dir/f'{record.index:03d}-reviewer{suffix}'
            if source.is_file() and not source.is_symlink():
                source.replace(self.run_dir/f'{record.index:03d}-reviewer-preflight{suffix}')
        turn.output_path = self.run_dir/f'{record.index:03d}-reviewer-preflight.log'
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree:
            if self._business_head_changed():
                raise BusinessHeadChanged('Business HEAD changed during repair preflight')
            raise RecoveryError('Workspace changed during read-only repair preflight; no approval applied')
        value = self._verdict_from_turn(turn, role='reviewer')
        failure = _failure_reason(turn, value) or turn.interruption
        actionable = (not failure and not value.parse_error and value.status in {'continue','blocked'}
                      and bool(value.gaps or value.blockers) and not value.issue_resolutions)
        record.watcher_result['repair_preflight'] = {
            'status':'changes_required' if actionable else 'ready' if _accepted(value) and not failure else 'invalid',
            'tree':record.tree, 'context_digest':context_digest, 'output_file':turn.output_path.name,
            'summary':value.summary, 'rejected_candidates':len(rejected)}
        self._event('repair_preflight', round=record.index, **record.watcher_result['repair_preflight'])
        self._write_state(self._records, 'running', 0, '', record.tree)
        if actionable:
            # Existing review processing records these findings without granting
            # delivery. The next worker sees one complete repair handoff.
            return turn
        if failure:
            return turn  # Authentication, stop and transport retain normal handling.
        return None

    def _reuse_module_checks(self, record, context):
        """Carry an accepted module's exact checks into an unchanged handoff."""
        if (self.config.task_type != 'code' or self.config.function_dispatch
                or self._pending_directions()
                or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree):
            return False
        # A final report may cover previously accepted modules awaiting
        # revalidation. Joint coverage is still checked after independent review.
        if context['final'] and (self.modules.coverage(record.verdict)
                or any(item['status'] != 'accepted' and not item['round']
                       for item in self.modules.items.values())):
            return False
        previous = next((item for item in reversed(self._records) if item.role == 'worker'
                         and item.index < record.index and item.watcher_result.get('candidate_checks')), None)
        if previous is None:
            return False
        receipt = previous.watcher_result['candidate_checks']
        old = receipt.get('context')
        # Reporting scope changes; all execution inputs, commands and source
        # must match. Never reuse an approval, partial result or old failed log.
        if (not candidate_checks.valid(receipt, self.run_dir, old) or not receipt['passed']
                or not receipt['complete'] or receipt['source_changed'] or old.get('task') is not None
                or old.get('final') is not False):
            return False
        execution = lambda value: {k:v for k,v in value.items() if k not in {'round','final','module_ids'}}
        if execution(old) != execution(context):
            return False
        approved = next((item for item in reversed(self._records)
                         if item.role == 'reviewer' and item.index == previous.index
                         and item.delivery.get('status') == 'accepted'), None)
        if (approved is None or approved.failure or approved.interruption
                or approved.delivery.get('status') != 'accepted'
                or not self._candidate_approval(approved, previous)):
            return False
        # Keep a current-round copy so retention of older rounds cannot remove
        # the final review's evidence. These are historical executions, not reruns.
        import copy
        results = copy.deepcopy(receipt['checks'])
        copies = []
        for index, result in enumerate(results, 1):
            source = self.run_dir/result['log']
            try:
                content = source.read_bytes()
            except OSError:
                # Historical evidence can become unavailable after validation.
                # No current-round logs have been written yet, so checks can rerun.
                return False
            target = self.run_dir/f'{record.index:03d}-check-{index:02d}.log'
            if (target.exists() or target.is_symlink()
                    or hashlib.sha256(content).hexdigest() != receipt['logs'].get(result['log'])):
                return False
            result['log'] = target.name
            copies.append((target, content))
        for target, content in copies:
            with target.open('xb') as stream:
                stream.write(content)
        self._write_acceptance_snapshot(record.index, record.tree, context['final'], results, [], False, False)
        reused = candidate_checks.create(self.run_dir, context, {'checks':results}, True)
        reused.update(scope_gaps=[], target_hashes={}, failures=[],
                      reused_from={'round':previous.index,'checks_digest':receipt['digest']})
        reused['digest'] = candidate_checks.digest({k:v for k,v in reused.items() if k != 'digest'})
        record.watcher_result['candidate_checks'] = reused
        self._passed_function_checks = dict(reused['passed_commands'])
        self._delivery_tree = record.tree
        self._event('module_checks_reused_for_final' if context['final'] else 'module_checks_reused',
                    round=record.index, from_round=previous.index, tree=record.tree)
        return True

    def _prepare_candidate_checks(self, record):
        """Execute checks before review; a successful receipt survives a restart."""
        import copy
        context = copy.deepcopy(self._candidate_check_context(record))
        previous = record.watcher_result.get('candidate_checks')
        if candidate_checks.valid(previous, self.run_dir, context) and previous['passed'] and previous['complete']:
            self._passed_function_checks = dict(previous['passed_commands'])
            self._delivery_tree = record.tree
            self._event('candidate_checks_reused', round=record.index, tree=record.tree)
            return
        if not previous and self._reuse_module_checks(record, context):
            return
        prior_results = []
        same_checked_candidate = None
        if previous:
            old = previous.get('context', {})
            if (old == context and not previous.get('source_changed')
                    and candidate_checks.valid(previous, self.run_dir, context)):
                same_checked_candidate = previous
                for command, result in zip(context['commands'], previous['checks']):
                    if result.get('status') != 'passed' or result.get('command') != command:
                        break
                    prior_results.append(result)
            compare = lambda value: {key: item for key, item in value.items()
                                     if key not in {'commands', 'supplemental_checks'}}
            if (context.get('supplemental_checks') and previous.get('passed')
                    and candidate_checks.valid(previous, self.run_dir, old)
                    and compare(old) == compare(context)
                    and len(old['commands']) < len(context['commands'])
                    and old['commands'] == context['commands'][:len(old['commands'])]):
                prior_results = previous['checks']
            archived = candidate_checks.archive(self.run_dir,previous)
            if archived:
                record.watcher_result['candidate_checks_history'] = [
                    *record.watcher_result.get('candidate_checks_history',[])[-19:], archived]
        verdict = copy.deepcopy(record.verdict)
        self._passed_function_checks = {}
        snapshot = {'checks':[]}
        passed = False
        scope_gaps = []
        local_keys = self._local_check_keys(context)
        reusable = self._reusable_local_checks(context, local_keys)
        # Early precondition failures may return without executing a command.
        # Never attach the preceding attempt's snapshot to this new receipt.
        self._write_acceptance_snapshot(record.index, record.tree, context['final'], [], [], True, False)
        try:
            # A structural scope defect belongs to the reviewer/worker repair
            # path. Do not run an assignment's tests on an unauthorized edit.
            scope_gaps = self._function_scope_problems() if self._function_gate else []
            if scope_gaps:
                verdict.gaps.extend(scope_gaps)
            else:
                passed = self._delivery_checks(record.index, verdict, final=context['final'],
                    expected_tree=record.tree, module_ids=set(context['module_ids']),
                    function_commands=context['commands'], pre_review=True, reusable=reusable,
                    prior_results=prior_results)
        finally:
            # Budget and stop exceptions still keep the checks captured so far.
            path = self.run_dir/'acceptance-results.json'
            if path.is_file() and not path.is_symlink():
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('round') == record.index and saved.get('tree') == record.tree:
                    snapshot = saved
            receipt = candidate_checks.create(self.run_dir, context, snapshot, passed)
            receipt['scope_gaps'] = list(scope_gaps)
            receipt['target_hashes'] = dict(getattr(self,'_function_target_hashes',None) or {})
            receipt['failures'] = list(verdict.gaps[len(record.verdict.gaps):])
            receipt['digest'] = candidate_checks.digest({key:value for key,value in receipt.items() if key!='digest'})
            record.watcher_result['candidate_checks'] = receipt
            if same_checked_candidate and self._verification_only():
                from auto_moniter.field_handoff import rebind_after_check_retry
                rebind_after_check_retry(self.run_dir, same_checked_candidate, receipt, context)
            if (local_keys and passed and candidate_checks.valid(receipt, self.run_dir, context)
                    and local_keys == self._local_check_keys(context)):
                self._local_check_receipt = (receipt, local_keys)
            if self.config.function_dispatch and self.function_tasks.current:
                from auto_moniter.experience import Experience
                try:
                    if (candidate_checks.valid(receipt,self.run_dir,context)
                            and (receipt['passed'] or candidate_checks.observed_assertion_failure(receipt,self.run_dir))):
                        Experience(self.config.cwd,self.run_dir).observe(self.function_tasks.current,receipt)
                except (OSError, ValueError, LockError) as exc:
                    self.log('Project experience update deferred: ' + redact(str(exc)))
            self._write_state(self._records, 'running', 0, '', record.tree)
        self._event('candidate_checks_completed', round=record.index, tree=record.tree, passed=passed)

    def _local_check_keys(self, context):
        task = context['task']
        if (not self._function_gate or self.config.task_type != 'code' or context['final']
                or not task or task.get('kind') not in (None, 'verification')):
            return {}
        from auto_moniter.local_tests import reuse_key
        return {command: key for command in context['commands']
                if command not in self.config.checks and (key := reuse_key(self.config.cwd, command)) is not None}

    def _reusable_local_checks(self, context, keys):
        if not keys or self._local_check_receipt is None:
            return {}
        receipt, previous_keys = self._local_check_receipt
        previous = receipt['context']
        scope_fields = {'round', 'task', 'commands', 'module_ids'}
        execution = {key: value for key, value in previous.items() if key not in scope_fields}
        if (execution != {key: value for key, value in context.items() if key not in scope_fields}
                or not candidate_checks.valid(receipt, self.run_dir, previous)):
            return {}
        return {command: (result, receipt['logs'][result['log']], keys[command])
                for command, result in zip(previous['commands'], receipt['checks'])
                if command in keys and keys[command] == previous_keys.get(command)}

    def _reuse_local_check(self, index, number, command, saved, tree, results):
        if saved is None:
            return False
        from auto_moniter.local_tests import reuse_key

        result, digest, key = saved
        if reuse_key(self.config.cwd, command) != key:
            return False
        original = self.run_dir / result['log']
        try:
            if original.is_symlink():
                return False
            content = original.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(content).hexdigest() != digest:
            return False
        target = self.run_dir / f"{index:03d}-check-{number:02d}.log"
        if target.is_symlink():
            return False
        target.write_bytes(content)
        results.append({**result, 'log': target.name, 'reused_from': result.get('reused_from', original.name)})
        self._passed_function_checks[command] = target.name
        self._event('local_check_reused', round=index, tree=tree, log=target.name, source=original.name)
        return True

    def _repeated_reviewer_test_failure(self, record):
        task = self.function_tasks.current
        if not task or not task.get('review_test') or self._pending_directions():
            return None
        from auto_moniter.local_tests import command

        receipt = record.watcher_result['candidate_checks']
        context = self._candidate_check_context(record)
        check_command = command(self.config.cwd, task['review_test']['path'])
        if (not check_command or not candidate_checks.valid(receipt, self.run_dir, context)
                or not receipt['complete'] or receipt['source_changed']):
            return None
        logged_command = redact(check_command)
        signature = candidate_checks.assertion_signature(receipt, self.run_dir, logged_command)
        if signature is None:
            return None
        previous = next((item for item in reversed(self._records)
                         if item.role == 'worker' and item.index < record.index), None)
        if not previous or previous.verdict.task_id != task['id'] or previous.tree != record.tree:
            return None
        review = next((item for item in reversed(self._records)
                       if item.role == 'reviewer' and item.index == previous.index), None)
        if (not review or review.failure or review.interruption or review.verdict.parse_error
                or review.verdict.task_status != 'changes_required'):
            return None
        old = previous.watcher_result.get('candidate_checks')
        if not candidate_checks.valid(old, self.run_dir, {**context, 'round': previous.index}):
            return None
        if (not old['complete'] or old['source_changed']
                or candidate_checks.assertion_signature(old, self.run_dir, logged_command) != signature
                or [item['status'] for item in old['checks']] != [item['status'] for item in receipt['checks']]):
            return None
        failure = next(item for item in receipt['checks'] if item['command'] == logged_command)
        return {'task_id': task['id'], 'test': task['review_test']['path'], 'previous_round': previous.index,
                'log': failure['log'], 'tree': record.tree}

    def _review_check_results(self, record):
        """Load the check stage's result once, before handing the candidate to the reviewer."""
        receipt = record.watcher_result.get('candidate_checks')
        context = self._candidate_check_context(record)
        if not candidate_checks.valid(receipt, self.run_dir, context):
            return ['Pre-review check receipt does not match the submitted candidate'], []
        if not receipt['passed'] or not receipt['complete'] or receipt['source_changed']:
            return receipt.get('failures') or ['Pre-review checks did not pass'], receipt.get('scope_gaps', [])
        self._function_target_hashes = dict(receipt.get('target_hashes', {}))
        return [], []

    def _save_candidate_approval(self, worker, reviewer):
        receipt = worker.watcher_result.get('candidate_checks')
        if receipt and self._uses_candidate_checks(worker):
            reviewer.watcher_result['candidate_approval'] = {
                'checks_digest':receipt['digest'], 'context':receipt['context'], 'review':reviewer.verdict.to_dict()}

    def _candidate_approval(self, record, worker):
        approval = self._verified_candidate_approval(record, worker, self._candidate_check_context(worker))
        if (not approval or workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) != worker.tree
                or self._pending_directions()):
            return None
        return approval

    def _verified_candidate_approval(self, record, worker, context):
        approval = record.watcher_result.get('candidate_approval')
        receipt = worker.watcher_result.get('candidate_checks')
        if (not isinstance(approval,dict) or approval.get('invalidated') or not isinstance(approval.get('review'),dict)
                or not candidate_checks.valid(receipt,self.run_dir,context)
                or approval.get('context') != receipt['context'] or approval.get('checks_digest') != receipt['digest']):
            return None
        value = parse_verdict(json.dumps(approval['review']),function_gate=self._function_gate)
        return value if (not value.parse_error and (self._review_approved(value) or value.task_status=='accepted')
                         and self._cached_function_report_valid(value)) else None

    def _cached_function_report_valid(self, review):
        return not self.config.function_dispatch or not self._function_reviewer_report_errors(review)

    def _acceptance_commands(self, final, module_ids, function_commands=None, *, task=None):
        commands = list(self.config.checks) if final or self.config.workflow == 'review-first' else []
        ids = set(module_ids)
        if final:
            ids.update(key for key,item in self.modules.items.items() if item['status']=='accepted')
        commands.extend(self.modules.checks_for(ids))
        if self._function_gate:
            commands.extend(self._local_test_commands(self._local_test_paths))
            task = task or self.function_tasks.current
            if task:
                commands.extend(self._local_test_commands(task.get('test_files', [])))
        return list(dict.fromkeys(commands if function_commands is None else function_commands))

    def _assignment_check_commands(self, task):
        final = task['delivery_scope'] == 'requirement'
        modules = [task['module_id']]
        if task['delivery_scope'] != 'function' or task.get('kind') == 'verification':
            fixed = self._acceptance_commands(final, modules, task=task)
        else:
            fixed = []
        if task['delivery_scope'] != 'function':
            return fixed
        catalog = self._function_check_catalog(task)
        return list(dict.fromkeys([*(catalog[key] for key in task['check_ids']),
                                    *self._local_test_commands(task['test_files']), *fixed]))

    def _missing_dispatch_inputs(self, task):
        bindings = self.config.quality_policy.get('check_inputs', {})
        if not bindings:
            return []
        try:
            commands = self._assignment_check_commands(task)
        except KeyError:
            # The existing dispatch validator reports unknown check IDs.
            return []
        missing = []
        root = self.config.cwd
        for key, paths in bindings.items():
            index = int(key.split(':')[1]) - 1
            if self.config.checks[index] not in commands:
                continue
            for name in paths:
                try:
                    path = (root / name).resolve()
                    available = path.is_relative_to(root) and path.exists()
                except (OSError, RuntimeError):
                    available = False
                if not available:
                    missing.append(f'{key} {name}')
        return missing

    def _delivery_checks(self, index: int, verdict: Verdict, *, final: bool = True, expected_tree: str = "",
                         module_ids: set[str] | None = None, function_commands: list[str] | None = None,
                         pre_review=False, reusable=None, prior_results=()) -> bool:
        """Execute user-selected checks independently of the agents' claims."""
        if final and self.config.task_type == 'deployment':
            try:
                deployment.validate(self.config.cwd, self.run_dir, self.config.requirement)
            except (OSError, ValueError) as exc:
                verdict.status = 'continue'
                verdict.gaps.append('Deployment acceptance blocked: ' + str(exc))
                verdict.next_actions.append('Complete the deployment evidence report from actual remote results; preserve completed stages.')
                return False
        if self._pending_directions():
            verdict.status = 'continue'
            verdict.gaps.append('New human instructions arrived after the worker started. Implement and review them before accepting delivery.')
            return False
        before = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        if (pre_review and self.config.task_type == 'documentation'
                and before == snapshot_fingerprint(self.config.cwd, self._baseline_files)):
            verdict.gaps.append('No document or report was created or changed for this delivery.')
            return False
        original_review = verdict.to_dict()
        baseline_errors = self.quality.baseline_errors(self.config.cwd) if self.config.quality_policy else []
        if baseline_errors:
            verdict.status = 'continue'
            verdict.gaps.append('Protected acceptance baseline changed: ' + ', '.join(baseline_errors))
            return False
        if final and not pre_review and self.quality.config.get('requirements'):
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
        commands = self._acceptance_commands(final, verdict.module_ids if module_ids is None else module_ids, function_commands)
        failures = []
        check_results = list(prior_results)
        self._passed_function_checks.update({command: result['log']
                                             for command, result in zip(commands, prior_results)})
        budget_stop = None
        for number, command in enumerate(dict.fromkeys(commands), 1):
            if number <= len(prior_results):
                continue
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
            if reusable and self._reuse_local_check(index, number, command, reusable.get(command), before, check_results):
                self._progress_reset |= self.quality.check_result(command, True)
                self._write_acceptance_snapshot(index, before, final, check_results, failures, True, False)
                continue
            self._beat(f"round {index}: acceptance check {number}")
            completed = False
            live_check = LiveOutput(self.run_dir, 'check', index, '验收检查')
            live_check.emit({'kind': 'command', 'text': redact(command), 'status': 'running'})
            result = None
            source_alias = None
            check_started = time.monotonic()
            try:
                argv = ["sh", "-c", command]
                if command in getattr(self, '_check_aliases', {}):
                    check_id, label = self._check_aliases[command]
                    source_alias = check_environment.source_root(self.run_dir, self.config.cwd, check_id, label)
                    argv = ["env", f"FRONTEND_ROOT={source_alias}", *argv]
                result = self._run_command(
                    PreparedCommand(argv), self.config.cwd,
                    timeout_s=self.config.check_timeout_s,
                    detect_input_requests=False,
                    should_stop=self._should_stop, on_child=self._remember_child,
                    max_output_bytes=self.config.max_output_bytes,
                    memory_bytes=self.config.memory_bytes, file_bytes=self.config.file_bytes,
                    heartbeat=self._heartbeat, heartbeat_note=f"round {index}: acceptance check {number}",
                    on_output=live_check.feed,
                )
                check_elapsed = time.monotonic() - check_started
                completed = True
            finally:
                live_check.close()
                self._forget_child(completed=completed)
                if result is not None:
                    path = self._record_check_result(index, number, command, result, before, final, check_results, failures, verdict,
                                                     source_alias=source_alias)
                    self._record_stage_elapsed('checks', check_elapsed)
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
        if (not pre_review and failures and check_results and (self.config.function_dispatch or
                check_results[-1]['status'] == 'environment_unavailable'
                and not any(c['status'] not in ('passed', 'environment_unavailable') for c in check_results))
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

    def _record_check_result(self, index, number, command, result, before, final, check_results, failures, verdict, *, source_alias=None):
        path = self.run_dir / f"{index:03d}-check-{number:02d}.log"
        environment = f"FRONTEND_ROOT={source_alias}\ncwd={self.config.cwd}\n" if source_alias else ''
        path.write_text(
            f"$ {redact(command)}\nexit={result.exit_code} timed_out={result.timed_out} interruption={result.interruption or '-'}\n"
            + environment + clipped(redact(result.stdout[-16000:]), 16000) + "\n" + clipped(redact(result.stderr[-8000:]), 8000), encoding="utf-8",
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
        complete = bool(check_results) and not failures and not incomplete and not source_changed and all(
            item.get('status') == 'passed' for item in check_results)
        write_json(self.run_dir/'acceptance-results.json', {
            'round':index,'tree':tree,'scope':'requirement' if final else 'module',
            'checks':check_results,
            'complete':complete,
            'source_changed':source_changed,
            'note':'This is a check snapshot, not final delivery approval. Unexecuted behavior remains unverified.'}, durable=True)
        if not incomplete and check_results:
            self._event('acceptance_checks_concluded', round=index, status='passed' if complete else 'failed',
                        total=len(check_results), passed=sum(item.get('status') == 'passed' for item in check_results),
                        source_changed=source_changed)

    def _scope_replan_candidate(self, records):
        """Route a verified out-of-scope submission to planning."""
        task = self.function_tasks.current
        if (not self._function_gate or not task or task.get('kind') in ('integration', 'verification')
                or self._pending_directions()
                or self.project and (self.project.needs_review or self._project_replan)):
            return None
        latest = next((r for r in reversed(records) if r.role == 'worker' and r.verdict.task_id == task['id']), None)
        if not latest or latest.verdict.task_status != 'done' or latest.failure or latest.interruption or latest.exit_code or latest.timed_out:
            return None
        receipt = latest.watcher_result.get('candidate_checks', {})
        gaps = receipt.get('scope_gaps', [])
        if not gaps or any(not gap.startswith(('Restore changes outside this assignment: ',
                                               'Restore edits outside the assigned function: ')) for gap in gaps):
            return None
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != latest.tree:
            return None
        return latest if candidate_checks.valid(receipt, self.run_dir, self._candidate_check_context(latest)) else None

    def _pending_review(self, records):
        """Resume an exact submitted candidate after review transport/format failure."""
        if self.config.workflow != 'review-first' or self.config.no_review or self._pending_directions():
            return None
        if isinstance(self.git_history, ApprovedHistory) and not self.git_history.candidate:
            return None
        worker = next((r for r in reversed(records) if r.role == 'worker'), None)
        if not worker:
            return None
        route = worker.watcher_result.get('referee_route', {})
        if route.get('action') == 'retry_worker' and route.get('tree') == worker.tree:
            return None
        task = self.function_tasks.current
        prior_task = worker.watcher_result.get('candidate_checks', {}).get('context', {}).get('task') or {}
        if (self._verification_only() and task['delivery_scope'] == 'function'
                and prior_task.get('id') == task['id'] and prior_task.get('delivery_scope') != 'function'):
            return None  # Recheck an assignment that was separated from module delivery.
        if (self.config.function_dispatch and task and task['delivery_scope'] == 'requirement'
                and len(self.modules.items) > 1 and worker.verdict.task_id == task['id']
                and worker.verdict.task_status == 'done' and worker.verdict.module_ids == [task['module_id']]
                and not (worker.failure or worker.interruption or worker.exit_code or worker.timed_out)
                and not worker.watcher_result.get('worker_report')
                and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == worker.tree):
            original = worker.verdict.to_dict()
            errors = self._validate_function_worker(worker.verdict)
            worker.watcher_result['worker_report'] = {
                'status':'pending', 'task':task, 'original':original, 'errors':errors}
            self._event('final_module_report_migration', round=worker.index, task_id=task['id'])
            return worker
        recovered = self._recover_function_submission(worker)
        if recovered:
            return worker
        report = worker.watcher_result.get('worker_report', {})
        if (report.get('status') in ('pending', 'attempted', 'rejected')
                and report.get('task') == self.function_tasks.current
                and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == worker.tree):
            return worker
        module_report = worker.watcher_result.get('module_report', {})
        if (module_report.get('status') in ('attempted', 'rejected')
                and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == worker.tree):
            # Resuming an exhausted report correction is not authorization to
            # repeat implementation. Preserve the candidate until input changes.
            return worker
        if worker.failure or worker.interruption:
            return None
        if (route.get('action') == 'retry_reviewer' and route.get('tree') == worker.tree
                and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == worker.tree
                and (not self.config.function_dispatch or self.function_tasks.current
                     and self.function_tasks.current['id'] == worker.verdict.task_id)):
            return worker
        rescope = worker.watcher_result.get('scope_replan', {})
        if (rescope.get('pending') and rescope.get('task') == self.function_tasks.current
                and worker.tree == workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
                and not self._function_scope_problems()):
            rescope['pending'] = False
            return worker  # Checks and independent review must run under the new manifest.
        review = next((r for r in reversed(records) if r.role == 'reviewer' and r.index == worker.index), None)
        if (not self.config.function_dispatch and worker.verdict.scope == 'module'
                and review and review.delivery.get('status') == 'accepted'):
            # An accepted module's cached approval is not an unfinished review.
            # Move to the next worker/final handoff instead of replaying and
            # rejecting that same module for having no newly accepted modules.
            return None
        if review and review.watcher_result.get('function_gate') == 'repair' and not self._check_retry_review(review):
            # Older monitors mislabeled failed checks as implementation repair.
            # Keep the submitted worker candidate, but obtain a fresh review:
            # no saved provider/direction context exists for reusing approval.
            reasons = review.watcher_result.get('reasons', [])
            if not reasons or any(not reason.startswith('Acceptance check failed: ') for reason in reasons):
                return None
        invalid_repair = bool(review and self._verification_only()
                              and review.verdict.task_status == 'changes_required' and review.verdict.next_task
                              and self._function_reviewer_report_errors(review.verdict))
        rejected_audit = bool(review and (invalid_repair or review.watcher_result.get('status') in {'rejected','blocked'}
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
        delivered_ids = set(worker.verdict.module_ids)
        if review:
            delivered_ids.intersection_update(review.verdict.module_ids)
        if published and delivered_ids and all(self.modules.items.get(k, {}).get('status') == 'accepted' for k in delivered_ids):
            return None
        if review and not (review.failure or review.interruption or review.verdict.parse_error or rejected_audit or published or ready):
            return None
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != worker.tree:
            return None
        return worker

    def _stage_saved_candidate_review(self, records):
        """Reopen only a checked, unchanged candidate while retaining the pending replan."""
        task = self.function_tasks.current
        if (not self._function_gate or not self._referee_replan or not task
                or task['delivery_scope'] != 'function' or self._business_head_changed()
                or self._assignment_repository_conflict(task)
                or self.project and (self.project.needs_review or self._project_replan)):
            raise ValueError('当前派单不符合原候选补审条件。')
        worker = next((record for record in reversed(records) if record.role == 'worker'), None)
        receipt = worker.watcher_result.get('candidate_checks', {}) if worker else {}
        old = receipt.get('context')
        if (not worker or worker.index != self.config.review_saved_round
                or worker.verdict.task_id != task['id'] or worker.exit_code or worker.timed_out
                or not self._uses_candidate_checks(worker)
                or worker.tree != workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
                or not receipt.get('passed') or not candidate_checks.valid(receipt, self.run_dir, old)):
            raise ValueError('候选或原检查回执已变化，请核对轮次、工作树和原始日志。')
        context = self._candidate_check_context(worker)
        directions = self.directions.messages()
        if ({**context, 'direction_ids': old.get('direction_ids')} != old
                or old.get('direction_ids') != self._dispatch_directions
                or context['direction_ids'] != [item['id'] for item in directions]
                or isinstance(self.git_history, ApprovedHistory)
                and self.git_history.candidate.get('round') != worker.index):
            raise ValueError('当前派单、验收配置或保存的发布候选与原回执不一致。')
        brief = self.requirements_review
        if brief.get('status') != 'ready':
            saved = self.run_dir / 'reviewed-requirement.json'
            brief = requirement_context.read(saved) if saved.is_file() and not saved.is_symlink() else {}
        if (brief.get('status') != 'ready' or brief.get('cycle') != len(self.requirement_cycles) + 1
                or brief.get('original_sha256') != requirements_review.requirement_hash(self.config.requirement)):
            brief = self._recover_approved_function_brief(task)
        if not brief:
            raise ValueError('缺少与当前派单对应的已批准需求。')
        requirements_review.parse(json.dumps({key: brief.get(key) for key in requirements_review.SCHEMA['required']}))
        self.requirements_review = brief
        self._deferred_replan = self._replan_binding(task, directions)
        self._event('saved_candidate_review_requested', round=worker.index, task_id=task['id'], tree=worker.tree)
        return worker

    def _recover_function_submission(self, record):
        """Recover a legacy report downgrade once; the reviewer still decides acceptance."""
        task = self.function_tasks.current
        if (not self.config.function_dispatch or not task or record.interruption
                or record.exit_code or record.timed_out
                or record.watcher_result.get('submission_recovered')
                or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree):
            return False
        pending_report = record.watcher_result.get('worker_report', {})
        if record.verdict.task_status != 'incomplete' and not pending_report:
            return False
        if not pending_report and not any(
                gap == 'Report only the dispatched function with its exact task ID and verified completion evidence.'
                or gap.startswith('Function completion report rejected:') for gap in record.verdict.gaps):
            return False
        source = pending_report.get('original')
        if not isinstance(source, dict):
            path = self.run_dir / f'{record.index:03d}-worker-text.md'
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024*1024:
                return False
            raw = path.read_text(encoding='utf-8')
        else:
            raw = json.dumps(source, ensure_ascii=False)
        verdict = parse_verdict(raw, function_gate=self._function_gate, worker_submission=True)
        if verdict.task_status != 'done' or self._validate_function_worker(verdict):
            return False
        record.watcher_result['submission_recovered'] = {
            'previous': record.verdict.to_dict(), 'tree': record.tree,
            'reason': 'Report annotations require independent assessment, not another worker submission.'}
        if pending_report:
            pending_report['status'] = 'review_pending'
        record.failure = ''
        record.verdict = verdict
        self._event('function_submission_recovered', round=record.index, task_id=task['id'])
        return True

    def _check_retry_review(self, record):
        if record.watcher_result.get('candidate_approval'):
            worker = next((item for item in reversed(self._records)
                           if item.role=='worker' and item.index==record.index),None)
            return self._candidate_approval(record,worker) if worker else None
        retry = record.watcher_result.get('check_retry')
        if not isinstance(retry, dict):
            return self._scope_retry_review(record)
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
        if (review.parse_error or not (self._review_approved(review) or review.task_status == 'accepted')
                or not self._cached_function_report_valid(review)):
            return None
        return review

    def _scope_retry_review(self, record):
        """Reuse an unchanged approval after correcting a function locator failure."""
        reasons = record.watcher_result.get('reasons', [])
        if (record.watcher_result.get('function_gate') != 'repair' or not reasons
                or any(not reason.startswith('Implement or change the assigned function: ') for reason in reasons)
                or self._pending_directions() or not self.function_tasks.current
                or record.verdict.task_id != self.function_tasks.current['id']
                or record.tree != workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
                or self._function_scope_problems()):
            return None
        path = self.run_dir/f'{record.index:03d}-reviewer-text.md'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024*1024:
            return None
        review = parse_verdict(path.read_text(encoding='utf-8'), function_gate=self._function_gate)
        if (review.parse_error or review.task_status != 'accepted' or review.task_id != record.verdict.task_id
                or review.gaps or review.blockers or review.next_actions):
            return None
        if not self._cached_function_report_valid(review):
            return None
        self._event('function_scope_review_reused', round=record.index, task_id=review.task_id)
        return review

    def _saved_approval(self, index, tree):
        path = self.run_dir / 'approval-ready.json'
        if (not getattr(self.git_history, 'business_commits', False) or not path.is_file()
                or path.is_symlink() or path.stat().st_size > 512*1024):
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if (value.get('round') != index or value.get('tree') != tree
                or value.get('candidate_tree') != self.git_history.candidate.get('commit')
                or value.get('business_head') != self.git_history.business_head):
            return None
        return value

    def _previous_handoff_approval(self, reviewer, worker, tree):
        """Recover a saved approval rejected solely by the former file-citation rule."""
        result = reviewer.watcher_result
        receipt = worker.watcher_result.get('candidate_checks')
        reasons = result.get('reasons', [])
        if (result.get('status') != 'rejected' or not reasons or any(
                not reason.startswith('worker must cite every changed assignment file; missing: ')
                for reason in reasons)
                or reviewer.failure or reviewer.interruption or reviewer.exit_code or reviewer.timed_out
                or reviewer.tree != worker.tree or tree != worker.tree or self._pending_directions()
                or not candidate_checks.valid(receipt, self.run_dir, self._candidate_check_context(worker))
                or not receipt['passed']):
            return None
        from dataclasses import replace
        review = replace(reviewer.verdict, status=reviewer.reported_status or reviewer.verdict.status)
        if review.task_status == 'accepted' and self._cached_function_report_valid(review):
            self._direction_ids['reviewer'] = list(receipt['context']['direction_ids'])
            return review
        return None
