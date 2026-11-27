"""Business branch synchronization and approved publication."""

from __future__ import annotations

import json
from auto_moniter.workspace import file_snapshot, snapshot_fingerprint, workspace_fingerprint
from auto_moniter.storage import write_json
from auto_moniter import function_gate
from auto_moniter.git_history import GitHistoryError
from auto_moniter.publication import StagedChangesConflict
from auto_moniter.business_recovery import BusinessHeadChanged
from auto_moniter.project_workspace import ProjectInputsChanged
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter.supervisor_models import BudgetExceeded, RecoveryError, RunReport


class Publication:
    """Business branch synchronization and approved publication."""

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
                    boundary=lambda: self._publication_boundary(reviewer), receipt=receipt)
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
