"""Requirement planning, coverage review and follow-up cycles."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

from auto_moniter.workspace import file_snapshot, snapshot_fingerprint, workspace_fingerprint
from auto_moniter.modules import ModuleLedger
from auto_moniter.storage import write_json
from auto_moniter.redaction import redact, safe
from auto_moniter import candidate_checks, check_git_tree, check_history, function_tasks
from auto_moniter.business_recovery import validate_change
from auto_moniter import requirements_review, requirement_context, requirement_coverage
from auto_moniter import function_plan
from auto_moniter.skills import browser_guidance
from auto_moniter import migration
from auto_moniter import assignment_delivery
from auto_moniter.supervisor_models import RecoveryError, _CompletedTurn, _accepted
from auto_moniter.command_runner import _paseo_archive, _paseo_stop


class _PlanningTransportFailure(ValueError):
    pass


class RequirementPlanning:
    """Requirement planning, coverage review and follow-up cycles."""

    def _begin_follow_up(self, records, review, review_record=None):
        if not self._record_follow_up(records, review, review_record):
            return False
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

    def _recover_requirement_handoff(self, records):
        """Recover final bookkeeping from the published audit pair and saved check receipt."""
        history = self.git_history
        if (self.config.workflow != 'review-first' or self.config.no_review
                or self.config.function_dispatch or self.project
                or not getattr(history, 'business_commits', False)):
            return None
        worker = next((record for record in reversed(records) if record.role == 'worker'), None)
        if not worker or worker.verdict.scope != 'requirement':
            return None
        if self.requirement_cycles and worker.index <= self.requirement_cycles[-1]['handoff']['round']:
            return None
        review = next((record for record in reversed(records)
                       if record.role == 'reviewer' and record.index == worker.index), None)
        publication = history.last_business_commit
        if (not review or review.verdict.scope != 'requirement'
                or (history.last_record.get('round') != worker.index
                    and publication.get('round') != worker.index
                    and not review.git_checkpoint.get('business_commit'))):
            return None

        history.verify()
        checkpoint = history.last_record
        if (not checkpoint or checkpoint.get('round') != worker.index
                or checkpoint.get('commit') != history.head
                or checkpoint != review.git_checkpoint or history.last_worker != worker.git_checkpoint
                or checkpoint.get('business_commit') != publication
                or history.last_worker.get('business_commit') != publication
                or publication.get('round') != worker.index
                or not publication.get('commit') or not publication.get('candidate_tree')):
            raise RecoveryError('Published requirement does not match the saved Git checkpoints')

        path = (self.config.cwd.relative_to(history.root) / checkpoint['record']).as_posix()
        audit_record = json.loads(history.git('show', history.head + ':' + path))
        result = audit_record['result']
        saved_approval = result.get('watcher_result', {}).get('candidate_approval')
        if (audit_record.get('role') != 'reviewer' or audit_record.get('round') != worker.index
                or audit_record.get('before') != history.last_worker['commit']
                or result.get('tree') != worker.tree or result.get('business_commit') != publication
                or not isinstance(saved_approval, dict)
                or saved_approval != review.watcher_result.get('candidate_approval')
                or not isinstance(saved_approval.get('context'), dict)):
            raise RecoveryError('Published requirement does not match the immutable review record')

        published_head = {key: publication[key] for key in ('ref', 'commit')}
        saved_context = saved_approval['context']
        published_update_count = saved_context.get('business_update_count')
        if (published_head != history.business_head and not history.candidate
                and self._business_input.get('after') == history.business_head
                and type(published_update_count) is int
                and 0 <= published_update_count < self._business_update_count):
            # Reconciliation clears the candidate. This also covers a restart
            # after the new business input was saved but before a Worker ran.
            validate_change(history.git, published_head, history.business_head)
            return None

        context = self._candidate_check_context(worker)
        direction_ids = saved_context.get('direction_ids')
        if (not isinstance(direction_ids, list)
                or direction_ids != context['direction_ids'][:len(direction_ids)]):
            raise RecoveryError('Published requirement does not match the saved direction history')
        old_checks = saved_context.get('configured_checks')
        current_checks = context['configured_checks']
        if old_checks not in check_history.configurations(current_checks, self._check_changes):
            raise RecoveryError('Published requirement does not match the saved acceptance check history')

        # Git binds the receipt to the configuration used at publication.
        # Resume has already authorized any new role assignment independently.
        approval = self._verified_candidate_approval(review, worker, saved_context)
        receipt = worker.watcher_result.get('candidate_checks')
        resume_fields = {'provider_config', 'worker_model', 'reviewer_model', 'direction_ids',
                         'configured_checks', 'commands', 'check_timeout_s'}
        if (published_head != history.business_head
                or publication['candidate_tree'] != history.candidate.get('commit')
                or history.git('rev-parse', publication['commit'] + '^{tree}').decode().strip()
                    != history.candidate.get('business_tree')
                or not approval or {**approval.to_dict(), 'modules': []} != review.verdict.to_dict()
                or any(saved_context.get(key) != value for key, value in context.items() if key not in resume_fields)
                or not saved_context.get('final') or not receipt['passed']
                or not _accepted(worker.verdict) or worker.tree != review.tree
                or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != worker.tree
                or self.modules.coverage(worker.verdict, approval) or self.audit.pending()
                or self.requirements_review.get('status') != 'ready'
                or self.requirements_review.get('cycle', 1) != len(self.requirement_cycles) + 1):
            raise RecoveryError('Published requirement does not match the saved candidate, approval or checks')

        self._delivery_tree = worker.tree
        review.delivery = {'status': 'accepted', 'reasons': []}
        self.modules.accept(worker.verdict, approval, worker.index, worker.tree)
        if (direction_ids != context['direction_ids'] or self._pending_directions()
                or old_checks != current_checks or saved_context['commands'] != context['commands']
                or saved_context.get('check_timeout_s') != context['check_timeout_s']):
            return None
        return review

    def _record_follow_up(self, records, review, review_record):
        """Save an explicitly queued requirement or an authorized automatic follow-up.

        A jointly accepted requirement still finishes the run unless the original
        request asks to continue, or the person queued a concrete requirement. Repair
        rounds are not follow-ups: open audit issues stay with the worker.
        """
        queued = self.requirement_queue.snapshot(self._requirement_queue_state())['waiting']
        if not queued and not requirements_review.requests_follow_up(self.config.requirement):
            return False
        if self.audit.pending():
            return False
        if self._pending_directions():
            return False
        self._close_follow_up_sessions()
        with self.directions.lock():
            if self._pending_directions():
                return False
            queued = self.requirement_queue.snapshot(self._requirement_queue_state())['waiting']
            if not queued and not requirements_review.requests_follow_up(self.config.requirement):
                return False
            self._save_requirement_handoff(records, review, review_record, queued[0] if queued else None)
        return True

    def _requirement_queue_state(self):
        return {'requirement_cycles': self.requirement_cycles,
                'active_queued_requirement': self.active_queued_requirement, 'status': 'running'}

    def _save_requirement_handoff(self, records, review, review_record, next_queued):
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
        if review_record is not None:
            approval = review_record.watcher_result.get('candidate_approval', {})
            if approval.get('checks_digest'):
                handoff['checks_digest'] = approval['checks_digest']
        if self.active_queued_requirement or next_queued:
            handoff['queued_requirement'] = self.active_queued_requirement
            handoff['next_queued_requirement'] = next_queued['id'] if next_queued else None
        self.requirement_cycles.append({
            'index': handoff['cycle'],
            'status': 'committed',
            'requirements_review': self.requirements_review,
            'handoff': handoff,
        })
        self._event('requirement_follow_up', cycle=handoff['cycle'], tree=self._delivery_tree, commit=handoff['commit'])
        self.requirements_review = {}
        self.function_plan = None
        self._referee_decision = None
        self._referee_replan = False
        self.active_queued_requirement = ({key: next_queued[key] for key in ('id', 'text', 'at')}
                                          if next_queued else None)
        self.worker_session_id = ''
        self.reviewer_session_id = ''
        self._active_reviewer_id = ''
        self._drop_worker_session = False
        self._audit_feedback = None
        self._review_feedback = None
        self._write_state(records, 'running', 0, '', self._delivery_tree)

    def _close_follow_up_sessions(self):
        errors = self._close_delivered_sessions()
        if errors:
            raise RuntimeError('Requirement accepted, but agent cleanup failed: ' + '; '.join(errors))
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != self._delivery_tree:
            raise RuntimeError('Workspace changed while closing delivered agents; revalidation required')

    def _elaborate_requirement(self,records, *, next_function=False):
        while True:
            self._process_control()
            if self._stop:
                return False
            self._project_review_changed = False
            self._requirement_input_changed = False
            result = self._elaborate_requirement_once(records,next_function=next_function)
            if self._requirement_input_changed and not self._stop:
                self._event('requirements_input_retry',reason='New user directions arrived during review; stale plan was not dispatched')
                self._check_budget()
                continue
            if not self._project_review_changed or self._stop:
                if (not result and not self._stop
                        and self.requirements_review.get('failure_kind') != 'model_transport'):
                    latest = next((row for row in reversed(records) if row.role == 'worker'), None)
                    candidate = latest if not self._unplanned_directions() and self._git_tree_check_options(latest) else None
                    action = self._consult_referee(
                        'planning', self.requirements_review.get('summary', 'Requirement planning did not finish'),
                        records, candidate=candidate)
                    if action == 'run_git_tree_checks':
                        return True
                    if action:
                        next_function = bool(self.requirements_review.get('acceptance_criteria') or self.function_tasks.current
                                             or self._referee_replan)
                        continue
                return result
            self._event('project_review_retry', reason='Concurrent changes invalidated the review; preserving work and reviewing current inputs')
            self._check_budget()
            self._retry_pause(1,'coalescing concurrent project changes')
            self._sync_business_head(records)

    def _queued_cycle_can_add_modules(self):
        """Only a newly activated user request may extend the previous module ledger."""
        if (self._migration_context.get('active_queued_requirement') and not self.requirement_cycles
                and not self.function_tasks.current and not self.function_tasks.accepted):
            original_modules = self._migration_context['accepted_modules'] + self._migration_context['pending_modules']
            return set(self.modules.items) == set(original_modules)
        if not self.active_queued_requirement or not self.requirement_cycles or self.function_tasks.current:
            return False
        handoff = self.requirement_cycles[-1]['handoff']
        return (handoff.get('next_queued_requirement') == self.active_queued_requirement['id']
                and set(self.modules.items) == {item['id'] for item in handoff.get('modules', [])}
                and not any(item['round'] > handoff['round'] for item in self.function_tasks.accepted))

    def _stage_requirement_plan(self, assignment, modules, backlog, brief, *, tree, next_function=False):
        """Validate on cloned ledgers: no accepted state changes or worker dispatch."""
        from auto_moniter.modules import definitions
        if next_function:
            assignment, backlog = self._retain_linked_contracts(assignment, backlog)
        contracts = self.config.assignment_contracts or {}
        if contracts:
            assignment = assignment_delivery.preserve(contracts, assignment)
            if isinstance(backlog, list):
                backlog = [assignment_delivery.preserve(contracts, item) for item in backlog]
        proposed = definitions(modules)
        if any(m.get('files') or m.get('checks') for m in proposed):
            raise ValueError('Planning cannot supply executable checks or override fixed file scopes')
        if (self.modules.items and any(m['id'] not in self.modules.items for m in proposed)
                and not self._queued_cycle_can_add_modules()):
            raise ValueError('Function planning cannot expand fixed module scope')
        staged = ModuleLedger.restore(self.modules.export())
        staged.merge(proposed)
        dispatch = function_tasks.FunctionTasks(self.function_tasks.export())
        resumed_assignment = False
        preflight_repair = self._dispatch_preflight_repair()
        if next_function and self._independent_verification(assignment, backlog):
            dispatch.current = None
        elif next_function and self._amended_verification_replacement(assignment, backlog):
            dispatch.current = None
        elif self._referee_replan and dispatch.current:
            revised = function_tasks.validate(assignment, staged.items)
            if self._assignment_worker_started(dispatch.current) and revised['id'] == dispatch.current['id']:
                if not self._can_continue_started_assignment(dispatch.current, revised):
                    raise ValueError('已启动派单的目标、范围或原有检查发生变化。替换派单须使用新编号。')
                resumed_assignment = True
                if revised != dispatch.current:
                    previous = dispatch.current
                    same_head = isinstance(backlog, list) and backlog and backlog[0] == assignment
                    assignment = {**revised, 'objective': previous['objective'],
                                  'verification': list(dict.fromkeys([*previous['verification'], *revised['verification']]))}
                    if same_head:
                        backlog = [assignment, *backlog[1:]]
            dispatch.current = None
        elif next_function and dispatch.current:
            revised = function_tasks.validate(assignment, staged.items)
            if self._unstarted_assignment_test_conflicts():
                # Repair the test declaration only before this assignment starts.
                previous = dispatch.current
                retained = {k for k in previous if k not in {'test_files', 'review_test', 'check_ids', 'verification'}}
                approved = {key for key in previous['check_ids'] if not key.startswith('auto:')}
                changed = sorted(k for k in retained if revised.get(k) != previous[k])
                if changed:
                    raise ValueError('Test path repair changed protected assignment fields: ' + ', '.join(changed))
                missing = sorted(approved - set(revised['check_ids']))
                if missing:
                    raise ValueError('Test path repair removed approved check IDs: ' + ', '.join(missing))
                problems = function_plan.assertion_problems(revised)
                if problems:
                    raise ValueError(problems[0])
            elif preflight_repair:
                previous = dispatch.current
                fixed = {key for key in previous['check_ids'] if not key.startswith('auto:')}
                if revised['id'] != previous['id'] or revised['module_id'] != previous['module_id']:
                    raise ValueError('Dispatch repair must preserve the assignment ID and owning module')
                if (revised.get('kind') != previous.get('kind')
                        or revised['delivery_scope'] != previous['delivery_scope']):
                    raise ValueError('Dispatch repair must preserve the assignment type and delivery scope')
                if (not fixed <= set(revised['check_ids'])
                        or not set(previous['criterion_ids']) <= set(revised['criterion_ids'])):
                    raise ValueError('Dispatch repair must preserve fixed checks and assigned acceptance criteria')
                problems = function_plan.assertion_problems(revised)
                if problems:
                    raise ValueError(problems[0])
            elif function_tasks.identity(revised) != function_tasks.identity(dispatch.current):
                repairing_scope = bool((self._planning_rejection or {}).get('scope_repair'))
                if (not repairing_scope or function_tasks.identity(revised)[:-1] != function_tasks.identity(dispatch.current)[:-1]
                        or any(revised.get(key) != dispatch.current.get(key)
                               for key in ('test_files', 'criterion_ids', 'check_ids'))):
                    raise ValueError('Updated instructions must preserve the current assignment identity and target scope')
            dispatch.current = None
        dispatch.assign(assignment, staged.items)
        if self.project and self.project.scope:
            from auto_moniter.project_workspace import contains
            files = function_tasks.writable_files(dispatch.current)
            if any(not contains(self.project.scope,file) for file in files):
                raise ValueError('Parallel assignment targets must stay inside the declared task scope')
        if self._function_gate:
            self._validate_function_plan(dispatch, staged.items, brief)
            if dispatch.current.get('kind') == 'verification' and not self._assignment_repository_conflict(dispatch.current):
                from auto_moniter import verification_baseline
                verification_baseline.validate_candidate(self.run_dir, self.config.cwd, dispatch.current, tree,
                    cycle=len(self.requirement_cycles) + 1, after_round=self._verification_attempt_boundary())
            if not resumed_assignment and (self._referee_replan or preflight_repair or not self.function_tasks.current
                                           or self._unstarted_assignment_test_conflicts()):
                self._validate_new_assignment_tests(dispatch.current)
        elif dispatch.current.get('kind') in ('integration','verification') or dispatch.current.get('review_test'):
            raise ValueError('Integration, verification and reviewer-authored tests require the verified function gate')
        complete = (self._validate_complete_function_plan(backlog, dispatch, staged.items, brief)
                    if self.config.full_function_plan else None)
        if not self.config.full_function_plan and not self.function_tasks.current:
            problems = function_plan.assertion_problems(dispatch.current)
            if problems:
                raise ValueError(problems[0])
        return staged, dispatch, complete

    @staticmethod
    def _can_continue_started_assignment(previous, revised):
        if revised == previous:
            return True
        return (previous.get('kind') == 'verification'
                and function_tasks.identity(revised) == function_tasks.identity(previous)
                and set(previous['check_ids']) <= set(revised['check_ids'])
                and set(previous['criterion_ids']) <= set(revised['criterion_ids']))

    def _can_revise_waiting_verification(self):
        current = self.function_tasks.current
        return bool(self._verification_only() and current
                    and self._unplanned_directions()
                    and any(row.role == 'reviewer' and row.verdict.task_id == current['id']
                            and row.watcher_result.get('status') == 'blocked' for row in self._records))

    def _amended_verification_replacement(self, assignment, backlog=None):
        if not self._can_revise_waiting_verification() or not isinstance(assignment, dict):
            return False
        current = self.function_tasks.current
        revised = function_tasks.validate(assignment)
        inputs = self.config.quality_policy.get('assignment_inputs', {})
        if revised.get('kind') != 'verification' or revised['id'] == current['id']:
            return False
        # The environment amendment may move a read-only task ahead of a
        # retained external delivery, including a module-level checkpoint.
        if isinstance(backlog, list) and current in backlog:
            original = next((task for task in (self.function_plan or {}).get('tasks', [])
                             if task['id'] == revised['id']), None)
            return bool(revised['delivery_scope'] != 'requirement'
                        and (original is None or (
                            set(original['check_ids']) <= set(revised['check_ids'])
                            and set(original['criterion_ids']) <= set(revised['criterion_ids']))))
        # A new ID forces a fresh worker/check/review cycle. Original check and
        # criterion references remain enforceable under the amended environment.
        return (all(revised[key] == current[key] for key in ('module_id', 'delivery_scope'))
                and all(item in inputs.get(revised['id'], []) for item in inputs.get(current['id'], []))
                and set(current['check_ids']) <= set(revised['check_ids'])
                and set(current['criterion_ids']) <= set(revised['criterion_ids']))

    def _independent_verification(self, assignment, backlog, *, tree=None):
        """Allow a plan to propose a previously listed read-only task while field work waits."""
        from auto_moniter import field_handoff

        current = self.function_tasks.current
        if (not self.config.full_function_plan or not self._verification_only()
                or current['delivery_scope'] != 'function' or not self._complete_function_plan_ready()
                or not isinstance(backlog, list)
                or not isinstance(assignment, dict) or assignment.get('kind') != 'verification'
                or assignment.get('delivery_scope') != 'function'):
            return False
        try:
            assignment = function_tasks.validate(assignment)
            retained = [function_tasks.validate(item) for item in backlog
                        if isinstance(item, dict) and item.get('id') == current['id']]
        except ValueError:
            return False
        if retained != [current] or assignment not in function_plan.waiting(self.function_plan, self.function_tasks):
            return False
        handoff = field_handoff.read(self.run_dir)
        binding = handoff.get('binding', {})
        return bool(handoff.get('items') and not field_handoff.ready(handoff)
                    and binding.get('task_sha256') == candidate_checks.digest(current)
                    and binding.get('tree') == (tree or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir))
                    and set(assignment.get('depends_on', [])) <= {row['task']['id'] for row in self.function_tasks.accepted}
                    and not self._assignment_repository_conflict(assignment)
                    and not self._missing_dispatch_inputs(assignment))

    def _retain_linked_contracts(self, assignment, backlog):
        if not (self.config.assignment_deliveries and self.function_plan and isinstance(backlog, list)
                and backlog and isinstance(assignment, dict) and backlog[0] == assignment
                and all(isinstance(item, dict) and isinstance(item.get('id'), str) for item in backlog)):
            return assignment, backlog
        links = assignment_delivery.links(self.config.assignment_deliveries)
        current = self.function_tasks.current
        pending = function_plan.waiting(self.function_plan, self.function_tasks)
        if current:
            pending.insert(0, current)
        linked = {item['id']: item for item in pending if item['id'] in links
                  and self._assignment_repository_conflict(item)}
        missing = linked.keys() - {item['id'] for item in backlog}
        if missing:
            raise ValueError('派单计划须保留已关联验收任务的跨仓派单：' + '、'.join(sorted(missing)))
        backlog = [linked.get(item['id'], item) for item in backlog]
        if current and current['id'] in linked and not self._referee_replan:
            # Keep the checkpoint head during validation. Dispatch can then
            # advance independent local work from the complete approved plan.
            backlog.insert(0, backlog.pop(backlog.index(current)))
        return backlog[0], backlog

    def _close_requirement_reviewer(self):
        if self.config.reviewer.name == 'paseo' and self._active_reviewer_id:
            stopped = _paseo_stop(self.config.reviewer.binary, self._active_reviewer_id, self.config.cwd)
            archived = _paseo_archive(self.config.reviewer.binary, self._active_reviewer_id, self.config.cwd)
            if not stopped or not archived:
                raise RecoveryError('Requirement reviewer cleanup failed')
        self._active_reviewer_id = ''

    def _archive_requirement_review(self, index):
        artifact = f'{index:03d}-reviewer-repair-{self._agent_calls}'
        for suffix in ('.log', '-text.md', '-prompt.md'):
            source = self.run_dir / f'{index:03d}-reviewer{suffix}'
            if source.exists():
                source.replace(self.run_dir / (artifact + suffix))
        return artifact + '-text.md'

    def _invoke_planning_reviewer(self, prompt, index, *, directions, tree):
        from auto_moniter.runtime_policy import transient_model_failure

        for attempt in range(4):
            self._process_control()
            if (self._stop or self.directions.messages() != directions
                    or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree):
                return None
            turn = self._invoke(self.config.reviewer, prompt, '', role='reviewer', index=index,
                model=self._reviewer_model(), extra_args=self.config.reviewer_extra_args,
                read_only=True, direction_snapshot=directions)
            if turn.session_id:
                self._active_reviewer_id = turn.session_id
            self._process_control()
            if self._stop:
                return None
            if not (turn.exit_code or turn.timed_out or turn.interruption):
                return turn
            if not transient_model_failure(turn.interruption, turn.stderr):
                return turn
            if (self.directions.messages() != directions
                    or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree):
                self._archive_requirement_review(index)
                return None
            if attempt == 3:
                raise _PlanningTransportFailure(
                    f'Reviewer 的模型服务连接中断，自动重试后仍未恢复。方案和已验收结果已保存。'
                    f'模型服务恢复后可继续原任务。请查看 {turn.output_path.name}。')
            self._close_requirement_reviewer()
            archived = self._archive_requirement_review(index)
            self._event('planning_transport_retry', round=index, retry=attempt + 1,
                        log=archived.removesuffix('-text.md') + '.log')
            self._retry_pause(attempt + 1, 'Reviewer 模型服务连接中断')

    def _requirement_coverage_review(self, sources, plan, *, modules, checks, directions, tree):
        scoped_dispatch = self._function_gate and self.config.full_function_plan
        prompt = requirement_coverage.prompt(
            sources, plan, modules=modules, checks=checks, accepted=self.function_tasks.accepted,
            scoped_dispatch=scoped_dispatch)
        task = plan.get('next_task')
        if scoped_dispatch and task:
            missing = self._missing_dispatch_inputs(task)
            prompt += ('\n\nWatcher 派前核对结果：' + json.dumps({
                'task_id': task['id'],
                'missing_check_inputs': missing,
                'workspace_conflict': self._assignment_repository_conflict(task),
                'git_tree_checks': check_git_tree.inspect_checks(self.config.cwd, checks),
            }, ensure_ascii=False))
        schema = self.schema_path
        self.schema_path = self.run_dir / 'requirement-coverage.schema.json'
        try:
            write_json(self.schema_path, requirement_coverage.DISPATCH_SCHEMA if scoped_dispatch else requirement_coverage.SCHEMA)
            for attempt in range(2):
                self._close_requirement_reviewer()
                index = 2000000 + self._agent_calls
                self._event('requirement_coverage_started', round=index)
                turn = self._invoke_planning_reviewer(prompt, index, directions=directions, tree=tree)
                if (turn is None or self.directions.messages() != directions
                        or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree):
                    return None
                if turn.exit_code or turn.timed_out or turn.interruption:
                    detail = turn.interruption or turn.stderr or str(turn.exit_code)
                    raise ValueError(f'需求覆盖复核未完成。请查看 {turn.output_path.name} 后恢复任务。{detail}')
                try:
                    result = requirement_coverage.parse(turn.text, sources)
                except ValueError as exc:
                    if attempt == 0 and isinstance(exc, requirement_coverage.InvalidSource):
                        self._event('requirement_coverage_source_repair', round=index, log=turn.output_path.name)
                        prompt += requirement_coverage.source_repair_prompt(exc, turn.text)
                        continue
                    raise ValueError(f'{exc}。请查看 {turn.output_path.name} 后恢复任务。') from exc
                break
        finally:
            self.schema_path = schema
        receipt = f'{index:03d}-requirement-coverage.json'
        write_json(self.run_dir / receipt, safe({
            **result, 'plan_sha256': requirement_coverage.digest(plan),
            'sources_sha256': requirement_coverage.digest(sources), 'tree': tree,
            'checks_sha256': requirement_context.checks_digest(checks),
            'modules_sha256': requirement_coverage.digest(modules),
        }), durable=True)
        self._event('requirement_coverage_reviewed', status=result['status'], receipt=receipt,
                    round=index, summary=result.get('summary', ''))
        return result, receipt

    def _coverage_sources(self, directions):
        sources = {'original': self.config.requirement, 'acceptance': self.config.acceptance}
        if self.active_queued_requirement:
            sources['queued'] = self.active_queued_requirement['text']
        sources.update({item['id']: item['text'] for item in directions})
        return sources

    def _coverage_inputs(self, plan, staged_plan, sources, checks, tree):
        reviewed_plan = dict(plan)
        modules = self.modules.export()
        if staged_plan:
            ledger, dispatch, complete = staged_plan
            modules = ledger.export()
            reviewed_plan['next_task'] = dispatch.current
            if complete:
                reviewed_plan['function_plan'] = complete['tasks']
        binding = {
            'plan_sha256': requirement_coverage.digest(reviewed_plan),
            'sources_sha256': requirement_coverage.digest(sources),
            'checks_sha256': requirement_context.checks_digest(checks),
            'modules_sha256': requirement_coverage.digest(modules),
            'tree': tree,
        }
        return reviewed_plan, modules, binding

    def _saved_coverage_revision(self, continuation, *, checks, tree, sources, next_function):
        if (not continuation or continuation['source'] != 'saved requirement analysis'
                or continuation['workspace_changed'] or continuation['checks_changed']
                or continuation['new_direction_ids']):
            return None
        plan = continuation['previous_response']
        if plan['status'] != 'ready':
            return None
        try:
            brief = requirements_review.parse(json.dumps(
                {key: plan[key] for key in requirements_review.SCHEMA['required']}))
            brief['cycle'] = len(self.requirement_cycles) + 1
            staged = self._stage_requirement_plan(plan['next_task'], plan['modules'], plan.get('function_plan'),
                brief, tree=tree, next_function=next_function) if self.config.function_dispatch else None
            _, _, binding = self._coverage_inputs(plan, staged, sources, checks, tree)
        except (KeyError, ValueError):
            return None
        for path in sorted(self.run_dir.glob('*-requirement-coverage.json'), reverse=True):
            try:
                record = requirement_context.read(path, limit=128 * 1024)
                if not isinstance(record, dict) or any(record.get(k) != v for k, v in binding.items()):
                    continue
                schema = (requirement_coverage.DISPATCH_SCHEMA if 'independent_next_task' in record
                          else requirement_coverage.SCHEMA)
                review = requirement_coverage.parse(json.dumps(
                    {key: record[key] for key in schema['required']}), sources)
            except (OSError, KeyError, ValueError, RecursionError):
                continue
            return (review, path.name) if review['status'] == 'revise' else None
        return None

    def _saved_ready_dispatch(self, continuation, previous, *, index, tree, next_function):
        if (not self.config.resume or not self.config.function_dispatch or not next_function
                or previous.get('status') != 'failed' or not continuation
                or continuation['source'] != 'saved requirement analysis'
                or any(continuation.get(key) for key in ('workspace_changed', 'checks_changed', 'directions_changed'))):
            return None
        plan = continuation['previous_response']
        if plan.get('status') != 'ready':
            return None
        try:
            brief = requirements_review.parse(json.dumps(
                {key: plan[key] for key in requirements_review.SCHEMA['required']}))
            brief['cycle'] = len(self.requirement_cycles) + 1
            self._stage_requirement_plan(plan['next_task'], plan['modules'], plan.get('function_plan'),
                                         brief, tree=tree, next_function=True)
        except (KeyError, ValueError):
            return None
        path = self.run_dir / f'{index:03d}-reviewer.log'
        if not path.is_file() or path.is_symlink():
            return None
        return _CompletedTurn(json.dumps(plan, ensure_ascii=False), '', 0, False, 0, path, [])

    def _coverage_revision_prompt(self, prompt, review):
        return (prompt + '\n\n独立复核发现以下需求缺项。'
                '读取 ' + str(self.run_dir / 'requirement-review-context.json') + ' 中的 response 作为待修订方案。'
                '仅修订缺项及其直接关联的验收与依赖条目，照录其他条目并返回完整方案。'
                '仅在缺项需要新证据时查看对应文件。\n'
                + json.dumps({'findings': review['findings'], 'affected_tasks': review.get('affected_tasks', [])},
                             ensure_ascii=False))

    def _invoke_requirement_review(self, prompt, index, *, checks, next_function=False, previous_brief=None,
                                   directions, saved_revision=None, saved_dispatch=None):
        """Repair distinct planning errors with durable limits, without dispatching work."""
        from auto_moniter.adaptive_recovery import RecoveryJournal
        journal = RecoveryJournal(self.run_dir)
        tree = workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir)
        signature = self.project.signature if self.project else ''
        recovery_context = {'kind':'requirement_plan', 'tree':tree, 'index':index,
                            'task':self.function_tasks.current,
                            'directions':[item['id'] for item in directions]}
        try:
            prompt += journal.prompt()
        except (OSError, ValueError):
            pass  # Advisory history cannot prevent an otherwise valid review.
        base_prompt = prompt
        coverage_required = (saved_revision is not None or saved_dispatch is not None
                             or bool((previous_brief or {}).get('limited_dispatch')))
        if saved_revision:
            review, receipt = saved_revision
            prompt = self._coverage_revision_prompt(base_prompt, review)
            self._event('requirement_coverage_revision_resumed', receipt=receipt, round=index)
        for attempt in range(5):
            self._process_control()
            if self._stop:
                return None
            if attempt == 0 and saved_dispatch is not None:
                if (self.directions.messages() != directions
                        or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree):
                    return None
                self._direction_ids['reviewer'] = [item['id'] for item in directions]
                self._event('requirements_saved_dispatch_revalidated', round=index)
                turn = saved_dispatch
            else:
                turn = self._invoke_planning_reviewer(prompt, index, directions=directions, tree=tree)
            if turn is None:
                return None
            if turn.exit_code or turn.timed_out or turn.interruption:
                return turn
            if any(item['id'] not in self._direction_ids['reviewer'] for item in self.directions.messages()):
                return turn  # Rebuild the review with the latest user input first.
            staged_plan = None
            try:
                plan = json.loads(turn.text)
                if isinstance(plan,dict):
                    typed = {key:plan[key] for key in requirements_review.SCHEMA['required'] if key in plan}
                    try:
                        requirements_review.parse(json.dumps(typed))
                    except ValueError as error:
                        # Repair only a typed requirement report here. Arbitrary invalid
                        # output does not acquire an approved plan or an infinite retry.
                        requirements_review.parse(json.dumps(typed),context_only=True)
                        if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) == tree:
                            requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                                len(self.requirement_cycles)+1,plan,file_snapshot(self.config.cwd,ignore_dir=self.run_dir),
                                checks,[item for item in self.directions.messages()
                                                    if item['id'] in self._direction_ids['reviewer']])
                        raise requirements_review.RequirementReportRepair(str(error))
                assignment = plan.get('next_task') if isinstance(plan,dict) else None
                if isinstance(plan, dict) and plan.get('status') == 'needs_input' and not attempt:
                    brief = requirements_review.parse(json.dumps({key:plan[key] for key in requirements_review.SCHEMA['required'] if key in plan}))
                    if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) == tree:
                        requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                            len(self.requirement_cycles)+1,plan,file_snapshot(self.config.cwd,ignore_dir=self.run_dir),
                            checks,[item for item in self.directions.messages()
                                                if item['id'] in self._direction_ids['reviewer']])
                    raise requirements_review.NeedsInputReassessment(brief['summary'])
                if self.config.function_dispatch and isinstance(assignment,dict):
                    try:
                        function_tasks.validate_identifiers(assignment)
                    except function_tasks.AssignmentIdentifierError:
                        if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) == tree:
                            requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                                len(self.requirement_cycles)+1,plan,file_snapshot(self.config.cwd,ignore_dir=self.run_dir),
                                checks,[item for item in self.directions.messages()
                                                    if item['id'] in self._direction_ids['reviewer']])
                        raise
                if self.config.function_dispatch and isinstance(plan,dict) and plan.get('status') == 'ready':
                    try:
                        brief = requirements_review.parse(json.dumps(typed))
                        brief['cycle'] = len(self.requirement_cycles)+1
                        if next_function and (previous_brief or {}).get('status') == 'ready':
                            previous = previous_brief['acceptance_criteria']
                            if brief['acceptance_criteria'][:len(previous)] != previous:
                                brief['acceptance_criteria'] = list(previous)
                        staged_plan = self._stage_requirement_plan(
                            assignment, plan.get('modules'), plan.get('function_plan'), brief,
                            tree=tree, next_function=next_function)
                    except ValueError as error:
                        if workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) == tree:
                            requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                                len(self.requirement_cycles)+1,plan,file_snapshot(self.config.cwd,ignore_dir=self.run_dir),
                                checks,[item for item in self.directions.messages()
                                                    if item['id'] in self._direction_ids['reviewer']])
                        raise requirements_review.RequirementReportRepair(str(error))
            except (function_tasks.AssignmentIdentifierError, requirements_review.NeedsInputReassessment,
                    requirements_review.RequirementReportRepair) as exc:
                self._process_control()
                if (attempt >= 4 or self._stop or self._stop_requested
                        or workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir) != tree):
                    return turn
                try:
                    admitted = journal.admit(recovery_context, str(exc))
                except (OSError, ValueError) as history_error:
                    admitted = False
                    self.log('Automatic plan repair deferred: ' + redact(str(history_error)))
                if not admitted:
                    self._event('adaptive_recovery_exhausted', phase='requirement_plan', reason=str(exc))
                    return turn
                if self.project:
                    self.project.observe(force=True)
                    if signature != self.project.signature:
                        return turn
                self._close_requirement_reviewer()
                output_file = self._archive_requirement_review(index)
                # Keep the latest response and bounded error history, rather
                # than concatenating several complete copies of a large plan.
                prompt = base_prompt + journal.prompt()
                if isinstance(exc, requirements_review.NeedsInputReassessment):
                    self._event('requirements_blocker_reassessment',reason=str(exc),output_file=output_file)
                    prompt += requirements_review.reassessment_prompt(plan)
                    continue
                if isinstance(exc, requirements_review.RequirementReportRepair):
                    self._event('requirements_report_repair',reason=str(exc),output_file=output_file)
                    prompt += ('\n\nReviewer-only report repair; no worker has started. Correct only the report contradiction or plan validation errors: '
                               +str(exc)+'. For ready, blockers must be []. Move genuinely deferred validation gates '
                               'into constraints without deleting or marking them satisfied. If the next step is genuinely '
                               'blocked, return needs_input instead. Preserve substantive criteria, completed research, '
                               'authorized scope and valid plan entries. If a dispatch ID and field are cited, '
                               'revise those entries and their necessary order or dependency references only. '
                               'Return the complete plan with the other entries carried forward. '
                               'Do not repeat repository research or implementation. '
                               'Previous report:\n'+turn.text)
                    continue
                self._event('requirements_assignment_repair',reason=str(exc),output_file=output_file)
                prompt += ('\n\nReviewer-only repair of the requirement plan. No worker has started for this plan. '
                           'Correct identifier syntax and corresponding references in the previous response. '
                           'Preserve its requirements, acceptance criteria, target files/symbols and approved checks. '
                           +function_tasks.IDENTIFIER_CONTRACT+'Watcher rejection: '+str(exc)
                           +'\nPrevious response:\n'+turn.text)
            except ValueError:
                return turn
            else:
                if isinstance(plan, dict) and plan.get('status') == 'ready':
                    directions = self.directions.messages()
                    if [item['id'] for item in directions] != self._direction_ids['reviewer']:
                        return turn
                    sources = self._coverage_sources(directions)
                    modules = staged_plan[0].export() if staged_plan else self.modules.export()
                    current = self.function_tasks.current
                    revised_environment = (self._can_revise_waiting_verification() and assignment != current
                                           and not (self._referee_replan and self._can_continue_started_assignment(
                                               current, staged_plan[1].current)))
                    coverage_required |= (revised_environment or self._referee_replan
                                          or next_function and self._unplanned_directions(directions))
                    coverage_required |= self._independent_verification(assignment, plan.get('function_plan'))
                    coverage_required |= requirement_coverage.requires_review(
                        sources, modules, self.config.assignment_workspaces)
                    if coverage_required:
                        if next_function and (previous_brief or {}).get('status') == 'ready':
                            previous = previous_brief['acceptance_criteria']
                            if plan['acceptance_criteria'][:len(previous)] != previous:
                                plan['acceptance_criteria'] = list(previous)
                        snapshot = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
                        if snapshot_fingerprint(self.config.cwd, snapshot) != tree:
                            return turn
                        requirement_context.save(self.run_dir, self.config.cwd, self.config.requirement,
                            len(self.requirement_cycles) + 1, plan, snapshot, checks, directions)
                        reviewed_plan, modules, _ = self._coverage_inputs(plan, staged_plan, sources, checks, tree)
                        outcome = self._requirement_coverage_review(
                            sources, reviewed_plan, modules=modules, checks=checks,
                            directions=directions, tree=tree)
                        if outcome is None:
                            return None
                        review, receipt = outcome
                        if review['status'] == 'revise':
                            task = reviewed_plan.get('next_task')
                            if (not revised_environment and self._function_gate and self.config.full_function_plan
                                    and requirement_coverage.independent_dispatch(
                                        review, reviewed_plan, self.function_tasks.accepted)
                                    and not self._assignment_repository_conflict(task)
                                    and not self._missing_dispatch_inputs(task)):
                                self._limited_coverage_dispatch = {'task_id': task['id'], 'receipt': receipt,
                                    'task_sha256': requirement_coverage.digest(task), 'tree': tree,
                                    'checks': requirement_context.checks_digest(checks),
                                    'affected_tasks': review['affected_tasks']}
                                return turn
                            missing = '\n'.join(item['gap'] for item in review['findings'])[:1800]
                            if attempt >= 4 or not journal.admit(recovery_context, missing):
                                raise ValueError('需求覆盖复核仍有缺项，派单尚未开始。' + missing)
                            self._close_requirement_reviewer()
                            self._archive_requirement_review(index)
                            prompt = self._coverage_revision_prompt(base_prompt, review)
                            continue
                try:
                    if isinstance(plan,dict) and plan.get('status') == 'ready':
                        journal.recovered(recovery_context)
                except (OSError, ValueError):
                    pass
                return turn

    def _elaborate_requirement_once(self,records, *, next_function=False):
        self._limited_coverage_dispatch = None
        follow_up = [item['handoff'] for item in self.requirement_cycles]
        review_checks = list(self.config.checks)
        previous_brief = self.requirements_review
        previous_result = self.requirements_review
        if next_function and previous_brief.get('status') != 'ready':
            # Failed planning must not erase the approved criteria on resume.
            # This file is written only after a fully validated planning result.
            saved = self.run_dir / 'reviewed-requirement.json'
            if saved.is_file() and not saved.is_symlink():
                approved = json.loads(saved.read_text(encoding='utf-8'))
                if (isinstance(approved, dict) and approved.get('status') == 'ready'
                        and approved.get('cycle') == len(self.requirement_cycles) + 1
                        and approved.get('original_sha256') == requirements_review.requirement_hash(self.config.requirement)):
                    requirements_review.parse(json.dumps({k:v for k,v in approved.items()
                                                         if k in requirements_review.SCHEMA['required']}))
                    previous_brief = approved
        before_files=file_snapshot(self.config.cwd,ignore_dir=self.run_dir)
        before=snapshot_fingerprint(self.config.cwd,before_files)
        review_index = 910000 + self._next_round if next_function else 900000 + len(self.requirement_cycles) if follow_up else 0
        directions = self.directions.messages()
        continuation = requirement_context.load(self.run_dir,self.config.cwd,self.config.requirement,
            len(self.requirement_cycles)+1,before_files,review_checks,directions,
            previous=previous_result,output_file=f'{review_index:03d}-reviewer-text.md')
        if not continuation and next_function and previous_brief.get('status') == 'ready':
            continuation = {'previous_response':previous_brief,'source':'approved requirement brief',
                            'workspace_changed':True,'changed_files':None,
                            'rejection':self._planning_rejection}
        if continuation:
            self._event('requirements_review_continued',source=continuation['source'],
                        changed_files=continuation.get('changed_file_count'),next_function=next_function)
        self.requirements_review={**(previous_brief if next_function else {}),'status':'reviewing',
                                  'summary':'Reviewer is continuing the saved requirement analysis.' if continuation else
                                  'Reviewer is selecting the next function.' if next_function else
                                  'Reviewer is elaborating the next requirement after local commit.' if follow_up else
                                  'Reviewer is elaborating the original requirement before implementation.',
                                  'cycle':len(self.requirement_cycles)+1}
        self._write_state(records,'running',0,'',before)
        original_schema=self.schema_path
        schema=self.run_dir/'requirements.schema.json'
        plan_schema = function_tasks.schema(requirements_review.SCHEMA, planning=True, gate=self._function_gate,
            integration=bool(self._integration_scopes()), full_plan=self.config.full_function_plan) if self.config.function_dispatch else requirements_review.SCHEMA
        schema.write_text(json.dumps(plan_schema), encoding='utf-8')
        self.schema_path=schema
        self.log('requirements: reviewer continuing saved analysis; checking only pending work and changed inputs' if continuation else
                 'requirements: reviewer elaborating next requirement; worker has not started' if follow_up else
                 'requirements: reviewer elaborating original request; worker has not started')
        workspace_changes = None
        output_file = ''
        review_directions = []
        try:
            if self._stop:
                return False
            latest_directions = self.directions.messages()
            if latest_directions != directions:
                directions = latest_directions
                continuation = requirement_context.load(self.run_dir, self.config.cwd, self.config.requirement,
                    len(self.requirement_cycles)+1, before_files, review_checks, directions,
                    previous=previous_result, output_file=f'{review_index:03d}-reviewer-text.md') or continuation
            saved_revision = self._saved_coverage_revision(continuation, checks=review_checks, tree=before,
                sources=self._coverage_sources(directions), next_function=next_function)
            saved_dispatch = None if saved_revision else self._saved_ready_dispatch(
                continuation, previous_result, index=review_index, tree=before, next_function=next_function)
            if saved_revision:
                continuation = {key: value for key, value in continuation.items() if key != 'previous_response'}
                continuation['response_file'] = str(self.run_dir / 'requirement-review-context.json')
                self._archive_requirement_review(review_index)
            prompt = requirements_review.prompt(
                self.config, self.modules.export(), follow_up=follow_up or None,
                continuation=continuation, queued_requirement=self.active_queued_requirement,
                state_path=self.run_dir / 'state.json', checks=review_checks,
            )
            if not follow_up:
                prompt += migration.planning_context(self.run_dir, self._migration_context)
            if self.config.function_dispatch:
                module_contract = (
                    'This explicitly activated user-queued requirement may append new modules for its new scope. '
                    'Preserve all existing module IDs and definitions; append new IDs rather than rewriting them. '
                    'Prior fixed-module-scope rejection feedback does not override this current authorization. '
                    'Use modules=[] only when the new requirement needs no additional module definitions. '
                    if self._queued_cycle_can_add_modules() else
                    'Declare original modules once in modules (files=[]; no commands); use modules=[] when the ledger is already populated. ')
                prompt += ('\n\n'+function_tasks.IDENTIFIER_CONTRACT+
                           'You own function decomposition. Propose exactly one next_task, normally for a single named function, '
                           'with objective, inputs, outputs, boundaries, focused verification and only already accepted dependencies. '
                           'Use delivery_scope=function for intermediate work, including read-only verification that records standalone evidence. '
                           'Use module or requirement only when accepting the whole module or requirement. '
                           + module_contract +
                           'Distinguish planning input from execution evidence. When the user has defined the scope '
                           'and an approved execution path exists, keep its pending verification in the plan and '
                           'use ready. Missing observations to be produced by that execution do not prevent planning. '
                           'For needs_input use next_task=null. '
                           + (function_plan.planning_contract() if self.config.full_function_plan else 'Do not assign the full backlog. ')+
                           'Accepted function records: ' + json.dumps(self.function_tasks.accepted, ensure_ascii=False))
                if self.config.full_function_plan and self.function_plan and not saved_revision:
                    prompt += '\nPreviously approved function plan (revise remaining entries only with concrete findings):\n'+json.dumps(self.function_plan,ensure_ascii=False)
                if previous_brief.get('limited_dispatch'):
                    prompt += ('\n上一轮仅批准了一个独立派单。根据下列未关闭缺项及原始复核记录重新安排后续任务，'
                               '保留已验收工作及所有验收要求。\n'
                               + json.dumps(previous_brief['limited_dispatch'], ensure_ascii=False))
                if self.config.assignment_contracts:
                    prompt += ('\n以下派单来自原任务。按编号保留原始目标、边界、验收描述和测试源码，'
                               '仅调整本仓库的验收编号、检查编号和依赖关系。\n'
                               + json.dumps(list(self.config.assignment_contracts.values()), ensure_ascii=False))
                if self._function_gate:
                    prompt += self._function_planning_contract()
                    if bindings := check_git_tree.inspect_checks(self.config.cwd, review_checks):
                        prompt += ('\nWatcher 已核对固定检查的 Git 树和测试对象。执行时从 Git 树导出隔离副本，'
                                   '测试在该副本内运行，执行根和结果由正式日志记录。'
                                   'ready 表示候选绑定有效，验收结论由正式执行产生。\n'
                                   + json.dumps(bindings, ensure_ascii=False))
                    browser = browser_guidance(self.config.requirement + '\n' + json.dumps(
                        self.function_tasks.current, ensure_ascii=False))
                    if browser:
                        prompt += '\n\n' + browser
                if next_function:
                    prompt += ('\nMerge newly reviewed amendments into the remaining plan and consolidate overlapping work. '
                               'Preserve the current assignment and all accepted assignments. '
                               'Copy the existing acceptance_criteria as an exact prefix, without paraphrasing or renumbering. '
                               'Append additional criteria only for explicitly requested behavior. '
                               'The watcher retains these approved criteria and validates the new task against them. Brief: '
                               + json.dumps(previous_brief, ensure_ascii=False))
                    if self._referee_replan:
                        prompt += ('\nThe Referee has directed replanning of the remaining work. Its recovery '
                                   'decision supersedes the usual requirement to keep the unfinished assignment identity. '
                                   'Preserve its existing candidate, every accepted assignment and every pending original '
                                   'criterion. Keep the same ID when continuing the exact task. For a read-only task, '
                                   'retain its targets, dependencies, criteria and existing checks while adding approved checks. '
                                   'Use a fresh ID when replacing a started task. Return the complete remaining '
                                   'plan through final verification. Decision:\n' + json.dumps(self._referee_decision, ensure_ascii=False))
                        if any(item['task']['id'] == self._referee_decision.get('task_id')
                               for item in self.function_tasks.accepted):
                            prompt += ('\nWatcher has since accepted the assignment named in the decision. '
                                       'Use its accepted receipt for any matching handoff. Replan only unfinished successors '
                                       'and preserve distinct outstanding checks and original acceptance criteria.')
                    elif self.function_tasks.current:
                        if self._can_revise_waiting_verification():
                            prompt += ('\n若最新用户原文明确替换验收环境或执行方式，允许为当前受阻的只读派单创建新编号。'
                                       '按授权调整验证目标，保留所属模块、交付层级、固定检查和验收条目。'
                                       '也可先安排另一个只读派单，并在队列中原样保留当前外部待办。'
                                       '将仍适用的外部要求安排在相应后续派单中，只让实际依赖它的任务等待。'
                                       '分别核对执行环境、候选版本、获准执行方和可观察结果。'
                                       '独立覆盖复核必须用用户原文核对变更依据，旧回执继续保留。\n')
                        if self._dispatch_preflight_repair():
                            prompt += ('\nWatcher rejected the unstarted assignment at dispatch. Correct the exact validation error. '
                                       'You may repair the target before the worker starts. Keep the assignment ID, module, '
                                       'assignment type, delivery scope, fixed checks and acceptance criteria. '
                                       'Update any references in the remaining plan.\n')
                        conflicts = self._unstarted_assignment_test_conflicts()
                        if conflicts:
                            fixed = [key for key in self.function_tasks.current['check_ids']
                                     if not key.startswith('auto:')]
                            prompt += ('\nThe worker has not started. Preserve the existing project tests at '
                                       + ', '.join(conflicts)
                                       + '. Correct test_files, review_test and check_ids in the current assignment. '
                                       'Update evidence references in verification while keeping its expected behaviors. '
                                       'Retain these approved check IDs: ' + ', '.join(fixed)
                                       + '. Use a new path for any new test. Cite a fixed check that actually runs an '
                                       'existing test, or select a regression ID already listed in the catalog. '
                                       'The presence of a test file does not create a regression ID. Keep all other '
                                       'assignment fields and the rest of the plan unchanged. '
                                       'Keep next_task and the plan first entry equal.')
                        prompt += '\nExact unfinished assignment:\n' + json.dumps(self.function_tasks.current, ensure_ascii=False)
                        if self.function_plan:
                            pending = function_plan.waiting(self.function_plan, self.function_tasks)
                            candidates = [item['id'] for item in pending if self._independent_verification(
                                item, [item, self.function_tasks.current], tree=before)]
                            if candidates:
                                prompt += ('\nWatcher 已核对以下只读派单的依赖、工作区及检查输入。'
                                           '可选择一项交独立复核，并在计划中保留原待验派单：'
                                           + json.dumps(candidates, ensure_ascii=False))
            elif next_function:
                prompt += ('\nReconcile new user amendments and changed inputs with the existing brief. '
                           'Merge overlapping work. Keep existing acceptance criteria in their original order and append new criteria. '
                           'Preserve already accepted work and identify affected behavior that needs fresh evidence. '
                           +json.dumps(previous_brief,ensure_ascii=False))
            if self.project:
                self.project.observe(force=True)
            project_signature = self.project.signature if self.project else ''
            turn = self._invoke_requirement_review(prompt, index=review_index, checks=review_checks,
                next_function=next_function, previous_brief=previous_brief, directions=directions,
                saved_revision=saved_revision, saved_dispatch=saved_dispatch)
            review_directions = [item for item in self.directions.messages()
                                 if item['id'] in self._direction_ids['reviewer']]
            if turn is not None:
                if turn.session_id:
                    self._active_reviewer_id = turn.session_id
                output_file = turn.output_path.with_name(turn.output_path.stem + '-text.md').name
            after_files=file_snapshot(self.config.cwd,ignore_dir=self.run_dir)
            after=snapshot_fingerprint(self.config.cwd,after_files)
            if self.project:
                self.project.observe(force=True)
                if project_signature != self.project.signature:
                    self._project_review_changed = True
                    raise ValueError('Project inputs changed during requirement review; retrying on the latest inputs')
            if after!=before:
                changes = [{'file':str(Path(name).relative_to(self.config.cwd)),
                            'change':'added' if name not in before_files else 'removed' if name not in after_files else 'modified'}
                           for name in sorted(before_files.keys() | after_files.keys())
                           if before_files.get(name)!=after_files.get(name)]
                report = 'requirements-workspace-change.json'
                write_json(self.run_dir/report,safe({'before':before,'after':after,'files':changes,
                    'cycle':len(self.requirement_cycles)+1,'at':datetime.now(timezone.utc).isoformat(),
                    'output_file':output_file,'accepted':False,'source':'unknown'}),durable=True)
                workspace_changes = {'count':len(changes),'files':changes[:50],'report_file':report}
                if self.project:
                    self.project.note_changes([item['file'] for item in changes])
                    self._project_review_changed = True
                preview = ', '.join(item['change']+' '+json.dumps(item['file'],ensure_ascii=False) for item in changes[:10])
                raise ValueError('Workspace changed during requirement review; review was not accepted. '
                                 'Change source is unknown; concurrent writes are possible. Files: '+preview+'. See '+report)
            if self.directions.messages() != directions:
                if turn is not None and not (turn.exit_code or turn.timed_out or turn.interruption):
                    try:
                        requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                            len(self.requirement_cycles)+1,json.loads(turn.text),before_files,review_checks,review_directions)
                    except (ValueError,TypeError):
                        pass
                if output_file:
                    output_file = self._archive_requirement_review(review_index)
                self._requirement_input_changed = True
                raise ValueError('New user directions arrived during requirement review; revalidation required before dispatch')
            if turn is None:
                return False
            if turn.exit_code or turn.timed_out or turn.interruption:
                raise ValueError('Requirement reviewer failed: '+(turn.stderr or turn.interruption or str(turn.exit_code)))
            if self.config.function_dispatch:
                plan = json.loads(turn.text)
                if not isinstance(plan, dict) or not {'next_task','modules'} <= plan.keys():
                    raise ValueError('Reviewer must supply a structured function assignment and module plan')
                response = dict(plan)
                assignment, modules = plan.pop('next_task'), plan.pop('modules')
                backlog = plan.pop('function_plan', None)
                brief = requirements_review.parse(json.dumps(plan))
                requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                    len(self.requirement_cycles)+1,response,before_files,review_checks,review_directions)
                brief['cycle'] = len(self.requirement_cycles) + 1
                if next_function and previous_brief.get('status') == 'ready':
                    previous = previous_brief['acceptance_criteria']
                    if brief['acceptance_criteria'][:len(previous)] != previous:
                        # The reviewer owns the next assignment, not the existing
                        # criterion IDs. Keep canonical text without another model
                        # call; task references and coverage still use this list.
                        brief['acceptance_criteria'] = list(previous)
                        self._event('requirement_criteria_preserved',
                                    reason='Planning response rewrote approved criteria; original IDs and text retained')
                if brief['status']=='ready':
                    staged, dispatch, complete = self._stage_requirement_plan(
                        assignment, modules, backlog, brief, tree=before, next_function=next_function)
                    self.function_tasks = dispatch
                    self.function_plan = complete
                    self._planning_rejection = None
                    self.modules = staged
                    self._dispatch_directions = list(self._direction_ids['reviewer'])
                    self._record_assignment(dispatch.current)
                    self._referee_replan = False
                elif assignment is not None or backlog:
                    raise ValueError('Blocked planning cannot dispatch a function')
            else:
                brief=requirements_review.parse(turn.text)
                if next_function and previous_brief.get('status') == 'ready' and brief['status'] == 'ready':
                    previous = previous_brief['acceptance_criteria']
                    criteria = brief['acceptance_criteria']
                    if criteria[:len(previous)] != previous:
                        brief['acceptance_criteria'] = list(previous)
                requirement_context.save(self.run_dir,self.config.cwd,self.config.requirement,
                    len(self.requirement_cycles)+1,brief,before_files,review_checks,review_directions)
            brief['cycle']=len(self.requirement_cycles) + 1
            brief['session_id']=turn.session_id
            brief['original_sha256']=requirements_review.requirement_hash(self.config.requirement)
            if self._limited_coverage_dispatch:
                brief['limited_dispatch'] = self._limited_coverage_dispatch
                self._event('coverage_independent_dispatch', **self._limited_coverage_dispatch)
            if not self.config.function_dispatch and brief['status'] == 'ready':
                brief['reviewed_direction_sha256'] = self._last_invocation['requirement_revision']['sha256']
                brief['reviewed_checks_sha256'] = requirement_context.checks_digest(review_checks)
            self.requirements_review=brief
            if brief['status'] == 'ready' and not self.config.function_dispatch:
                self._referee_replan = False
            if self.project and brief['status']=='ready':
                self.project.mark_reviewed(project_signature)
                self._project_replan = False
            self.directions.receipt(self._direction_ids['reviewer'],'reviewer',1,responded=True)
            if not self.config.function_dispatch and brief['status'] == 'ready':
                # The run checkpoint is authoritative on resume. Save it before
                # publishing the separate brief so a ready plan is not reviewed twice.
                self._write_state(records, 'running', 0, '', before)
            approved_path = self.run_dir / 'reviewed-requirement.json'
            if brief['status'] == 'ready' or not approved_path.exists():
                write_json(approved_path, safe(brief), durable=True)
            self._event('requirements_reviewed', status=brief['status'], cycle=brief['cycle'],
                        summary=brief.get('summary', ''))
            return brief['status']=='ready'
        except ValueError as exc:
            self.requirements_review={'status':'failed','summary':redact(str(exc)),
                                      'cycle': len(self.requirement_cycles) + 1,
                                      **({'failure_kind': 'model_transport'} if isinstance(exc, _PlanningTransportFailure) else {}),
                                      **({'output_file':output_file} if output_file else {}),
                                      **({'workspace_changes':workspace_changes} if workspace_changes else {})}
            self._event('requirements_reviewed', status='failed', cycle=self.requirements_review['cycle'],
                        summary=self.requirements_review['summary'])
            if (follow_up and not self._stop_requested and not self._project_review_changed
                    and not self._requirement_input_changed):
                self._stop = True
                self._stop_reason = 'Next requirement not ready: ' + self.requirements_review['summary']
            return False
        finally:
            self.schema_path=original_schema
            self._close_requirement_reviewer()
            self.reviewer_session_id=''
            self._write_state(records,'running',0,'',workspace_fingerprint(self.config.cwd,ignore_dir=self.run_dir))
