"""Recovery decisions and evidence archiving."""

from __future__ import annotations

from auto_moniter.providers import ProviderError
from auto_moniter.workspace import workspace_fingerprint
from auto_moniter.storage import write_json
from auto_moniter.failures import classify
from auto_moniter.redaction import redact
from auto_moniter import candidate_checks
from auto_moniter import referee
from auto_moniter.supervisor_models import RecoveryError, RunReport
from auto_moniter.command_runner import _paseo_archive, _paseo_stop


class RefereeRecovery:
    """Recovery decisions and evidence archiving."""

    def _consult_referee(self, kind, reason, records, *, candidate=None, archive=None):
        """Apply one bounded scheduling decision without granting acceptance."""
        if not self.config.referee or self.config.no_review or not self.config.reviewer or self._stop:
            return ''
        self._process_control()
        if self._stop or classify(reason)[1] == 'stop':
            return ''
        self._check_budget(new_call=True)
        tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        revision = self.directions.revision(self.config.requirement)['sha256']
        current = self.function_tasks.current
        context = {'kind': kind, 'tree': tree, 'task': current,
                   'cycle': len(self.requirement_cycles) + 1, 'revision': revision,
                   'provider_config': self._provider_config(), 'checks': self.config.checks,
                   'referee_model': self.config.referee_model or self._reviewer_model()}
        if browser_recovery := kind == 'browser_evidence':
            context['browser_failure'] = candidate_checks.digest(reason)
        if archive:
            context['field_binding'] = archive['binding']
        journal = referee.Journal(self.run_dir, archive=bool(archive), browser=browser_recovery)
        try:
            ticket = journal.begin(context, reason, once=bool(archive))
        except (OSError, ValueError, RecursionError) as exc:
            self.log('Referee recovery unavailable: ' + str(exc))
            if archive:
                return ''
            self._stop = True
            self._stop_reason = 'Referee recovery unavailable: ' + str(exc)
            return ''
        if ticket is None:
            if archive:
                return ''
            self._event('referee_recovery_unchanged', incident=kind, reason=reason)
            self._stop = True
            self._stop_reason = '同一故障的裁判处理尚未完成或已失败。已保留进度，等待新的证据或执行条件。'
            return ''
        index_base = 940000 if browser_recovery else 930000 if archive else 920000
        ident = ticket['id']
        index = index_base + ident if ident < 10000 else ident * 10 + index_base // 10000 + 10_000_000
        if archive and ticket['status'] == 'captured':
            try:
                return self._apply_referee_archive(journal, ticket, archive, records, candidate)
            except (OSError, ValueError, KeyError) as exc:
                journal.update(ticket['id'], 'failed', error=redact(str(exc))[:4000])
                self._event('referee_rejected', decision_id=ticket['id'], reason=str(exc))
                return ''
        diagnosis = {**context, 'incident': reason, 'original_requirement': self.config.requirement,
            'acceptance': self.config.acceptance, 'requirements_review': self.requirements_review,
            'original_plan': self.function_plan, 'assignments': self.function_tasks.export(),
            'modules': self.modules.export(), 'audit_issues': self.audit.export(), 'usage': self._usage(),
            'recent_records': [record for record in RunReport('', 0, '', '', records[-8:]).to_dict()['records']],
            'prior_decisions': journal.read()['decisions'][-13:-1]}
        if archive:
            diagnosis['archive_request'] = archive
            diagnosis['available_checks'] = candidate_checks.review_summary(candidate.watcher_result['candidate_checks'])
        schema = self.schema_path
        path = self.run_dir / 'referee.schema.json'
        write_json(path, referee.SCHEMA)
        self.schema_path = path
        self._event('referee_start', incident=kind, decision_id=ticket['id'])
        try:
            self.config.reviewer.validate_review(self.config.reviewer_extra_args)
            turn = self._invoke(self.config.reviewer, referee.prompt(diagnosis), '', role='referee', index=index,
                model=self.config.referee_model or self._reviewer_model(),
                extra_args=self.config.reviewer_extra_args, read_only=True)
            self._process_control()
            if self._stop:
                return ''
            if (workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree
                    or self.directions.revision(self.config.requirement)['sha256'] != revision
                    or self._business_head_changed() or self.project and self.project.needs_review):
                raise ValueError('Referee inputs changed; decision was not applied')
            if turn.exit_code or turn.timed_out or turn.interruption:
                raise ValueError('Referee did not finish: ' + (turn.stderr or turn.interruption or str(turn.exit_code)))
            decision = referee.parse(turn.text)
            action = decision['action']
            if ((archive and action not in {'archive_checks', 'wait'})
                    or (not archive and action == 'archive_checks')):
                raise ValueError('The selected action is unavailable for this incident')
            if kind in {'reviewer_failure', 'browser_evidence'} and action == 'retry_worker':
                raise ValueError('Review transport failure cannot authorize repeating implementation')
            if kind == 'planning' and action not in {'replan', 'wait'}:
                raise ValueError('Failed planning requires replanning or a concrete external prerequisite')
            if action == 'retry_reviewer' and (candidate is None or candidate.failure or candidate.interruption
                                             or candidate.exit_code or candidate.timed_out):
                raise ValueError('Reviewer retry requires an intact submitted worker candidate')
            if (action == 'retry_reviewer' and candidate_checks.observed_assertion_failure(
                    candidate.watcher_result.get('candidate_checks', {}), self.run_dir)):
                raise ValueError('Failing behavioral assertions require implementation repair or replanning')
            if action == 'replan' and self.config.workflow != 'review-first':
                raise ValueError('Referee replanning requires the review-first workflow')
            if not archive and journal.already_applied(ticket, action):
                raise ValueError('同一故障已执行过这项恢复动作，需要新的证据或另一种处理方式。')
            # Save the decision before changing any scheduling state. A crash
            # here must not issue the same model call again on restart.
            captured = {'decision': decision, 'output_file': turn.output_path.name}
            if archive:
                captured['archive'] = archive
            journal.update(ticket['id'], 'captured', **captured)
            if archive:
                self.directions.receipt(self._direction_ids['referee'], 'referee', index, responded=True)
                return self._apply_referee_archive(journal, {**ticket, **captured}, archive, records, candidate)
            self._referee_decision = {**decision, 'id': ticket['id'], 'cycle': context['cycle'],
                                      'incident': kind, 'task_id': current['id'] if current else ''}
            if action == 'wait':
                self._stop = True
                self._stop_reason = 'Referee waiting for prerequisite: ' + decision['instructions']
            else:
                errors = self._close_delivered_sessions()
                if errors:
                    raise RecoveryError('Referee recovery cannot close previous agents: ' + '; '.join(errors))
                self.worker_session_id = self.reviewer_session_id = ''
                self._drop_worker_session = True
                self._referee_replan = action == 'replan'
                if candidate is not None:
                    candidate.watcher_result['referee_route'] = {'action': action, 'id': ticket['id'], 'tree': tree}
                self._recovery_note = 'Referee scheduling decision: ' + decision['instructions']
            self.directions.receipt(self._direction_ids['referee'], 'referee', index, responded=True)
            self._write_state(records, 'running', 0, '', tree)
            journal.update(ticket['id'], 'applied')
            self._event('referee_decision', decision_id=ticket['id'], action=action,
                        summary=decision['summary'], output_file=turn.output_path.name)
            return '' if action == 'wait' else action
        except (ValueError, ProviderError, OSError, KeyError) as exc:
            if not archive and isinstance(exc, (OSError, KeyError)):
                raise
            journal.update(ticket['id'], 'failed', error=str(exc)[:4000])
            self._event('referee_rejected', decision_id=ticket['id'], reason=str(exc))
            if archive:
                return ''
            self._stop = True
            self._stop_reason = 'Referee recovery rejected: ' + str(exc)
            return ''
        finally:
            self.schema_path = schema
            if self.config.reviewer.name == 'paseo' and self._active_reviewer_id:
                stopped = _paseo_stop(self.config.reviewer.binary, self._active_reviewer_id, self.config.cwd)
                archived = _paseo_archive(self.config.reviewer.binary, self._active_reviewer_id, self.config.cwd)
                if not stopped or not archived:
                    raise RecoveryError('Referee session cleanup failed')
                self._active_reviewer_id = ''

    def _apply_referee_archive(self, journal, ticket, archive, records, candidate):
        from auto_moniter import field_handoff

        decision = ticket['decision']
        if ticket['archive'] != archive:
            raise ValueError('归档请求已发生变化。')
        action = decision['action']
        if action == 'archive_checks':
            with self.directions.lock():
                self._process_control(active=True)
                if self._stop:
                    return ''
                if (self._unplanned_directions() or self._pending_directions()
                        or self._business_head_changed()
                        or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != candidate.tree):
                    raise ValueError('候选或任务要求已变化，须重新核对归档。')
                state = self.directions.read('state.json', 32 * 1024 * 1024)
                handoff = field_handoff.read(self.run_dir)
                if handoff.get('revision') != archive['revision']:
                    raise ValueError('交接待办已更新。')
                field_handoff.archive_checks(self.run_dir, state, handoff, archive['item'],
                                             candidate.watcher_result['candidate_checks'])
        elif action != 'wait':
            raise ValueError('该裁判决策无法处理管理端归档。')
        self._referee_decision = {**decision, 'id': ticket['id'], 'cycle': len(self.requirement_cycles) + 1,
                                  'incident': 'field_archive', 'task_id': archive['binding']['task_id']}
        self._write_state(records, 'running', 0, '', candidate.tree)
        journal.update(ticket['id'], 'applied')
        self._event('referee_decision', decision_id=ticket['id'], action=action,
                    summary=decision['summary'], output_file=ticket['output_file'])
        return 'archive_checks' if action == 'archive_checks' else ''
