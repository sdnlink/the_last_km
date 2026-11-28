"""Recovery decisions and evidence archiving."""

from __future__ import annotations

import json

from auto_moniter.providers import ProviderError
from auto_moniter.workspace import workspace_fingerprint
from auto_moniter.storage import write_json
from auto_moniter.failures import classify
from auto_moniter.redaction import redact
from auto_moniter import candidate_checks
from auto_moniter import field_handoff, referee
from auto_moniter.supervisor_models import RecoveryError, RefereeRedirect, RunReport
from auto_moniter.command_runner import _paseo_archive, _paseo_stop
from auto_moniter.runtime_policy import transient_model_failure


class RefereeRecovery:
    """Recovery decisions and evidence archiving."""

    @staticmethod
    def _recovery_candidate(record):
        return {'round': record.index, 'task_id': record.verdict.task_id or '', 'tree': record.tree,
                'checks': record.watcher_result.get('candidate_checks', {}).get('digest', '')}

    def _recovery_context(self, kind, reason, archive=None, candidate=None):
        tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        revision = self.directions.revision(self.config.requirement)['sha256']
        current = self.function_tasks.current
        context = {'kind': kind, 'tree': tree, 'task': current,
                   'cycle': len(self.requirement_cycles) + 1, 'revision': revision,
                   'provider_config': self._provider_config(), 'checks': self.config.checks,
                   'referee_model': self.config.referee_model or self._reviewer_model()}
        if kind == 'browser_evidence':
            context['browser_failure'] = candidate_checks.digest(reason)
        if archive:
            context['field_binding'] = archive['binding']
        elif candidate is not None:
            context['candidate'] = self._recovery_candidate(candidate)
            if kind == 'verification_evidence':
                context['verification_receipt'] = context['candidate']['checks']
        return context

    def _resume_referee_decision(self, records):
        if not self.config.referee or not self.config.reviewer or self._stop:
            return
        for browser in (False, True):
            journal = referee.Journal(self.run_dir, browser=browser)
            try:
                tickets = journal.read()['decisions']
            except (OSError, ValueError):
                continue
            if not tickets:
                continue
            ticket = tickets[-1]
            kind = ticket.get('incident')
            if not kind or ticket['status'] not in {'requested', 'captured'}:
                continue
            reason = ticket['reason']
            fallback = {}
            if browser:
                fallback = field_handoff.pending_fallback(field_handoff.read(self.run_dir))
                if not fallback or ticket.get('browser_failure') != candidate_checks.digest(self._field_failure(fallback)[0]):
                    continue
            checkpoint = ticket.get('candidate')
            candidate = None
            if checkpoint:
                candidate = next((row for row in reversed(records) if row.role == 'worker'
                                  and self._recovery_candidate(row) == checkpoint), None)
                if candidate is None:
                    self._event('referee_recovery_stale', decision_id=ticket['id'], reason='候选轮次或检查回执已变化。')
                    continue
            context = self._recovery_context(kind, reason, candidate=candidate)
            if ticket.get('browser_failure'):
                context['browser_failure'] = ticket['browser_failure']
            if referee.incident_key(context) != ticket['key']:
                continue
            if not checkpoint:
                current = context['task'] or {}
                candidate = next((row for row in reversed(records) if row.role == 'worker'
                                  and row.tree == context['tree'] and (row.verdict.task_id or '') == current.get('id', '')), None)
            action = self._consult_referee(kind, reason, records, candidate=candidate, saved_context=context)
            if browser and (action == 'replan' or self._stop_reason.startswith('Referee waiting for prerequisite:')):
                field_handoff.finish_fallback(self.run_dir, fallback)
            return

    @staticmethod
    def _field_failure(fallback):
        evidence = {key: fallback[key] for key in ('failures', 'waiting') if key in fallback}
        external = any(item.get('kind') in {'deployment', 'observation'} for item in evidence.get('failures', {}).values())
        reason = ('现场补证未完成。请核对最新用户要求与当前依赖，安排可执行的补证或独立派单。'
                  if external else '浏览器自动用例未完成。请确定可执行的补证路径。')
        return reason + json.dumps(evidence, ensure_ascii=False), evidence

    def _field_fallback_route(self, fallback, records, candidate):
        if not fallback:
            return '', {}
        reason, evidence = self._field_failure(fallback)
        if fallback.get('role') != 'referee' or not self.config.referee:
            return '', evidence
        route = candidate.watcher_result.get('referee_route', {})
        if route.get('action') == 'retry_reviewer' and route.get('fallback_key') == fallback.get('key'):
            return self._referee_decision['instructions'], evidence
        action = self._consult_referee('browser_evidence', reason, records, candidate=candidate)
        if action == 'replan' or self._stop_reason.startswith('Referee waiting for prerequisite:'):
            field_handoff.finish_fallback(self.run_dir, fallback)
        if action == 'replan':
            raise RefereeRedirect(reason)
        if self._stop and not self._stop_reason:
            self._stop_reason = reason
        return self._referee_decision['instructions'] if action == 'retry_reviewer' else '', evidence

    def _consult_referee(self, kind, reason, records, *, candidate=None, archive=None, saved_context=None):
        """Apply one bounded scheduling decision without granting acceptance."""
        if not self.config.referee or self.config.no_review or not self.config.reviewer or self._stop:
            return ''
        self._process_control()
        if self._stop or classify(reason)[1] == 'stop':
            return ''
        context = saved_context or self._recovery_context(kind, reason, archive, candidate)
        tree, revision = context['tree'], context['revision']
        browser_recovery = kind == 'browser_evidence'
        journal = referee.Journal(self.run_dir, archive=bool(archive), browser=browser_recovery)
        try:
            ticket = journal.begin(context, reason, once=bool(archive), resume=self.config.resume)
        except (OSError, ValueError, RecursionError) as exc:
            self.log('Referee recovery unavailable: ' + str(exc))
            if archive:
                return ''
            self._stop = True
            self._stop_reason = 'Referee recovery unavailable: ' + str(exc)
            return ''
        if ticket is None:
            if archive or kind == 'verification_evidence':
                return ''
            self._event('referee_recovery_unchanged', incident=kind, reason=reason)
            self._stop = True
            self._stop_reason = '同一故障的裁判处理尚未完成或已失败。已保留进度，等待新的证据或执行条件。'
            return ''
        index_base = 940000 if browser_recovery else 930000 if archive else 920000
        ident = ticket['id']
        index = index_base + ident if ident < 10000 else ident * 10 + index_base // 10000 + 10_000_000
        if ticket['status'] == 'captured':
            if archive:
                try:
                    return self._apply_referee_archive(journal, ticket, archive, records, candidate)
                except (OSError, ValueError, KeyError) as exc:
                    journal.update(ticket['id'], 'failed', error=redact(str(exc))[:4000])
                    self._event('referee_rejected', decision_id=ticket['id'], reason=str(exc))
                    return ''
            try:
                return self._apply_recovery_decision(journal, ticket, context, records, candidate)
            except ValueError as exc:
                journal.update(ticket['id'], 'failed', error=redact(str(exc))[:4000])
                self._event('referee_rejected', decision_id=ticket['id'], reason=str(exc))
                return ''
        self._check_budget(new_call=True)
        diagnosis = {**context, 'incident': reason, 'original_requirement': self.config.requirement,
            'acceptance': self.config.acceptance, 'requirements_review': self.requirements_review,
            'original_plan': self.function_plan, 'assignments': self.function_tasks.export(),
            'modules': self.modules.export(), 'audit_issues': self.audit.export(), 'usage': self._usage(),
            'recent_records': [record for record in RunReport('', 0, '', '', records[-8:]).to_dict()['records']],
            'prior_decisions': journal.read()['decisions'][-13:-1],
            'already_applied_actions': journal.actions_applied(ticket)}
        if archive:
            diagnosis['archive_request'] = archive
            diagnosis['available_checks'] = candidate_checks.review_summary(candidate.watcher_result['candidate_checks'])
        elif kind in {'planning', 'verification_evidence'}:
            if options := self._git_tree_check_options(candidate):
                diagnosis['git_tree_check_options'] = options
                diagnosis['available_checks'] = candidate_checks.review_summary(candidate.watcher_result['candidate_checks'])
        schema = self.schema_path
        path = self.run_dir / 'referee.schema.json'
        write_json(path, referee.SCHEMA)
        self.schema_path = path
        self._event('referee_start', incident=kind, decision_id=ticket['id'])
        provider = self.config.reviewer
        model = self.config.referee_model or self._reviewer_model()
        extra_args = self.config.reviewer_extra_args
        try:
            for attempt in range(2):
                provider.validate_review(extra_args)
                turn = self._invoke(provider, referee.prompt(diagnosis), '', role='referee', index=index,
                    model=model, extra_args=extra_args, read_only=True)
                self._process_control()
                if self._stop:
                    return ''
                if (workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree
                        or self.directions.revision(self.config.requirement)['sha256'] != revision
                        or self._business_head_changed() or self.project and self.project.needs_review):
                    raise ValueError('Referee inputs changed; decision was not applied')
                if turn.exit_code or turn.timed_out or turn.interruption:
                    if transient_model_failure(turn.interruption, turn.stderr):
                        backup = self.config.provider
                        if (attempt == 0 and self.quality.config.get('referee_transport_fallback')
                                and backup.name != provider.name and backup.name != 'paseo'):
                            provider, model, extra_args = backup, self.config.model, []
                            self._event('referee_provider_fallback', decision_id=ticket['id'], provider=provider.name)
                            continue
                        if not archive:
                            self._stop = True
                            self._stop_reason = '裁判模型服务暂不可用。已保存原任务和裁判请求，服务恢复后继续。'
                            return ''
                    raise ValueError('Referee did not finish: ' + (turn.stderr or turn.interruption or str(turn.exit_code)))
                decision = referee.parse(turn.text)
                action = decision['action']
                if ((archive and action not in {'archive_checks', 'wait'})
                        or (not archive and action == 'archive_checks')):
                    raise ValueError('The selected action is unavailable for this incident')
                if kind in {'reviewer_failure', 'browser_evidence'} and action == 'retry_worker':
                    raise ValueError('Review transport failure cannot authorize repeating implementation')
                if kind == 'planning' and action not in {'replan', 'wait', 'run_git_tree_checks'}:
                    raise ValueError('Failed planning requires replanning or a concrete external prerequisite')
                if kind == 'verification_evidence' and action == 'retry_worker':
                    raise ValueError('当前派单为只读补验，应安排补验、复核或调整后续计划。')
                if action == 'run_git_tree_checks':
                    try:
                        if 'git_tree_check_options' not in diagnosis:
                            raise ValueError('当前故障没有可补验的已提交候选。')
                        self._git_tree_supplements(candidate, decision['git_tree_checks'])
                        if kind == 'planning':
                            self._approved_brief_for_candidate_review()
                    except ValueError as exc:
                        if attempt == 0:
                            diagnosis['decision_correction'] = str(exc)
                            self._event('referee_decision_repair', decision_id=ticket['id'], action=action)
                            continue
                        raise
                if action == 'retry_reviewer' and (candidate is None or candidate.failure or candidate.interruption
                                                 or candidate.exit_code or candidate.timed_out):
                    raise ValueError('Reviewer retry requires an intact submitted worker candidate')
                if (action == 'retry_reviewer' and candidate_checks.observed_assertion_failure(
                        candidate.watcher_result.get('candidate_checks', {}), self.run_dir)):
                    raise ValueError('Failing behavioral assertions require implementation repair or replanning')
                if action == 'replan' and self.config.workflow != 'review-first':
                    raise ValueError('Referee replanning requires the review-first workflow')
                if not archive and journal.already_applied(ticket, action):
                    if attempt == 0:
                        diagnosis['decision_correction'] = '同一故障已执行过该动作，请使用不同的处理方式或指出具体等待条件。'
                        self._event('referee_decision_repair', decision_id=ticket['id'], action=action)
                        continue
                    raise ValueError('同一故障已执行过这项恢复动作，需要新的证据或另一种处理方式。')
                break

            # Save the decision before changing any scheduling state. A crash
            # here must not issue the same model call again on restart.
            captured = {'decision': decision, 'output_file': turn.output_path.name}
            if archive:
                captured['archive'] = archive
            journal.update(ticket['id'], 'captured', **captured)
            if archive:
                self.directions.receipt(self._direction_ids['referee'], 'referee', index, responded=True)
                return self._apply_referee_archive(journal, {**ticket, **captured}, archive, records, candidate)
            self.directions.receipt(self._direction_ids['referee'], 'referee', index, responded=True)
            return self._apply_recovery_decision(journal, {**ticket, **captured}, context, records, candidate)
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

    def _apply_recovery_decision(self, journal, ticket, context, records, candidate):
        decision = ticket['decision']
        kind, tree, action = context['kind'], context['tree'], decision['action']
        saved = self._referee_decision or {}
        if (saved.get('id'), saved.get('incident'), saved.get('decision_key')) == (ticket['id'], kind, ticket['key']):
            journal.update(ticket['id'], 'applied')
            if action == 'wait' and kind != 'verification_evidence':
                self._stop = True
                self._stop_reason = 'Referee waiting for prerequisite: ' + decision['instructions']
                return ''
            return action
        if action == 'run_git_tree_checks':
            if candidate is None:
                raise ValueError('补验所引用的原候选已失效。')
            additions = self._git_tree_supplements(candidate, decision['git_tree_checks'])
            if kind == 'planning':
                self.requirements_review = self._approved_brief_for_candidate_review()
            self._save_supplemental_checks(candidate, additions)
            candidate.watcher_result['referee_route'] = {'action': action, 'id': ticket['id'], 'tree': tree}
            self.reviewer_session_id = ''
            self._recovery_note = '裁判已安排 Watcher 补验，完成后由 Reviewer 复核原派单。'
        elif action == 'wait' and kind != 'verification_evidence':
            self._stop = True
            self._stop_reason = 'Referee waiting for prerequisite: ' + decision['instructions']
            if context['task'] and self._function_gate:
                missing = self._missing_dispatch_inputs(context['task'])
                if missing:
                    self._stop_reason = self._wait_for_dispatch_inputs(context['task'], missing)
        elif action != 'wait':
            errors = self._close_delivered_sessions()
            if errors:
                raise RecoveryError('Referee recovery cannot close previous agents: ' + '; '.join(errors))
            self.worker_session_id = self.reviewer_session_id = ''
            self._drop_worker_session = True
            self._referee_replan = action == 'replan'
            if candidate is not None:
                candidate.watcher_result['referee_route'] = {'action': action, 'id': ticket['id'], 'tree': tree}
                if kind == 'browser_evidence':
                    fallback = field_handoff.pending_fallback(field_handoff.read(self.run_dir))
                    if fallback:
                        candidate.watcher_result['referee_route']['fallback_key'] = fallback['key']
            self._recovery_note = 'Referee scheduling decision: ' + decision['instructions']
        self._referee_decision = {**decision, 'id': ticket['id'], 'cycle': context['cycle'],
                                  'incident': kind, 'decision_key': ticket['key'], 'task_id': (context['task'] or {}).get('id', '')}
        if action == 'run_git_tree_checks' and self._referee_replan:
            self._deferred_replan = self._replan_binding(context['task'], self.directions.messages())
        self._write_state(records, 'running', 0, '', tree)
        journal.update(ticket['id'], 'applied')
        self._event('referee_decision', decision_id=ticket['id'], action=action,
                    summary=decision['summary'], output_file=ticket['output_file'])
        return '' if action == 'wait' else action

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
