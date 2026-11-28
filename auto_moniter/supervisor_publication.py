"""Business branch synchronization and approved publication."""

from __future__ import annotations

import json
from auto_moniter.workspace import file_snapshot, snapshot_fingerprint, workspace_fingerprint
from auto_moniter.storage import write_json
from auto_moniter import candidate_checks, deployment, function_gate
from auto_moniter.git_history import GitHistoryError
from auto_moniter.publication import StagedChangesConflict
from auto_moniter.business_recovery import BusinessHeadChanged
from auto_moniter.project_workspace import ProjectInputsChanged
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter.supervisor_models import BudgetExceeded, RecoveryError, RunReport
from auto_moniter.directions import Directions
from auto_moniter import recovery_wait


class Publication:
    """Business branch synchronization and approved publication."""

    def _prepare_lifecycle_recovery(self, status, summary):
        if self._stop_requested:
            return
        condition = ''
        if status == 'recovery_failed' and (self.run_dir / 'delivery-cleanup.json').is_file():
            condition = 'delivery_cleanup'
        elif status == 'recovery_failed' and 'Referee recovery cannot close previous agents:' in summary:
            condition = 'referee_cleanup'
        elif status == 'action_required' and getattr(self, '_delivery_readiness_missing', False):
            condition = 'delivery_readiness'
        elif status == 'action_required' and summary.startswith('派单 '):
            from auto_moniter.dispatch_inputs import WAIT_FILE
            if self.directions.read(WAIT_FILE).get('summary') == summary:
                condition = 'dispatch_inputs'
        elif (status == 'action_required' and self._stop_reason.startswith('Acceptance environment unavailable;')
              and ((self.run_dir / 'delivery-cleanup.json').is_file() or not self.config.no_review)):
            snapshot = self.directions.read('acceptance-results.json', 4 * 1024 * 1024)
            outcomes = [item['status'] for item in snapshot.get('checks', [])]
            if outcomes and outcomes[-1] == 'environment_unavailable' and set(outcomes) <= {'passed', 'environment_unavailable'}:
                condition = 'check_environment'
        elif status == 'action_required' and (
                self.requirements_review.get('failure_kind') == 'model_transport'
                or getattr(self, '_model_transport_wait', False)
                or self._stop_reason.startswith('裁判模型服务暂不可用')
                or summary.startswith('Continuous model connection failures exceeded')):
            condition = 'model_transport'
        if condition:
            recovery_wait.schedule(self.run_dir, condition, 'resume', detail=summary, terminal_status=status)

    def _schedule_lifecycle_recovery(self, status, summary):
        recovery_wait.seal(self.run_dir, status, summary)

    def _adopt_lifecycle_recovery(self):
        recovery_wait.adopted(self.run_dir)

    def _delivery_cleanup_binding(self, tree):
        return {
            'tree': tree,
            'requirement': self.directions.revision(self.config.requirement)['sha256'],
            'checks': self.config.checks,
            'check_source_aliases': self.config.check_source_aliases or [],
            'check_timeout_s': self.config.check_timeout_s,
            'quality_policy': self.config.quality_policy,
            'queue': self.requirement_queue.snapshot(self._requirement_queue_state()),
            'git': getattr(self.git_history, 'business_head', {}),
        }

    def _save_delivery_cleanup(self, summary, records):
        from auto_moniter.redaction import redact

        receipt = self._delivery_receipt(records)
        write_json(self.run_dir / 'delivery-cleanup.json', {
            'summary': redact(summary), 'binding': self._delivery_cleanup_binding(self._delivery_tree),
            'receipt': receipt, 'artifacts': recovery_wait.delivery_artifacts(self.run_dir),
            'recovery_inputs': recovery_wait.inputs(self.run_dir),
        }, durable=True)
        self._write_state(records, 'running', 0, '', self._delivery_tree)
        if not self._valid_delivery_receipt(receipt, self._delivery_tree):
            raise RecoveryError('最终候选的正式验收回执发生变化，需要恢复后复验。')

    def _delivery_receipt(self, records):
        receipt = getattr(self, '_final_check_receipt', None)
        if receipt and receipt['context']['tree'] == self._delivery_tree:
            return receipt
        for record in reversed(records):
            receipt = record.watcher_result.get('candidate_checks')
            if record.role == 'worker' and record.tree == self._delivery_tree and receipt:
                if receipt.get('context', {}).get('final'):
                    return receipt
        return None

    def _valid_delivery_receipt(self, receipt, tree):
        context = receipt.get('context') if isinstance(receipt, dict) else None
        if not (isinstance(context, dict) and context.get('final') and context.get('tree') == tree
                and candidate_checks.valid(receipt, self.run_dir, context) and receipt['passed']
                and candidate_checks.covers_configured_checks(context, self.config.checks)):
            return False
        if self.config.task_type != 'deployment':
            return True
        expected = context.get('deployment_evidence')
        return bool(expected and 'error' not in expected and expected == deployment.fingerprint(
            self.config.cwd, self.run_dir, self.config.requirement))

    def _revalidate_saved_delivery(self, marker, records, tree):
        import copy

        receipt = marker.get('receipt')
        saved = self._delivery_receipt(records)
        reference = next((proof for proof in (saved, receipt)
                          if candidate_checks.intact(proof) and isinstance(proof.get('context'), dict)), {})
        context = reference.get('context') or {}
        if self.config.task_type == 'deployment' and context.get('deployment_evidence') != deployment.fingerprint(
                self.config.cwd, self.run_dir, self.config.requirement):
            return ['部署证据已变化，需要独立审查确认新的部署结果。']
        conclusion = next((r for r in reversed(records) if r.tree == tree and r.verdict.scope == 'requirement'
                           and r.verdict.status == 'done' and not (r.failure or r.interruption)), None)
        if not conclusion:
            return ['缺少绑定最终候选的审查结论。']
        reuse_context = (context.get('final') and context.get('tree') == tree
                         and context.get('round') == conclusion.index
                         and candidate_checks.covers_configured_checks(context, self.config.checks))
        if reuse_context:
            commands = context['commands']
            retained = candidate_checks.retained_results(reference, self.run_dir, context)
        else:
            if context.get('publication_checks'):
                return ['发布树检查的候选绑定需要重新核对。']
            commands = self._acceptance_commands(True, conclusion.verdict.module_ids)
            retained = {}
        verdict = copy.deepcopy(conclusion.verdict)
        old_gaps = len(verdict.gaps)
        index = conclusion.index
        attempt = max((int(path.stem.rsplit('-', 1)[1])
                       for path in self.run_dir.glob(f'{index:03d}-check-*-attempt-*.log')
                       if path.stem.rsplit('-', 1)[1].isdigit()), default=0) + 1
        if self._delivery_checks(index, verdict, expected_tree=tree, function_commands=commands,
                                 retained=retained, log_attempt=attempt, receipt_context=context if reuse_context else None):
            return []
        return verdict.gaps[old_gaps:] or ['最终候选的验收复验未完成。']

    def _resume_delivery_cleanup(self, records):
        marker = Directions(self.run_dir).read('delivery-cleanup.json', 32 * 1024 * 1024)
        if not marker or self._stop_requested:
            return None
        self._process_control()
        if self._stop_requested:
            return self._finish('stopped', self._stop_reason, records, None)
        tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        binding = self._delivery_cleanup_binding(tree)
        if 'receipt' not in marker:
            binding = {key: binding[key] for key in ('tree', 'requirement', 'checks', 'queue', 'git')}
        if marker.get('binding') != binding:
            (self.run_dir / 'delivery-cleanup.json').unlink(missing_ok=True)
            return None
        self._delivery_tree = tree
        if (not self._valid_delivery_receipt(marker.get('receipt'), tree)
                or marker.get('artifacts') != recovery_wait.delivery_artifacts(self.run_dir)):
            self._event('delivery_evidence_recheck', tree=tree)
            context = (marker.get('receipt') or {}).get('context') or {}
            if task := context.get('task'):
                missing = self._missing_dispatch_inputs(task, candidate_only=True)
                if missing:
                    self._delivery_readiness_missing = True
                    self._write_dispatch_input_wait(task, [value for _, value in missing], delivery=True)
                    labels = '、'.join(value['path'] for _, value in missing)
                    return self._finish('action_required', '最终候选的环境准备回执尚未就绪。' + labels, records, None)
            problems = self._revalidate_saved_delivery(marker, records, tree)
            if problems:
                return self._finish('stopped' if self._stop_requested else 'action_required',
                                    '最终候选需要通过正式复验。' + '；'.join(problems), records, None)
        else:
            self._final_check_receipt = marker['receipt']
        self._event('delivery_cleanup_resumed', tree=tree)
        self._closing_saved_delivery = True
        try:
            return self._finish('done', marker['summary'], records, None)
        finally:
            self._closing_saved_delivery = False

    def _verify_delivery_cleanup(self):
        if self._business_head_changed():
            raise BusinessHeadChanged('Business HEAD changed while closing the accepted delivery')
        marker = self.directions.read('delivery-cleanup.json', 32 * 1024 * 1024)
        if (not self._valid_delivery_receipt(marker.get('receipt'), self._delivery_tree)
                or marker.get('artifacts') != recovery_wait.delivery_artifacts(self.run_dir)):
            raise RecoveryError('收尾期间正式验收证据发生变化，需要恢复后复验。')

    def _final_delivery_proof(self):
        marker = self.directions.read('delivery-cleanup.json', 32 * 1024 * 1024)
        receipt = marker.get('receipt') or {}
        return {key: receipt[key] for key in ('digest', 'checks', 'logs') if key in receipt}

    def _business_head_changed(self):
        return (isinstance(self.git_history, ApprovedHistory)
                and self.git_history.business_commits
                and self.git_history._business_identity() != self.git_history.business_head)

    def _sync_business_head(self, records):
        if self.project:
            self.project.sync()
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
        if self._function_gate and baseline != self._baseline_files:
            scope = function_gate.reconcile_protected_baseline(
                self.run_dir / 'function-baseline.json.gz', self.config.cwd, self.function_tasks.current,
                self._baseline_files, baseline)
            if scope is not None:
                self._function_baseline = scope
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
        self._final_check_receipt = None
        self.worker_session_id = self.reviewer_session_id = self._active_reviewer_id = ''
        self._drop_worker_session = True
        self._impact_snapshot = current
        self._rounds_since_review = self.config.review_every or 1
        self._worker_seconds_since_review = 0.0
        self._business_update_count = history.business_update_count
        self._business_input = {'before': before, 'after': dict(history.business_head), 'count': self._business_update_count}
        if self.project:
            self._project_replan = True
        self._recovery_count += 1
        self._no_progress = 0
        self._recovery_note = (
            'Business HEAD changed from ' + (before.get('commit') or '(unborn)') + ' to '
            + history.business_head['commit'] + '. Continue the original requirement on the current source. '
            'Preserve existing work and open AUD IDs. Prior completion, review and check results need revalidation. '
            'Inspect the external commit delta, complete remaining modules, rerun checks and obtain a fresh read-only review. '
            'Committed acceptance assets are the new input; do not weaken uncommitted test assertions.')
        (self.run_dir / 'approval-ready.json').unlink(missing_ok=True)
        for name in ('acceptance-results.json', 'requirement-coverage.json'):
            path = self.run_dir / name
            if path.is_file() and not path.is_symlink():
                value = json.loads(path.read_text(encoding='utf-8'))
                value.update(invalidated=True, invalidated_reason='Business HEAD changed; revalidation required', complete=False)
                write_json(path, value, durable=True)
        self._event('business_head_adopted', **self._business_input, modules=invalidated, reopened_issues=reopened)
        self.log('Business HEAD changed; automatically continuing on ' + history.business_head['commit'][:12]
                 + ' with fresh implementation, review and acceptance checks')
        self._write_state(records, 'running', 0, '', snapshot_fingerprint(self.config.cwd, current))
        return True

    def _wait_project_dependencies(self):
        if not self.project:
            return
        self.project.observe(force=True)
        while self.project.waiting() and not self._stop:
            self._process_control()
            if self._stop:
                return
            cancelled = [peer['id'] for peer in self.project.current.get('peers', [])
                         if peer['id'] in self.project.waiting() and peer.get('status') == 'cancelled']
            if cancelled:
                self._stop = True
                self._stop_reason = 'Project dependencies cancelled; update the task dependencies before resuming: ' + ', '.join(cancelled)
                self._event('project_dependencies_blocked', dependencies=cancelled)
                return
            self._beat('waiting for project dependencies: '+', '.join(self.project.waiting()))
            self._check_budget()
            self._stop_event.wait(.5)
            self.project.observe()

    def _check_project_delivery(self, records):
        if not self.project:
            return
        self.project.observe(force=True)
        current = workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir)
        if self.project.needs_review or current != self._delivery_tree:
            if current != self._delivery_tree:
                self.project.note_changes(['Workspace changed at final delivery'])
            self._write_state(records,'running',0,'',current)
            self._event('project_review_stale',reason='Inputs changed at final delivery; reopening review')
            raise ProjectInputsChanged('Final project inputs need fresh review')

    def _commit_approved(self,worker,reviewer,worker_text,review_text,tree):
        if not self._publication_check_inputs(reviewer):
            return False
        problem = self._external_delivery_problem()
        if problem:
            reviewer.verdict.gaps.append(problem)
            return False
        if self.config.workflow != 'review-first':
            return True
        if self._verification_only() and self.function_tasks.current['delivery_scope'] == 'function':
            with self.directions.lock():
                if not (self._publication_boundary(reviewer) and not self._business_head_changed()
                        and not self._pending_directions()
                        and workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) == tree
                        and self._verified_candidate_approval(reviewer, worker, self._candidate_check_context(worker))):
                    reviewer.verdict.gaps.append('Verification evidence changed before it could be recorded.')
                    return False
                self._event('verification_evidence_accepted', task_id=self.function_tasks.current['id'],
                            round=worker.index, tree=tree)
                return True
        if not self.git_history:
            if self.config.task_type == 'deployment' and not self._verified_candidate_approval(
                    reviewer, worker, self._candidate_check_context(worker)):
                reviewer.verdict.status = 'continue'
                reviewer.verdict.gaps.append('Deployment evidence or its check receipt is stale. Review the current evidence.')
                return False
            return True
        with self.directions.lock():
            return self._commit_approved_locked(worker,reviewer,worker_text,review_text,tree)

    def _publication_check_inputs(self, reviewer):
        problem = self._candidate_input_wait()
        if not problem:
            return True
        reviewer.verdict.gaps.append(problem)
        self._stop_reason = problem
        return False

    def _publication_boundary(self, reviewer) -> bool:
        """Refuse a new local publication once stop or the cumulative budget is known.

        An already atomic publication is left intact. This gate runs at the commit
        entry and again immediately before irreversible ref updates.
        """
        self._process_control(active=True)
        problem = self._external_delivery_problem()
        if problem:
            reviewer.verdict.gaps.append(problem)
            return False
        if self.project:
            self.project.observe(force=True)
            if self.project.needs_review:
                reviewer.verdict.status = 'continue'
                reviewer.verdict.gaps.append('Parallel project inputs changed; review their impact and refresh the worker assignment before publication')
                self._project_replan = True
                return False
            outside = self.project.pending_scope_errors()
            if outside or self.project.sync_errors:
                reviewer.verdict.status = 'continue'
                reviewer.verdict.gaps.append('Parallel publication requires scope and integration repair: '
                    + ', '.join([*outside,*self.project.sync_errors]))
                return False
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
        self._beat(f'round {worker.index}: publishing approved candidate')
        if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir)!=tree:
            if self.project:
                self.project.note_changes(['Workspace changed after review'])
                self._project_replan = True
            reviewer.verdict.status='continue'
            reviewer.verdict.gaps.append('Workspace changed after review; review current inputs before committing')
            return False
        if self._pending_directions():
            reviewer.verdict.status='continue'
            reviewer.verdict.gaps.append('New instructions arrived before commit; implement and review them first.')
            return False
        if self._uses_candidate_checks(worker) and not self._verified_candidate_approval(
                reviewer, worker, self._candidate_check_context(worker)):
            reviewer.verdict.status = 'continue'
            reviewer.verdict.gaps.append('Reviewer approval does not match the saved candidate and check receipt. The saved acceptance is stale.')
            return False
        if not self._publication_boundary(reviewer):
            return False
        if self.git_history.business_commits:
            if self._function_gate and (recovered := self._record_published_function(worker, reviewer)) is not None:
                return recovered
            from auto_moniter.publication import PublicationRefused, publish
            write_json(self.run_dir / 'approval-ready.json', {'round': worker.index, 'tree': tree,
                'candidate_tree': self.git_history.candidate['commit'], 'review': reviewer.verdict.to_dict(),
                'business_head': self.git_history.business_head, 'provider_config': self._provider_config(),
                'reviewer_model': self._reviewer_model(),
                'checks_digest':worker.watcher_result.get('candidate_checks',{}).get('digest')}, durable=True)
            def payload(record):
                data = RunReport('', 0, '', '', [record]).to_dict()['records'][0]
                data.update(requirement=self.config.requirement, acceptance=self.config.acceptance,
                            directions=self.directions.messages())
                if record.role == 'reviewer':
                    data['audit_issues'] = self.audit.export()
                return data
            receipt = worker.watcher_result.get('candidate_checks') if self._uses_candidate_checks(worker) else None
            try:
                worker.git_checkpoint, reviewer.git_checkpoint = publish(
                    self.git_history, worker.index, payload(worker), payload(reviewer), worker_text, review_text,
                    boundary=lambda: (self._publication_boundary(reviewer)
                                      and self._publication_check_inputs(reviewer)), receipt=receipt)
            except PublicationRefused:
                return False
            except BusinessHeadChanged:
                raise
            except StagedChangesConflict:
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

    def _record_published_function(self, worker, reviewer):
        history = self.git_history
        publication = history.last_business_commit
        if (publication.get('round') != worker.index
                or publication.get('candidate_tree') != history.candidate.get('commit')):
            return None
        history.verify()
        checkpoint = history.last_record
        if (checkpoint.get('round') != worker.index or checkpoint.get('commit') != history.head
                or checkpoint.get('business_commit') != publication
                or history.last_worker.get('business_commit') != publication):
            raise RecoveryError('已发布派单的 Git 记录不一致，无法恢复验收登记。')
        path = (self.config.cwd.relative_to(history.root) / checkpoint['record']).as_posix()
        payload = json.loads(history.git('show', history.head + ':' + path))
        saved = payload.get('result', {})
        approval = saved.get('watcher_result', {}).get('candidate_approval', {})
        receipt = worker.watcher_result.get('candidate_checks', {})
        if (payload.get('role') != 'reviewer' or saved.get('tree') != worker.tree
                or saved.get('task_id') != self.function_tasks.current['id']
                or saved.get('task_status') != 'accepted'
                or not approval.get('checks_digest') or not receipt.get('digest')
                or approval.get('context') != receipt.get('context')):
            reviewer.verdict.gaps.append('已发布派单的原审查结论或检查回执发生变化，无法恢复验收登记。')
            return False
        worker.git_checkpoint, reviewer.git_checkpoint = history.last_worker, checkpoint
        self._event('function_publication_recovered', task_id=self.function_tasks.current['id'], round=worker.index)
        return True
