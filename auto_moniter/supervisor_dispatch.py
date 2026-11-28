"""Validate and advance function assignments and their handoffs."""

from __future__ import annotations

from pathlib import Path
import gzip
import hashlib
import json
import subprocess

from auto_moniter.providers import ConfiguredProvider, ProviderError
from auto_moniter.workspace import LockError, file_snapshot, snapshot_fingerprint, workspace_fingerprint
from auto_moniter.verdict import Verdict
from auto_moniter.modules import ModuleLedger
from auto_moniter.storage import write_json
from auto_moniter.redaction import redact, safe
from auto_moniter import candidate_checks, check_git_tree, function_gate, function_tasks
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter import requirements_review, requirement_context, requirement_coverage
from auto_moniter import function_plan
from auto_moniter import referee
from auto_moniter import assignment_delivery
from auto_moniter.supervisor_models import RefereeRedirect, _failure_reason


class FunctionDispatch:
    """Validate and advance function assignments and their handoffs."""

    def _pending_directions(self) -> bool:
        messages = self.directions.messages()
        if self._deferred_replan_applies(messages):
            messages = [item for item in messages if item['id'] in self._dispatch_directions]
        return any(item['id'] not in self._direction_ids['worker'] for item in messages)

    def _unplanned_directions(self, messages=None) -> bool:
        if messages is None:
            messages = self.directions.messages()
        return (not self._deferred_replan_applies(messages)
                and any(item['id'] not in self._dispatch_directions for item in messages))

    def _replan_binding(self, task, messages):
        return referee.fingerprint({
            'assignment': task, 'decision': self._referee_decision, 'checks': self.config.checks,
            'requirements': self.directions.revision(self.config.requirement, messages)['sha256'],
        })

    def _deferred_replan_applies(self, messages=None):
        task = self.function_tasks.current
        return bool(self._referee_replan and self._deferred_replan and task
                    and self._deferred_replan == self._replan_binding(
                        task, self.directions.messages() if messages is None else messages))

    def _integration_scopes(self):
        return self.config.quality_policy.get('integration_scopes', []) if self._function_gate else []

    def _verification_only(self):
        return bool(self._function_gate and self.function_tasks.current
                    and self.function_tasks.current.get('kind') == 'verification')

    def _function_publication_paths(self):
        if self._function_gate and self.function_tasks.current and isinstance(self.git_history, ApprovedHistory):
            return [] if self._verification_only() else function_tasks.writable_files(self.function_tasks.current)
        return None

    def _local_test_commands(self, paths):
        from auto_moniter.local_tests import command
        return list(dict.fromkeys(value for name in paths
                                 if (value := command(self.config.cwd, name)) is not None))

    def _function_check_catalog(self, task):
        return function_gate.catalog(self.config.checks, self.modules.items, task['module_id'],
                                     root=self.config.cwd, task=task, regressions=self._local_test_paths)

    def _writable_function_tests(self, task):
        protected = self.quality.protected(self.config.cwd)
        authored = (task.get('review_test') or {}).get('path')
        return [name for name in task.get('test_files', [])
                if name != authored and str(self.config.cwd / name) not in protected]

    def _function_test_scope(self, task):
        return ('Watcher scope for this assignment. Editable focused tests: '
                + json.dumps(self._writable_function_tests(task), ensure_ascii=False)
                + '. Focused tests introduced by this assignment remain editable until acceptance, '
                  'including on retry or resume. Fix their fixture or harness defects within this assignment '
                  'and preserve the behavior assertions. Use this scope to resolve conflicting descriptions '
                  'in earlier reviews. All other test files are outside its edit scope. '
                  'For a failure that requires changes outside this scope, use blockers and cite its exact '
                  'test path and existing AUD ID when available. For a test fixture defect, cite the fixture '
                  'file in the finding and name it as the first repair action.')

    def _record_assignment(self, task):
        fields = ({'task_kind': 'integration', 'targets': task['targets'], 'integration_scope': task['integration_scope']}
                  if task.get('kind') == 'integration' else {'file': task['file'], 'symbol': task['symbol']})
        if task.get('kind') == 'verification':
            fields['task_kind'] = 'verification'
        if task.get('edit_files'):
            fields['edit_files'] = task['edit_files']
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
                'When an unfinished read-only function is waiting for field evidence, you may propose an unchanged '
                'read-only function from the previously saved plan as next_task, provided its dependencies and fixed '
                'check inputs are ready. Keep the blocked function unchanged in function_plan after the proposed task. '
                'Watcher checks this exception and requires independent coverage review before dispatch. '
                + ('Fixed checks with a Watcher-configured FRONTEND_ROOT source alias: '
                   + json.dumps(self.config.check_source_aliases, ensure_ascii=False)
                   + '. The alias points to the current workspace. Treat execution environment readiness as the prerequisite; '
                     'the first formal receipt is produced by executing the fixed check after dispatch. '
                   if self.config.check_source_aliases else '')
                +
                'Python symbols are source-local: use a top-level function name or Class.method, not a filename/module prefix. '
                'When implementation is already correct and only module/final acceptance remains, use kind=verification, '
                'delivery_scope=module or requirement, test_files=[] and an existing function; no source edits are required '
                'or allowed. The worker and reviewer inspect read-only, and the watcher runs the fixed checks. '
                'Python, JavaScript and HTML inline JavaScript work without optional packages. '
                'Other registered languages use the optional tree-sitter grammar pack; the monitor checks availability before dispatch. '
                'Include criterion_ids from the assigned module (<module ID>:<one-based acceptance index>) '
                f'and applicable requirement criteria (req:{cycle}:<one-based acceptance_criteria index>). '
                'Use the same indices for new module definitions or a new brief returned in this response. Select check_ids from the '
                'approved catalog; never write an executable command in a task. Include test_files=[] or exact paths '
                'for new focused test files. Preserve tests that predate the assignment and code outside the assigned targets. '
                'For one atomic increment that also needs imports, helper declarations, model properties, resource files, '
                'project registration or docs, list every necessary exact path in edit_files. A listed source path allows '
                'whole-file edits. Use the named function to anchor the change; related declarations can be the entire change '
                'when the function’s own file is explicitly listed. Verify the resulting behavior. '
                'Use edit_files=[] for strict single-function work. Boundaries prose does not grant extra file permissions. '
                'Before dispatch, trace the user-facing entry point, direct callers, helpers and relevant tests for this behavior. '
                'Split independently testable behaviors. For each verification assertion, name the operation and expected observable result. '
                'Include files that must change together in edit_files and describe '
                'the entry point and dependencies in boundaries. Leave unaffected callers outside the write scope. '
                'Existing tests and test configuration remain protected. Do not list directories, patterns or unrelated files. '
                'A scope_repair rejection authorizes reviewer-only correction of edit_files for the SAME unfinished task, '
                'preserving its ID, primary function, tests, criteria and dependencies; do not redo the implementation. '
                'You own acceptance design. For new implementation, supply review_test={path,source} when a local '
                'runner can exercise the assigned function or its public entry point. Put that new path in test_files '
                'and write executable assertions for normal, invalid and boundary inputs tied to the listed criteria. '
                'Use review_test=null when no such runnable case applies. Keep existing review_test unchanged on retry. '
                'Watcher stages the exact test source for the Worker and binds it to the assignment. '
                'Watcher automatically derives and executes local tests declared in test_files, even if not selected. '
                'Supported: Python unittest test_*.py or *_test.py; Node *.js/*.cjs/*.mjs; '
                'Rust Cargo package tests/name.rs or tests/name/main.rs with an existing Cargo.toml; '
                'Swift XCTest files under a test target with an existing shared Xcode scheme, or Swift package Tests with Package.swift. '
                'Use auto:N in check_ids for the Nth test_files entry (one-based); no shell commands or test installation. '
                'For verification-only delivery with test_files=[], select regression:N from previously accepted tests. '
                'Current test files become eligible for regression:N only after this assignment is accepted. '
                'These local tests need no manually supplied check command. The worker implements the feature against '
                'the reviewer-authored test and can provide additional focused regressions within its scope. '
                'Watcher rejects missing, empty or entirely skipped test suites and reruns accepted regressions at delivery. '
                'Select behavioral checks relevant to the target; git diff --check alone is insufficient. '
                'Operator preflights listed in quality_policy.operator_preflights stay outside next_task and function_plan. '
                'Use their candidate-bound evidence as context; only Watcher registers formal acceptance. '
                'For verification assignments and module or requirement delivery, Watcher also runs fixed checks. '
                'Plan independent runnable work ahead of checks with unavailable configured check_inputs. '
                'For page workflows touched by shared filters, tables or navigation, include the affected roles, '
                'entry points and behaviors in final-candidate verification. Record each missing final receipt as pending. '
                'Missing native tools are verification limitations, not a request for the person to invent check IDs. '
                'Plan independent authorized increments first; stop for an unavailable indispensable gate only when no such work remains. '
                'Do not substitute unrelated checks, claim unavailable tests passed or generate executable commands. '
                'Only declare module or requirement delivery once accepted tasks plus the current task map to every '
                'criterion in the delivery scope. Approved check IDs: '
                + json.dumps(function_gate.catalog(self.config.checks, modules), ensure_ascii=False)
                + '\nAccepted regression checks (including current tests only after acceptance): '
                + json.dumps(function_gate.catalog([], {}, root=self.config.cwd,
                    regressions=list(dict.fromkeys([*self._local_test_paths,
                        *(self.function_tasks.current or {}).get('test_files', [])]))), ensure_ascii=False)
                + '\nCriteria by ID: ' + json.dumps({**function_gate.requirement_criteria(self.requirements_review),
                    **{k: v for ident in modules for k, v in function_gate.criteria(modules, ident).items()}}, ensure_ascii=False)
                + '\nOptional language parser availability: ' + json.dumps(language_availability(), ensure_ascii=False)
                + ('\nThe previous next assignment was rejected by the watcher: '
                   + json.dumps(self._planning_rejection, ensure_ascii=False)
                   + '. Correct this specific dispatch problem. Do not repeat accepted implementation or replace approved checks.'
                   if self._planning_rejection else '')
                + integration)

    def _prepare_function_baseline(self, snapshot=None):
        task = self.function_tasks.current
        if task.get('kind') == 'verification':
            from auto_moniter import verification_baseline
            if snapshot is None:
                snapshot = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
            saved, recovered = verification_baseline.prepare(
                self.run_dir, self.config.cwd, task, snapshot,
                cycle=len(self.requirement_cycles) + 1, after_round=self._verification_attempt_boundary(),
                started=self._assignment_worker_started(task), records=self._records)
            if recovered:
                self._event('verification_baseline_recovered', task_id=task['id'],
                            tree=snapshot_fingerprint(self.config.cwd, snapshot))
            return saved
        if self._function_baseline is not None and self._function_baseline['id'] == task['id']:
            return self._function_baseline
        if snapshot is None:
            snapshot = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        self._function_baseline = function_gate.baseline(
            self.run_dir / 'function-baseline.json.gz', self.config.cwd, task, snapshot,
            missing_ok=not self._assignment_worker_started(task), historical_source=self._historical_function_source)
        return self._function_baseline

    def _repair_reviewer_test(self, records, stall, gap_key):
        """Approve the exact revised test without reopening the requirement plan."""
        task = self.function_tasks.current
        test = (task or {}).get('review_test')
        defer = bool(self._referee_replan and task and task['delivery_scope'] == 'function'
                     and (self._referee_decision or {}).get('action') == 'replan')
        if (not self._function_gate or not test or not self._assignment_worker_started(task)
                or self.config.max_rounds and self._next_round > self.config.max_rounds
                or not defer and (self._referee_replan or self._unplanned_directions())
                or self.project and (self.project.needs_review or self._project_replan)
                or task['id'] in (self.config.assignment_contracts or {})
                or self.config.full_function_plan and not self._complete_function_plan_ready()
                or self._assignment_repository_conflict(task) or self._business_head_changed()):
            return ''
        root = self.config.cwd
        path = root / test['path']
        if path.resolve() != path or not path.is_file():
            return ''
        try:
            with path.open('rb') as stream:
                contents = stream.read(function_gate.MAX_SOURCE + 1)
            if contents == test['source'].encode():
                return ''
            return self._review_revised_test(records, task, path, contents, stall, gap_key, defer=defer)
        except (OSError, ValueError) as exc:
            return '当前派单的验收测试未能恢复。' + redact(str(exc))

    def _review_revised_test(self, records, task, path, contents, stall, gap_key, *, defer):
        test = task['review_test']
        if not contents or len(contents) > function_gate.MAX_SOURCE:
            raise ValueError(f'{test["path"]} 的内容为空或超过复核限制。')
        brief = self.requirements_review
        if brief.get('status') != 'ready':
            saved = self.run_dir / 'reviewed-requirement.json'
            brief = requirement_context.read(saved) if saved.is_file() and not saved.is_symlink() else {}
            if brief.get('status') != 'ready':
                brief = self._recover_approved_function_brief(task)
        if (brief.get('status') != 'ready' or brief.get('cycle', 1) != len(self.requirement_cycles) + 1
                or brief.get('original_sha256') != requirements_review.requirement_hash(self.config.requirement)):
            return ''
        requirements_review.parse(json.dumps({key: brief.get(key) for key in requirements_review.SCHEMA['required']}))
        baseline = self._prepare_function_baseline()
        if str(path) in baseline['files']:
            raise ValueError(f'{test["path"]} 是派单前已存在的测试。')
        source = contents.decode('utf-8')
        revised = {**task, 'review_test': {**test, 'source': source}}
        revised = function_tasks.validate(revised, self.modules.items)
        dispatch = function_tasks.FunctionTasks(self.function_tasks.export())
        dispatch.current = revised
        self._validate_function_plan(dispatch, self.modules.items, brief)
        tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
        directions = self.directions.messages()
        if not defer and [item['id'] for item in directions] != self._dispatch_directions:
            return ''
        self._process_control()
        if self._stop:
            return self._stop_reason
        index = 2200000 + self._agent_calls
        schema = self.schema_path
        self.schema_path = self.run_dir / 'reviewer-test-repair.schema.json'
        try:
            properties = {'approved': {'type': 'boolean'}, 'summary': {'type': 'string'}}
            if defer:
                properties['finish_current_first'] = {'type': 'boolean'}
            write_json(self.schema_path, {
                'type': 'object', 'additionalProperties': False,
                'properties': properties, 'required': list(properties),
            })
            prompt = ('只读复核当前派单的验收测试改动。比较锁定源码和当前文件内容，判断改动是否只修正测试准备或模拟逻辑，'
                      '并保留全部原验收场景、有效断言和原预期。只有满足这些条件才填写 approved=true。'
                      '报告具体判断依据。不得编辑文件、修改派单、调整需求计划或提前批准业务候选。\n'
                      + json.dumps({'assignment': task, 'revised_test_source': source}, ensure_ascii=False))
            if defer:
                prompt += ('\n还须核对已保存的裁判决定和所有追加要求。仅当当前派单的目标、范围和验收条件'
                           '均可按原批准方案继续，且裁判未要求先改变或替换当前派单时，填写 finish_current_first=true。'
                           '后续派单的调整会在本项验收后处理。有冲突或无法确认时填写 false。\n'
                           + json.dumps(self._referee_decision, ensure_ascii=False))
            self._event('reviewer_test_repair_started', task_id=task['id'], round=index)
            turn = self._invoke(self.config.reviewer, prompt, '', role='reviewer', index=index,
                model=self._reviewer_model(), extra_args=self.config.reviewer_extra_args,
                read_only=True, direction_snapshot=directions)
            self._active_reviewer_id = turn.session_id
        finally:
            self.schema_path = schema
            self._close_requirement_reviewer()
        self._process_control()
        if self._stop:
            return self._stop_reason
        with self.directions.lock():
            if (self.directions.messages() != directions or self._business_head_changed()
                    or self.project and (self.project.needs_review or self._project_replan)
                    or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree
                    or path.resolve() != path or not path.is_file() or path.read_bytes() != contents):
                raise ValueError('复核期间工作区或用户要求发生变化，请从原任务恢复。')
            if turn.exit_code or turn.interruption or turn.timed_out:
                raise ValueError(f'专项复核未完成，请查看 {turn.output_path.name}。')
            try:
                result = json.loads(turn.text)
            except ValueError:
                result = None
            if (not isinstance(result, dict) or set(result) != properties.keys()
                    or type(result['approved']) is not bool or not isinstance(result['summary'], str)
                    or defer and type(result['finish_current_first']) is not bool
                    or not result['summary'].strip() or len(result['summary']) > 2000):
                raise ValueError(f'专项复核未返回有效结论，请查看 {turn.output_path.name}。')
            if not result['approved']:
                raise ValueError(f'{result["summary"]} 请查看 {turn.output_path.name}。')
            self.function_tasks = dispatch
            if self.function_plan:
                self.function_plan = {**self.function_plan, 'tasks': [
                    revised if item == task else item for item in self.function_plan['tasks']]}
            self.requirements_review = brief
            if defer and result['finish_current_first']:
                self._deferred_replan = self._replan_binding(revised, directions)
            else:
                self._deferred_replan = ''
            self._write_state(records, 'running', stall, gap_key, tree)
            write_json(self.run_dir / 'reviewed-requirement.json', safe(brief), durable=True)
            self._event('reviewer_test_repair_approved', task_id=task['id'], tree=tree,
                        log=turn.output_path.name, summary=result['summary'])
            if defer and not self._deferred_replan:
                self._event('reviewer_test_replan_required', task_id=task['id'], log=turn.output_path.name)
        return ''

    def _recover_approved_function_brief(self, task):
        """Recover older checkpoints whose approved brief was overwritten by a blocked replanning result."""
        if not self._complete_function_plan_ready():
            return {}
        sources = {'original': self.config.requirement, 'acceptance': self.config.acceptance}
        if self.active_queued_requirement:
            sources['queued'] = self.active_queued_requirement['text']
        sources.update({item['id']: item['text'] for item in self.directions.messages()
                        if item['id'] in self._dispatch_directions})
        source_digest = requirement_coverage.digest(sources)
        checks_digest = requirement_context.checks_digest(self.config.checks)
        digests = set()
        for path in self.run_dir.glob('*-requirement-coverage.json'):
            try:
                value = requirement_context.read(path, limit=128 * 1024)
            except (OSError, ValueError, RecursionError):
                continue
            if (isinstance(value, dict) and value.get('status') == 'pass'
                    and value.get('sources_sha256') == source_digest and value.get('checks_sha256') == checks_digest
                    and isinstance(value.get('plan_sha256'), str)):
                digests.add(value['plan_sha256'])
        if not digests:
            return {}
        for path in sorted(self.run_dir.glob('*-reviewer-text.md'), reverse=True):
            try:
                plan = requirement_context.parsed_response(requirement_context.read(path))
                if plan['status'] != 'ready' or plan.get('next_task') != task:
                    continue
                normalized = {**plan, 'function_plan': self.function_plan['tasks']}
                if requirement_coverage.digest(normalized) not in digests:
                    continue
                brief = {key: plan[key] for key in requirements_review.SCHEMA['required']}
                requirements_review.parse(json.dumps(brief))
            except (OSError, ValueError, RecursionError):
                continue
            self._event('approved_function_brief_recovered', task_id=task['id'], output_file=path.name)
            return {**brief, 'cycle': self.function_plan['cycle'],
                    'original_sha256': requirements_review.requirement_hash(self.config.requirement)}
        return {}

    def _verification_attempt_boundary(self):
        return max((item['round'] for item in self.function_tasks.accepted), default=0)

    def _assignment_worker_started(self, task):
        # Verification can be reassigned under the same ID after an accepted
        # repair. Only calls after that repair belong to the new attempt.
        after_round = self._verification_attempt_boundary() if task.get('kind') == 'verification' else 0
        return any(call.get('role') == 'worker' and call.get('task_id') == task['id']
                   and call['round'] > after_round
                   for call in (self._last_worker_invocation, self._last_invocation)) or any(
                       record.role == 'worker' and record.verdict.task_id == task['id']
                       and record.index > after_round for record in self._records)

    def _existing_assignment_tests(self, task):
        if task['test_files'] and self._assignment_repository_conflict(task):
            return []  # The receiving Watcher validates test paths in its workspace.
        return [name for name in task['test_files']
                if (self.config.cwd / name).exists() or (self.config.cwd / name).is_symlink()]

    def _validate_new_assignment_tests(self, task):
        existing = self._existing_assignment_tests(task)
        if existing:
            raise ValueError(task['id'] + '.test_files already exist: ' + ', '.join(existing)
                             + '. Preserve those files and choose unique paths for new tests.')

    def _unstarted_assignment_test_conflicts(self):
        task = self.function_tasks.current
        if not self._function_gate or not task or self._assignment_worker_started(task):
            return []
        path = self.run_dir / 'function-baseline.json.gz'
        if path.exists():
            try:
                with gzip.open(path, 'rt', encoding='utf-8') as stream:
                    if json.load(stream)['id'] == task['id']:
                        return []  # Materialized reviewer tests belong to this baseline.
            except (OSError, ValueError, KeyError, TypeError):
                return []  # Damaged evidence must fail baseline recovery, not be reset.
        return self._existing_assignment_tests(task)

    def _dispatch_preflight_repair(self):
        task = self.function_tasks.current
        problem = self._planning_rejection or {}
        return bool(task and problem.get('dispatch_preflight') and problem.get('task_id') == task['id']
                    and not self._assignment_worker_started(task))

    def _historical_function_source(self, file, digest):
        if not self.git_history:
            return None
        relative = (self.config.cwd.relative_to(self.git_history.root)/file).as_posix()
        refs = [self.git_history.initial, getattr(self.git_history, 'candidate', {}).get('base')]
        for ref in dict.fromkeys(refs):
            if not ref:
                continue
            data = self.git_history.git('show', ref+':'+relative, check=False)
            if len(data) > function_gate.MAX_SOURCE:
                continue
            try:
                source = data.decode('utf-8').replace('\r\n', '\n')
            except UnicodeError:
                continue
            if hashlib.sha256(source.encode()).hexdigest() == digest:
                return source
        return None

    def _validate_function_plan(self, ledger, modules, brief, *, provisional=None):
        task = ledger.current
        if task['id'] in self.config.quality_policy.get('operator_preflights', []):
            raise ValueError(f'{task["id"]} 是管理端预检，不能作为正式派单。')
        original = (self.config.assignment_contracts or {}).get(task['id'])
        if original and not assignment_delivery.same_contract(original, task):
            raise ValueError(f'派单 {task["id"]} 与原任务提供的合同不一致。')
        if task.get('kind') != 'verification':
            root = self.config.cwd
            protected = self.quality.protected(root)
            paths = [root / file for file in function_tasks.writable_files(task)]
            restrictions = [(root / file).resolve() for file in self.config.protect_paths]
            denied = [str(path.relative_to(root)) for path in paths
                      if str(path) in protected
                      or any(path.is_relative_to(restriction) for restriction in restrictions)]
            if denied:
                raise ValueError('Assignment cannot edit protected files: ' + ', '.join(sorted(denied)))
        if task['delivery_scope'] == 'module':
            accepted = {key for key, item in modules.items() if item['status'] == 'accepted'}
            if provisional and provisional['delivery_scope'] in ('module', 'requirement'):
                accepted.add(provisional['module_id'])
            if task.get('kind') != 'verification':
                impact = ModuleLedger.restore(list(modules.values()))
                accepted.difference_update(impact.affected(function_tasks.writable_files(task)))
            missing = set(modules[task['module_id']].get('depends_on', [])) - accepted
            if missing:
                raise ValueError('Complete prerequisite module acceptance before module delivery: '
                                 + ', '.join(sorted(missing)))
        if self.project and self.project.scope:
            from auto_moniter.project_workspace import contains
            files = function_tasks.writable_files(task)
            if any(not contains(self.project.scope,file) for file in files):
                raise ValueError('Parallel assignment targets must stay inside the declared task scope')
        foreign = bool(self._assignment_repository_conflict(task))
        function_gate.validate_task(task, self.config.cwd, modules, self.config.checks, brief,
                                    approvals=self._integration_scopes(),
                                    future=foreign,
                                    regressions=list(dict.fromkeys([
                                        *self._local_test_paths,
                                        *(name for item in ledger.accepted if foreign or 'external_delivery' not in item
                                          for name in item['task'].get('test_files', [])),
                                    ])))
        if not foreign:
            if task.get('publication_check_ids'):
                if not isinstance(self.git_history, ApprovedHistory) or not self.git_history.business_commits:
                    raise ValueError('实际发布树检查需要启用 Git 发布记录。')
                if task.get('kind') == 'verification':
                    head = self.git_history.business_head['commit']
                    if not head:
                        raise ValueError('只读发布树检查需要已登记的业务版本。')
                    try:
                        tree = self.git_history.git('rev-parse', head + '^{tree}').decode().strip()
                        self._publication_check_plan(task, tree)
                    except (OSError, ValueError, subprocess.SubprocessError, KeyError) as exc:
                        raise ValueError('当前发布树检查无法派发。' + str(exc)) from exc
            commands = None
            if task['delivery_scope'] == 'function' and task.get('kind') != 'verification':
                commands = {command for index, command in enumerate(self.config.checks, 1)
                            if f'check:{index}' in task['check_ids']}
            for report in check_git_tree.inspect_checks(self.config.cwd, self.config.checks, commands):
                if report['status'] == 'blocked':
                    raise ValueError(f"固定检查 {report['check_id']} 的候选树无法绑定。{report['reason']}")
        if task['delivery_scope'] != 'function':
            missing = function_gate.coverage(ledger, modules, task, brief, self.config.cwd,
                                              provisional=provisional)
            if missing:
                raise ValueError('Assign remaining implementation before declaring delivery; uncovered criteria: ' + ', '.join(missing))

    def _assignment_repository_conflict(self, task):
        root = self.config.cwd
        from auto_moniter.project_workspace import assignment_workspaces
        bindings = assignment_workspaces(self.config.assignment_workspaces or [])
        for file in function_tasks.writable_files(task):
            path = Path(file)
            match = next((workspace for prefix, workspace in bindings if path.is_relative_to(prefix)), None)
            if match is not None and match != root:
                return self._assignment_repository_message(task, file, match)
        # The same relative path in an unrelated checkout does not establish ownership.
        return ''

    def _assignment_repository_message(self, task, file, workspace):
        return (f'派单 {task["id"]} 的目标文件 {file} 位于 {workspace}。'
                f'当前工作区是 {self.config.cwd}。请在目标仓库安排修复和验收。')

    def _receive_external_assignment(self, task, records):
        from auto_moniter import assignment_delivery
        from auto_moniter.project_workspace import assignment_workspaces
        run = assignment_delivery.links(self.config.assignment_deliveries or []).get(task['id'])
        bindings = assignment_workspaces(self.config.assignment_workspaces or [])
        roots = {next((root for prefix, root in bindings if Path(file).is_relative_to(prefix)),
                      self.config.cwd) for file, _ in function_tasks.targets(task)}
        if len(roots) != 1 or self.config.cwd in roots or task['delivery_scope'] == 'requirement':
            return None
        workspace = roots.pop()
        module = self.modules.items[task['module_id']]
        criteria = function_gate.known_criteria({module['id']: module}, self.requirements_review)
        handoff = {'task': task, 'workspace': str(workspace), 'run_dir': str(run) if run else '',
                   'module': {key: module[key] for key in ('id', 'title', 'acceptance')},
                   'criteria': {key: criteria[key] for key in task['criterion_ids'] if key in criteria},
                   'requirement_cycle': len(self.requirement_cycles) + 1}
        write_json(self.run_dir / f'assignment-handoff-{task["id"]}.json', handoff, durable=True)
        if not run:
            return None
        if run == self.run_dir or self._unplanned_directions():
            return '跨仓派单需要关联另一个任务，并使用最新的需求版本。'
        try:
            proof = assignment_delivery.verified(run, workspace, task, module, self.requirements_review)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return f'派单 {task["id"]} 的跨仓验收记录未通过核对。{exc}'
        if proof is None:
            return f'派单 {task["id"]} 正在等待 {workspace} 的关联任务完成验收。'
        index = max(1, self._next_round - 1)
        evidence = [str(run / 'report.json'), proof['commit']]
        staged = self.function_tasks.export()
        staged['accepted'].append({'task': task, 'round': index, 'tree': proof['tree'],
                                  'evidence': evidence, 'external_delivery': proof})
        staged['current'] = None
        if task['delivery_scope'] == 'module':
            verdict = Verdict('done', scope='module', module_ids=[task['module_id']], evidence=evidence)
            problems = self.modules.coverage(verdict, verdict)
            if problems and self.modules.items[task['module_id']]['status'] != 'accepted':
                return '跨仓模块验收仍缺少前置模块。' + ' '.join(problems)
        ledger = function_tasks.FunctionTasks(staged)
        if task['delivery_scope'] == 'module':
            self.modules.accept(verdict, verdict, index, workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir))
        if self._complete_function_plan_ready():
            waiting = function_plan.waiting(self.function_plan, ledger)
            if waiting:
                next_task = waiting[0]
                try:
                    ledger.assign(next_task, self.modules.items)
                    self._validate_function_plan(ledger, self.modules.items, self.requirements_review)
                    self._validate_new_assignment_tests(ledger.current)
                except ValueError as exc:
                    ledger.current = None
                    self._planning_rejection = {'task_id': next_task['id'], 'reason': str(exc)[:2000]}
                else:
                    self._planning_rejection = None
                    self._record_assignment(ledger.current)
        self.function_tasks = ledger
        self._drop_worker_session = True
        self._event('external_assignment_accepted', task_id=task['id'], workspace=workspace,
                    commit=proof['commit'], checks_digest=proof['checks_digest'])
        self._write_state(records, 'running', 0, '', workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir))
        return ''

    def _external_delivery_problem(self):
        from auto_moniter import assignment_delivery
        for item in self.function_tasks.accepted:
            saved = item.get('external_delivery')
            if not saved:
                continue
            task = item['task']
            try:
                proof = assignment_delivery.verified(Path(saved['run_dir']), Path(saved['workspace']), task,
                    self.modules.items[task['module_id']], self.requirements_review)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                return f'派单 {task["id"]} 的跨仓验收记录需要重新核对。{exc}'
            if proof != saved:
                return f'派单 {task["id"]} 的跨仓验收版本已变化，需要重新核对。'
        return ''

    def _external_assignment_running(self, task):
        from auto_moniter.assignment_delivery import links
        from auto_moniter.health import run_health
        run = links(self.config.assignment_deliveries or []).get(task['id'])
        if run is None or run == self.run_dir:
            return False
        return run_health(run)['health'] in {'active', 'recovering'}

    @staticmethod
    def _field_wait_message(review):
        return '验收缺少现场条件。' + '；'.join(review.blockers)

    def _recover_check_archive(self, records, worker, review):
        from auto_moniter import field_handoff

        if not self.config.referee or self._referee_replan or not self._complete_function_plan_ready():
            return False
        handoff = field_handoff.read(self.run_dir)
        item = field_handoff.archive_item(handoff)
        receipt = worker.watcher_result.get('candidate_checks', {})
        if (not item or not receipt.get('passed') or not receipt.get('checks')
                or not field_handoff.matches_checkpoint(handoff, task=self.function_tasks.current,
                    round_index=worker.index, tree=worker.tree, checks_digest=receipt['digest'],
                    directions=self._dispatch_directions)
                or not candidate_checks.valid(receipt, self.run_dir, receipt.get('context'))):
            return False
        request = {'binding': handoff['binding'], 'revision': handoff['revision'], 'item': item}
        return bool(self._consult_referee('field_archive', self._field_wait_message(review), records,
                                          candidate=worker, archive=request))

    def _waiting_field_review(self, records):
        from auto_moniter import field_handoff

        task = self.function_tasks.current
        if (not self._verification_only() or not self._complete_function_plan_ready()
                or self._pending_directions() or self._unplanned_directions()):
            return None, None
        worker = next((row for row in reversed(records) if row.role == 'worker'
                       and row.verdict.task_id == task['id']), None)
        if not worker or worker.tree != workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir):
            return None, None
        if worker.watcher_result.get('referee_route', {}).get('action') == 'run_git_tree_checks':
            return None, None
        review = next((row for row in reversed(records) if row.role == 'reviewer' and row.index == worker.index), None)
        if not review or review.watcher_result.get('status') != 'blocked' or not review.verdict.blockers:
            return None, None
        receipt = worker.watcher_result.get('candidate_checks', {})
        saved_tree = None
        if isinstance(self.git_history, ApprovedHistory) and self.git_history.business_commits:
            saved_tree = receipt.get('context', {}).get('publication_tree')
        # Rebuild the original check commands before deciding whether to recreate
        # the preview. The recreated preview is validated again before review.
        context = self._candidate_check_context(worker, saved_publication_tree=saved_tree)
        if not candidate_checks.valid(receipt, self.run_dir, context):
            return None, None
        binding = {'task_id': task['id'], 'task_sha256': candidate_checks.digest(task),
                   'round': worker.index, 'tree': worker.tree, 'checks_digest': receipt['digest'],
                   'configured_checks': candidate_checks.digest(self.config.checks),
                   'directions': self._dispatch_directions}
        with self.directions.lock():
            handoff = field_handoff.activate(self.run_dir, binding)
        if not handoff:
            return None, None
        if not (field_handoff.ready(handoff) or field_handoff.pending_fallback(handoff)):
            if not self._recover_check_archive(records, worker, review.verdict):
                return None, review
        if isinstance(self.git_history, ApprovedHistory) and self.git_history.candidate.get('round') != worker.index:
            base = self.git_history.begin_round(worker.index)
            self.git_history.prepare_review(worker.index, base, publication_paths=self._function_publication_paths())
            if not candidate_checks.valid(receipt, self.run_dir, self._candidate_check_context(worker)):
                return None, None
        return worker, None

    def _continue_verification_planning(self, worker, review, records, *, reason=''):
        """Keep local review and scheduling work out of the external wait path."""
        amended = self._unplanned_directions()
        if not amended and not reason:
            return False
        reasons = list(review.verdict.blockers)
        if amended:
            reasons.append('收到补充需求，须按最新要求复核派单并重新绑定检查。')
        review.watcher_result.update(status='blocked', reasons=reasons)
        self._write_state(records, 'running', 0, '', worker.tree)
        if amended:
            self._event('verification_replanning', task_id=worker.verdict.task_id, round=worker.index)
            return True
        self._event('supplemental_checks_rejected', task_id=worker.verdict.task_id,
                    round=worker.index, reason=reason)
        on_tree = review.verdict.git_tree_checks or any(item.get('git_tree') for item in review.verdict.supplemental_checks)
        kind = 'verification_evidence' if on_tree else 'planning'
        return self._consult_referee(kind, reason, records, candidate=worker) in {
            'replan', 'retry_reviewer', 'run_git_tree_checks'}

    def _route_verification_checks(self, worker, review_record, records):
        """Resolve local work before opening a handoff for external evidence."""
        review_record.watcher_result.pop('pending_local_checks', None)
        review = review_record.verdict
        if self._continue_verification_planning(worker, review_record, records):
            return True, ''
        if review.supplemental_checks or review.git_tree_checks:
            try:
                self._queue_supplemental_checks(worker, review)
            except ValueError as exc:
                reason = str(exc)
                review.blockers.append(reason)
                if self._continue_verification_planning(worker, review_record, records, reason=reason):
                    return True, ''
                on_tree = review.git_tree_checks or any(item.get('git_tree') for item in review.supplemental_checks)
                return False, 'Git 候选补验请求未通过校验。' + reason if on_tree else ''
            review_record.watcher_result.update(status='blocked', reasons=['已安排 Watcher 补验。'])
            self._write_state(records, 'running', 0, '', worker.tree)
            return True, ''
        if self._separate_verification_milestone(records):
            return True, ''
        if (not review.field_handoffs and self.config.referee and review.blockers
                and self._git_tree_check_options(worker)):
            review_record.watcher_result.update(status='blocked', reasons=list(review.blockers))
            self._write_state(records, 'running', 0, '', worker.tree)
            action = self._consult_referee('verification_evidence', '；'.join(review.blockers), records, candidate=worker)
            if action in {'run_git_tree_checks', 'retry_reviewer', 'replan'}:
                return True, ''
            if review.repair_steps is not None:
                return False, '本轮尚无可执行的补验或现场交接。' + '；'.join(review.blockers)
        return False, ''

    def _resume_local_check_handoff(self, worker, records):
        review = next((row for row in reversed(records) if row.role == 'reviewer' and row.index == worker.index), None)
        if not review or not (binding := review.watcher_result.get('pending_local_checks')):
            return
        review.watcher_result.pop('pending_local_checks')
        receipt = worker.watcher_result.get('candidate_checks', {})
        if (binding == receipt.get('digest') and (review.verdict.supplemental_checks or review.verdict.git_tree_checks)
                and not self._function_reviewer_report_errors(review.verdict, worker)):
            try:
                self._queue_supplemental_checks(worker, review.verdict)
            except ValueError as exc:
                self._event('supplemental_checks_rejected', round=worker.index, reason=str(exc))
            else:
                review.watcher_result.update(status='blocked', reasons=['已恢复待执行的 Watcher 补验。'])
                self._event('supplemental_checks_resumed', round=worker.index, task_id=worker.verdict.task_id)
        self._write_state(records, 'running', 0, '', worker.tree)

    def _apply_verification_repair(self, worker, review_record, records):
        review = review_record.verdict
        previous_id = self.function_tasks.current['id']
        staged = self._stage_verification_repair(review)
        if self.config.full_function_plan and self._complete_function_plan_ready():
            remaining = function_plan.waiting(self.function_plan, staged)
            if staged.current['delivery_scope'] == 'requirement':
                remaining = [item for item in remaining if item['id'] != previous_id]
            try:
                self.function_plan = self._validate_complete_function_plan(
                    [staged.current, *remaining], staged, self.modules.items, self.requirements_review)
            except ValueError as exc:
                self._planning_rejection = {'task_id': staged.current['id'], 'reason': str(exc)[:2000]}
            else:
                self._planning_rejection = None
        self.function_tasks = staged
        self._dispatch_directions = list(self._direction_ids['reviewer'])
        self._save_worker_handoff(worker, review_record, staged.current)
        review_record.watcher_result.pop('pending_verification_handoff', None)
        self._record_assignment(staged.current)
        self._event('verification_repair_assigned', verification_task=previous_id, task_id=staged.current['id'])
        self._write_state(records, 'running', 0, '', worker.tree)

    def _write_field_handoff(self, worker, review_record, records, *, reopen=False, handoff=None):
        from auto_moniter import field_handoff

        review = review_record.verdict
        review_record.watcher_result.update(status='blocked', reasons=list(review.blockers))
        self._write_state(records, 'running', 0, '', worker.tree)
        with self.directions.lock():
            state = requirement_context.read(self.run_dir / 'state.json', limit=32 * 1024 * 1024)
            binding = field_handoff.binding(state)
            if reopen and handoff and handoff['binding'] == binding:
                field_handoff.reopen(self.run_dir, handoff, review.blockers, review.field_handoffs)
            elif field_handoff.read(self.run_dir).get('binding') != binding:
                field_handoff.configure(self.run_dir, state, review.field_handoffs or [
                    {'kind': 'observation', 'title': blocker[:300], 'owner': '任务负责人'}
                    for blocker in review.blockers[:12]
                ], automatic=True)
        if review_record.watcher_result.pop('pending_verification_handoff', None):
            self._write_state(records, 'running', 0, '', worker.tree)

    def _resume_verification_handoff(self, worker, records):
        review = next((row for row in reversed(records) if row.role == 'reviewer' and row.index == worker.index), None)
        if not review or not (pending := review.watcher_result.get('pending_verification_handoff')):
            return False
        receipt = worker.watcher_result.get('candidate_checks')
        context = self._candidate_check_context(worker)
        if (not self._verification_only() or self._pending_directions() or self._unplanned_directions()
                or self._business_head_changed() or not candidate_checks.valid(receipt, self.run_dir, context)
                or pending['checks_digest'] != receipt['digest']
                or self._function_reviewer_report_errors(review.verdict, worker)):
            review.watcher_result.pop('pending_verification_handoff', None)
            return False
        try:
            self._direction_ids['reviewer'] = list(context['direction_ids'])
            if review.verdict.next_task:
                self._apply_verification_repair(worker, review, records)
            else:
                self._write_field_handoff(worker, review, records, reopen=pending.get('reopen', False),
                                          handoff=self._field_handoff_at_revision(pending.get('revision', '')))
        except (OSError, ValueError, KeyError) as exc:
            review.watcher_result.pop('pending_verification_handoff', None)
            self._event('verification_handoff_rejected', round=worker.index, reason=str(exc))
            return False
        self._event('verification_handoff_resumed', round=worker.index, task_id=review.verdict.task_id)
        return True

    def _field_handoff_at_revision(self, revision):
        from auto_moniter import field_handoff

        value = field_handoff.read(self.run_dir)
        return value if value.get('revision') == revision else {}

    def _ready_deferred_tasks(self, tree):
        from auto_moniter import field_handoff

        ready = [value for _, value in field_handoff.saved_handoffs(self.run_dir) if field_handoff.ready(value)]
        if not ready:
            return set()
        state = self.directions.read('state.json', 32 * 1024 * 1024)
        return {value['binding']['task_id'] for value in ready
                if field_handoff.deferred_state(self.run_dir, state, value, tree=tree)}

    def _advance_local_assignment(self, records, *, missing_inputs=False, field_waiting=False):
        if (self.requirements_review.get('limited_dispatch') or not self._complete_function_plan_ready()
                or self._unplanned_directions()):
            return False
        blocked = self.function_tasks.current
        snapshot = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        tree = snapshot_fingerprint(self.config.cwd, snapshot)
        waiting_ids = set()
        if field_waiting:
            if not self._verification_only() or blocked['delivery_scope'] == 'requirement':
                return False
            waiting_ids = {row.verdict.task_id for row in records
                           if row.role == 'reviewer' and row.tree == tree
                           and row.watcher_result.get('status') == 'blocked'}
        if self._assignment_worker_started(blocked):
            try:
                if self._prepare_function_baseline(snapshot)['files'] != snapshot:
                    return False
            except (OSError, ValueError):
                return False
        waiting = function_plan.waiting(self.function_plan, self.function_tasks)
        ready = self._ready_deferred_tasks(tree)
        for task in sorted(waiting, key=lambda item: item['id'] not in ready):
            if task['delivery_scope'] == 'requirement' or self._assignment_repository_conflict(task):
                continue
            if field_waiting and (task.get('kind') != 'verification' or task['delivery_scope'] != 'function'
                                  or task['id'] in waiting_ids and task['id'] not in ready):
                continue
            staged = function_tasks.FunctionTasks(self.function_tasks.export())
            staged.current = None
            try:
                staged.assign(task, self.modules.items)
                self._validate_function_plan(staged, self.modules.items, self.requirements_review)
                self._validate_new_assignment_tests(task)
                if self._missing_dispatch_inputs(task):
                    continue
                plan = self._validate_complete_function_plan(
                    [task, blocked, *(item for item in waiting if item['id'] != task['id'])],
                    staged, self.modules.items, self.requirements_review)
            except ValueError:
                continue
            self.function_tasks, self.function_plan = staged, plan
            self._drop_worker_session = True
            self._record_assignment(task)
            event = ('function_field_handoff_deferred' if field_waiting else
                     'function_check_inputs_deferred' if missing_inputs else 'function_workspace_deferred')
            self._event(event, task_id=blocked['id'], next_task_id=task['id'])
            self._write_state(records, 'running', 0, '', tree)
            return True
        return False

    def _complete_function_plan_ready(self):
        return bool(self.function_plan and self.function_plan['cycle'] == len(self.requirement_cycles) + 1
                    and self.function_tasks.current in self.function_plan['tasks'])

    def _separate_verification_milestone(self, records):
        """Keep an independently reviewed check separate from a later module delivery."""
        task = self.function_tasks.current
        if not (self._verification_only() and task['delivery_scope'] == 'module'
                and self._complete_function_plan_ready() and not self._pending_directions()):
            return False
        last = records[-1] if records else None
        if not (last and last.role == 'reviewer' and last.verdict.task_id == task['id']
                and last.verdict.task_status == 'changes_required'
                and last.verdict.blockers and not last.verdict.next_task):
            return False
        worker = next((record for record in reversed(records)
                       if record.role == 'worker' and record.index == last.index), None)
        receipt = worker.watcher_result.get('candidate_checks') if worker else None
        if not (receipt and receipt.get('passed') and worker.tree == last.tree
                and candidate_checks.valid(receipt, self.run_dir, self._candidate_check_context(worker))):
            return False
        remaining = function_plan.waiting(self.function_plan, self.function_tasks)
        if not any(item['module_id'] == task['module_id'] and item['delivery_scope'] == 'module'
                   for item in remaining):
            return False
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != last.tree:
            return False
        revised = {**task, 'delivery_scope': 'function'}
        staged = function_tasks.FunctionTasks(self.function_tasks.export())
        staged.current = None
        staged.assign(revised, self.modules.items)
        try:
            self._validate_function_plan(staged, self.modules.items, self.requirements_review)
            plan = self._validate_complete_function_plan(
                [revised, *remaining], staged, self.modules.items, self.requirements_review)
        except ValueError as exc:
            self._event('verification_scope_separation_rejected', task_id=task['id'], reason=str(exc))
            return False
        self.function_tasks, self.function_plan = staged, plan
        self._event('verification_scope_separated', task_id=task['id'], tree=last.tree)
        self._write_state(records, 'running', 0, '', last.tree)
        return True

    def _validate_complete_function_plan(self, backlog, ledger, modules, brief):
        impact = ModuleLedger.restore(list(modules.values()))
        preserved = self.function_tasks.current or {}
        plan = function_plan.validate(backlog, ledger, modules, brief, head=ledger.current, impact=impact,
                                      preserved_id=preserved.get('id'))
        regressions = list(dict.fromkeys([
            *self._local_test_paths,
            *(name for item in ledger.accepted if 'external_delivery' not in item
              for name in item['task'].get('test_files', [])),
        ]))
        remote_regressions = [name for item in ledger.accepted if 'external_delivery' in item
                              for name in item['task'].get('test_files', [])]
        problems = []
        planned = {item['task']['id']: item['task'] for item in ledger.accepted}
        planned.update((task['id'], task) for task in plan['tasks'])
        for ident, original in (self.config.assignment_contracts or {}).items():
            if ident not in planned or not assignment_delivery.same_contract(original, planned[ident]):
                problems.append(f'派单计划须保留原任务提供的合同 {ident}。')
        accepted_modules = {key for key, item in modules.items() if item['status'] == 'accepted'}
        for task in plan['tasks']:
            if task['id'] in self.config.quality_policy.get('operator_preflights', []):
                problems.append(f'{task["id"]} 是管理端预检，不能进入正式派单计划。')
            if task.get('kind') != 'verification':
                accepted_modules.difference_update(impact.affected(function_tasks.writable_files(task)))
            if task['delivery_scope'] == 'module':
                missing = set(modules[task['module_id']].get('depends_on', [])) - accepted_modules
                if missing:
                    problems.append(task['id'] + ': Complete prerequisite module acceptance before module delivery: '
                                    + ', '.join(sorted(missing)))
                accepted_modules.add(task['module_id'])
            if self.project and self.project.scope:
                from auto_moniter.project_workspace import contains
                paths = function_tasks.writable_files(task)
                if any(not contains(self.project.scope, name) for name in paths):
                    problems.append('Complete plan must stay inside the declared parallel task scope: '+task['id'])
            if self._function_gate:
                foreign = bool(self._assignment_repository_conflict(task))
                catalog = list(dict.fromkeys([*regressions, *remote_regressions])) if foreign else regressions
                try:
                    function_gate.validate_task(task, self.config.cwd, modules, self.config.checks, brief,
                        approvals=self._integration_scopes(), regressions=catalog, future=True)
                except ValueError as error:
                    problems.append(task['id']+': '+str(error))
                if foreign:
                    remote_regressions.extend(task.get('test_files', []))
                else:
                    regressions = list(dict.fromkeys([*regressions, *task.get('test_files', [])]))
        if problems:
            raise ValueError('; '.join(problems)[:12000])
        return plan

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

    def _finish_function_acceptance(self, record, records, reasons):
        """A watcher rejection is not a new implementation assignment."""
        from auto_moniter import dispatch_inputs
        if self.directions.read(dispatch_inputs.WAIT_FILE).get('summary') == self._stop_reason and self._stop_reason:
            record.watcher_result.update(status='blocked', reasons=[self._stop_reason], function_gate='checks')
            return self._finish('action_required', self._stop_reason, records, record.verdict)
        reasons = list(dict.fromkeys(reasons or ['Function acceptance did not complete']))
        worker = next((item for item in reversed(records) if item.role == 'worker'), None)
        if self._consult_referee('candidate_checks', '; '.join(reasons), records, candidate=worker):
            raise RefereeRedirect('Referee recovery decision saved')
        record.watcher_result.update(status='blocked', reasons=reasons, function_gate='checks')
        record.delivery = {'status': 'rejected', 'reasons': reasons}
        self._event('function_acceptance_blocked', task_id=record.verdict.task_id, reasons=reasons)
        if self._stop_requested:
            return self._finish('stopped', 'Stopped before publishing the approved candidate: '+self._stop_reason,
                                records, record.verdict)
        if self._stop and 'budget' in self._stop_reason.lower():
            return self._finish('budget_exceeded', 'Stopped before publication: '+self._stop_reason,
                                records, record.verdict)
        stopped = self._stop_requested or self._stop and not self._stop_reason.startswith(('Acceptance environment', 'Referee'))
        return self._finish('stopped' if stopped else 'action_required',
            ('Function acceptance blocked for ' + record.verdict.task_id if self.config.function_dispatch
             else 'Candidate acceptance blocked for round '+str(record.index)) +
            '; candidate preserved without another worker turn. Inspect the failed checks and arrange '
            'a bounded repair before resuming. Approval is reusable only for the same candidate and check receipt; '
            'changed check results require fresh review. '
            + '; '.join(reasons), records, record.verdict)

    def _validate_function_worker(self, verdict):
        task = self.function_tasks.current
        expected = task['delivery_scope']
        delivery_modules = list(self.modules.items) if expected == 'requirement' else [task['module_id']]
        complete_modules = (len(verdict.module_ids) == len(delivery_modules)
                            and set(verdict.module_ids) == set(delivery_modules))
        errors = []
        for invalid, reason in [
            (verdict.parse_error, 'Invalid structured report: ' + verdict.parse_error),
            (verdict.task_id != task['id'], 'task_id must equal ' + task['id']),
            (verdict.task_status not in ('done', 'incomplete'), 'task_status must be done or incomplete'),
            (verdict.next_task is not None, 'next_task must be null; only the reviewer assigns the next task'),
            (verdict.supplemental_checks or verdict.git_tree_checks, 'Only the reviewer may request supplemental checks'),
            (verdict.repair_steps, '只有 Reviewer 可以下发返工步骤。'),
            (verdict.field_handoffs, '只有 Reviewer 可以申请现场补证。'),
            (verdict.modules, 'modules must be []; the worker cannot redefine modules'),
            (set(verdict.module_ids) - set(delivery_modules), 'module_ids may contain only ' + ', '.join(delivery_modules)),
            (verdict.status == 'done' and (expected == 'function' or verdict.scope != expected
                or not complete_modules or verdict.task_status != 'done'),
                'Overall status/scope must match delivery_scope=' + expected + '; function completion uses status=continue'),
        ]:
            if invalid:
                errors.append(reason)
        if verdict.task_status == 'done':
            if not verdict.evidence:
                errors.append('task_status=done requires existing completion evidence')
            if expected != 'function' and (verdict.status != 'done' or verdict.scope != expected
                    or not complete_modules):
                errors.append('Module/requirement completion requires status=done, complete delivery evidence and '
                              'module_ids=' + json.dumps(delivery_modules))
        if errors:
            verdict.status = 'continue'
            verdict.task_status = 'incomplete'
            verdict.task_id = task['id']
            verdict.modules = []
            verdict.issue_resolutions = []
            verdict.next_task = None
            verdict.module_ids = []
            verdict.gaps.extend('Function completion report rejected: ' + error for error in errors)
        return errors

    def _apply_function_assessment(self, worker_record, review_record):
        """Use independent acceptance without rewriting the captured worker report."""
        worker, review = worker_record.verdict, review_record.verdict
        if not (review.task_status == 'accepted' and worker.task_status == 'done'):
            return
        if worker.gaps or worker.next_actions or worker.blockers or worker.issue_resolutions:
            worker_record.watcher_result['function_assessment'] = {
                'review_round': review_record.index, 'tree': review_record.tree,
                'original': worker.to_dict(), 'reviewer_evidence': list(review.evidence),
                'note': 'Independent review assessed the worker concerns; original transcript is preserved.'}
            from dataclasses import replace
            worker_record.verdict = replace(worker, gaps=[], next_actions=[], blockers=[], issue_resolutions=[])

    def _repair_function_worker_report(self, record, records):
        """Correct one completion report on the saved candidate, without implementation."""
        report = record.watcher_result['worker_report']
        if report['status'] != 'pending':
            record.failure = 'The bounded function report correction did not succeed; inspect the preserved report and errors.'
            return None
        self._process_control()
        if self._stop:
            return None
        if (self._pending_directions() or report['task'] != self.function_tasks.current
                or workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree):
            report['status'] = 'stale'
            record.failure = 'Inputs changed before function report correction; the candidate requires fresh review.'
            return None
        import copy
        provider = copy.copy(self.config.provider)
        delegate = provider
        while isinstance(delegate, ConfiguredProvider):
            delegate.mode = ''
            delegate.delegate = copy.copy(delegate.delegate)
            delegate = delegate.delegate
        delegate.mode = ''  # Narrow writable modes without changing saved provider settings.
        try:
            provider.validate_review(self.config.extra_args)
        except ProviderError as exc:
            record.failure = 'Function report correction requires a supported read-only worker: ' + str(exc)
            return None
        # Preserve the candidate before a budget interruption can prevent correction.
        saved = json.loads((self.run_dir/'state.json').read_text(encoding='utf-8'))
        self._write_state(records, 'running', saved.get('stall', 0), saved.get('gap_key', ''), record.tree)
        self._check_budget(new_call=True)
        artifact = f'{record.index:03d}-worker-report-original-{self._agent_calls}'
        for suffix in ('.log', '-text.md', '-prompt.md'):
            source = self.run_dir / f'{record.index:03d}-worker{suffix}'
            if source.exists():
                source.replace(self.run_dir/(artifact+suffix))
        report.update(status='attempted', original_output=str(self.run_dir/(artifact+'-text.md')),
                      original_session_id=record.session_id)
        record.output_path = str(self.run_dir/(artifact+'.log'))
        self._write_state(records, 'running', saved.get('stall', 0), saved.get('gap_key', ''), record.tree)
        self._event('worker_report_repair', round=record.index, task_id=self.function_tasks.current['id'],
                    reasons=report['errors'])
        prompt = ('Worker report correction only for the SAME saved candidate. Do not implement, edit files, '
                  'commit, run builds/tests, or repeat repository analysis. Return only the corrected JSON report '
                  'using existing evidence. Watcher checks and independent review happen automatically. '
                  'Keep substantive gaps or missing evidence as incomplete; do not invent validation or suppress defects.\n'
                  + function_tasks.verdict_contract('worker', self.function_tasks.current, report_only=True,
                                                    module_ids=self.modules.items)
                  + '\nAssignment:\n' + json.dumps(self.function_tasks.current, ensure_ascii=False)
                  + '\nWatcher report errors:\n' + json.dumps(report['errors'], ensure_ascii=False)
                  + '\nOriginal worker report:\n' + json.dumps(report['original'], ensure_ascii=False))
        if report.get('review'):
            prompt += ('\nIndependent assessment of the SAME candidate:\n' + json.dumps(report['review'], ensure_ascii=False)
                       + '\nCurrent watcher acceptance catalog (historical command failures may be obsolete):\n'
                       + json.dumps(safe(self._function_check_catalog(self.function_tasks.current)), ensure_ascii=False))
        signature = self.project.signature if self.project else ''
        turn = self._invoke(provider, prompt, '', role='worker', index=record.index,
            model=self.config.model, extra_args=self.config.extra_args, read_only=True, report_only=True)
        self.worker_session_id = ''
        self._drop_worker_session = True
        verdict = self._verdict_from_turn(turn, role='worker')
        failure = _failure_reason(turn, verdict) or turn.interruption or verdict.parse_error
        errors = self._validate_function_worker(verdict)
        record.verdict = verdict
        record.elapsed_s += turn.elapsed_s
        self._worker_seconds_since_review += turn.elapsed_s
        record.output_path = str(turn.output_path)
        record.session_id = turn.session_id
        record.exit_code, record.timed_out = turn.exit_code, turn.timed_out
        record.interruption = turn.interruption
        if (workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != record.tree
                or self._pending_directions() or report['task'] != self.function_tasks.current
                or self.project and (self.project.needs_review or self.project.signature != signature)):
            report['status'] = 'stale'
            record.failure = 'Workspace or instructions changed during read-only function report correction; no approval applied.'
            verdict.status, verdict.task_status = 'continue', 'incomplete'
            return None
        if failure or errors:
            report.update(status='rejected', correction_errors=errors)
            record.failure = 'Function report correction failed: ' + (str(failure) if failure else '; '.join(errors))
            return None
        report['status'] = 'corrected'
        record.failure = ''
        self._write_state(records, 'running', saved.get('stall', 0), saved.get('gap_key', ''), record.tree)
        return turn

    def _stage_verification_repair(self, review):
        if self.requirements_review.get('limited_dispatch'):
            raise ValueError('独立派单的后续修复须重新经过需求覆盖复核。')
        if not (review.gaps or review.blockers) or review.next_task.get('kind') == 'verification':
            raise ValueError('Failed verification requires concrete findings and an implementation assignment')
        staged = function_tasks.FunctionTasks(self.function_tasks.export())
        if review.next_task.get('id') == staged.current['id']:
            raise ValueError('Verification repair must use a new implementation task ID')
        staged.current = None
        staged.assign(review.next_task, self.modules.items)
        if (staged.current['delivery_scope'] == 'requirement'
                and not set(self.function_tasks.current.get('publication_check_ids', []))
                <= set(staged.current.get('publication_check_ids', []))):
            raise ValueError('替代最终验收的修复派单须保留原发布树检查。')
        self._validate_new_assignment_tests(staged.current)
        problems = function_plan.assertion_problems(staged.current)
        if problems:
            raise ValueError(problems[0])
        self._validate_function_plan(staged, self.modules.items, self.requirements_review)
        return staged

    def _function_reviewer_report_errors(self, review, worker=None):
        """Check the report protocol before entering the function handoff."""
        task = self.function_tasks.current
        if not task or review.task_id != task['id'] or review.task_status not in ('accepted', 'changes_required'):
            return ['Reviewer must conclude on the dispatched function ID']
        local_checks = review.supplemental_checks or review.git_tree_checks
        if review.repair_steps:
            if self._verification_only() or review.task_status != 'changes_required' or review.next_task is not None:
                return ['返工步骤只能交给当前有写入权限的实现派单。']
            protected = self.quality.protected(self.config.cwd)
            writable = (set(function_tasks.writable_files(task)) - set(task.get('test_files', []))
                        | set(self._writable_function_tests(task)))
            writable = {path for path in writable if str(self.config.cwd / path) not in protected}
            selected = set(task['check_ids'])
            for step in review.repair_steps:
                if step['criterion_id'] not in task['criterion_ids'] or step['path'] not in writable:
                    return ['返工步骤须引用当前派单的验收条目和获准修改的文件。']
                if (step['check_id'] and step['check_id'] not in selected
                        or step['log'] and not step['check_id']):
                    return ['返工步骤须引用当前派单的检查编号，引用检查日志时须同时提供该编号。']
            if worker is not None and any(step['log'] for step in review.repair_steps):
                receipt = worker.watcher_result.get('candidate_checks')
                if not candidate_checks.valid(receipt, self.run_dir, self._candidate_check_context(worker)):
                    return ['返工步骤引用了当前候选无法核对的检查回执。']
                logs = {row['log']: row.get('check_ids', []) for row in self._candidate_receipt_summary(receipt)['checks']}
                if any(step['log'] and step['check_id'] not in logs.get(step['log'], []) for step in review.repair_steps):
                    return ['返工步骤引用的检查编号与当前候选的日志不对应。']
        if review.supplemental_checks and review.git_tree_checks:
            return ['每份补验结论只能选择按测试路径或按 Git 候选申请。']
        if review.field_handoffs and (task.get('kind') != 'verification'
                or review.task_status != 'changes_required' or not review.blockers
                or review.next_task or local_checks):
            return ['现场交接仅用于等待外部前置条件的只读派单。']
        if local_checks and (not self._verification_only()
                or review.task_status != 'changes_required' or review.next_task is not None):
            return ['Supplemental checks require a read-only verification request without a replacement assignment.']
        if review.task_status == 'changes_required':
            verification = task.get('kind') == 'verification'
            repair = (verification and review.next_task is not None and review.next_task.get('kind') != 'verification')
            if (self._review_approved(review) or (review.next_task is not None and not repair)
                    or not (review.gaps or review.blockers or local_checks or review.repair_steps)):
                return ['Rejected function requires focused gaps and cannot dispatch another task']
            if not verification and not review.blockers and review.repair_steps == []:
                return ['实现派单需要返工时，请在 repair_steps 填写可执行的步骤、文件和预期结果。']
            if verification and not repair and not (review.blockers or local_checks):
                return ['Read-only verification cannot implement the reported fixes. Provide a bounded implementation '
                        'next_task, or identify the external prerequisite in blockers if no repair can run.']
            if (worker is not None and verification and not repair and review.repair_steps is not None
                    and not (local_checks or review.field_handoffs)
                    and not (self.config.referee and self._git_tree_check_options(worker))):
                return ['只读派单须指定补跑的获准检查、具体修复派单或现场执行方及所需证据。']
            if repair:
                try:
                    self._stage_verification_repair(review)
                except ValueError as exc:
                    return [f"Invalid verification repair {review.next_task.get('id', '')}: {exc}"]
            return []
        expected = task['delivery_scope']
        delivery_modules = list(self.modules.items) if expected == 'requirement' else [task['module_id']]
        errors = []
        if review.gaps or review.blockers or review.next_actions:
            errors.append('Accepted function requires gaps=[], blockers=[] and next_actions=[]; '
                          'use changes_required for substantive unfinished work')
        if ((expected == 'function' and self._review_approved(review))
                or expected != 'function' and (not self._review_approved(review) or review.scope != expected
                    or len(review.module_ids) != len(delivery_modules)
                    or set(review.module_ids) != set(delivery_modules))):
            errors.append('Function approval requires delivery_scope=' + expected
                          + ' and module_ids=' + json.dumps(delivery_modules))
        if expected == 'requirement' and review.next_task is not None:
            errors.append('Final delivery cannot include another implementation assignment')
        return errors

    def _save_worker_handoff(self, worker, review_record, task):
        review = review_record.verdict
        review_record.watcher_result['worker_handoff'] = {
            'action': 'repair', 'source_task_id': review.task_id, 'target_task_id': task['id'],
            'task_digest': candidate_checks.digest(task), 'round': worker.index, 'tree': worker.tree,
            'checks_digest': worker.watcher_result.get('candidate_checks', {}).get('digest', ''),
            'revision': self.directions.revision(self.config.requirement)['sha256'],
            'business_update_count': self._business_update_count,
            'check_plan_digest': self._handoff_check_plan_digest(task),
            'steps': review.repair_steps or [],
            **({'findings': review.gaps, 'evidence': review.evidence} if task['id'] != review.task_id else {}),
        }

    def _worker_handoff(self, task, tree):
        latest = next((row for row in reversed(self._records)
                       if row.role == 'reviewer' and (row.verdict.task_id == task['id']
                           or row.watcher_result.get('worker_handoff', {}).get('target_task_id') == task['id'])), None)
        if not latest:
            return {}
        handoff = latest.watcher_result.get('worker_handoff', {})
        if (handoff and handoff['task_digest'] == candidate_checks.digest(task) and handoff['tree'] == tree
                and handoff['business_update_count'] == self._business_update_count
                and handoff['check_plan_digest'] == self._handoff_check_plan_digest(task)
                and handoff['revision'] == self.directions.revision(self.config.requirement)['sha256']):
            return handoff
        return {}

    def _handoff_check_plan_digest(self, task):
        return candidate_checks.digest([self._assignment_check_commands(task), self.config.check_source_aliases])

    def _review_function(self, worker_record, review_record, tree):
        worker, review = worker_record.verdict, review_record.verdict
        task = self.function_tasks.current
        errors = self._function_reviewer_report_errors(review)
        if errors:
            raise ValueError('; '.join(errors))
        if review.task_status == 'changes_required':
            return None
        expected = task['delivery_scope']
        if worker.task_id != task['id']:
            raise ValueError('Function approval must match the submitted assignment')
        if self._pending_directions():
            review.status = 'continue'
            review.task_status = 'changes_required'
            review.gaps.append('New user instructions require reviewer planning before this function can advance.')
            return None
        if workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir) != tree:
            raise ValueError('Function review is stale because the workspace changed')
        staged = function_tasks.FunctionTasks(self.function_tasks.export())
        staged.accept(worker, review, review_record.index, tree)
        if self._function_gate:
            self._stage_function_verification(staged.accepted[-1], task, worker_record, review_record)
        if self.requirements_review.get('limited_dispatch'):
            return staged
        if self._deferred_replan_applies():
            return staged  # Apply the saved replan before selecting a successor.
        if expected == 'requirement' and review.next_task is not None:
            raise ValueError('Final delivery cannot include another implementation assignment')
        candidate = review.next_task
        if self.config.full_function_plan:
            waiting = function_plan.waiting(self.function_plan, staged)
            planned = waiting[0] if waiting else None
            if (self.function_plan is None or candidate is not None and candidate != planned
                    or expected != 'requirement' and planned is None):
                self._planning_rejection = {'task_id': str((candidate or {}).get('id',''))[:120],
                    'reason':'Reviewer must supply an updated complete function plan before the next dispatch'}
                return staged
            if candidate is None:
                ready = self._ready_deferred_tasks(tree)
                candidate = next((item for item in waiting if item['id'] in ready
                                  and set(item['depends_on']) <= {row['task']['id'] for row in staged.accepted}), planned)
        if candidate is not None:
            if self._function_gate:
                try:
                    staged.assign(candidate, self.modules.items)
                    if not self.config.full_function_plan:
                        problems = function_plan.assertion_problems(staged.current)
                        if problems:
                            raise ValueError(problems[0])
                    self._validate_function_plan(staged, self.modules.items, self.requirements_review,
                                                 provisional=task)
                    self._validate_new_assignment_tests(staged.current)
                except ValueError as exc:
                    staged.current = None
                    rejected_id = candidate.get('id', '') if isinstance(candidate, dict) else ''
                    self._planning_rejection = {'task_id': str(rejected_id)[:120], 'reason': str(exc)[:2000]}
                    self._event('function_next_assignment_rejected', task_id=task['id'],
                                rejected_task_id=self._planning_rejection['task_id'], reason=str(exc))
                else:
                    self._planning_rejection = None
            else:
                if candidate.get('kind') in ('integration','verification') or candidate.get('review_test'):
                    raise ValueError('Integration, verification and reviewer-authored tests require the verified function gate')
                staged.assign(candidate, self.modules.items)
        return staged

    def _stage_function_verification(self, accepted, task, worker, reviewer):
        receipt = worker.watcher_result.get('candidate_checks', {})
        replacements = candidate_checks.publication_commands(receipt.get('context', {}))
        logs = receipt.get('passed_commands', {})
        selected = self._function_check_catalog(task)
        local = self._local_test_commands(task.get('test_files', []))
        try:
            accepted['verification'] = {
                key: logs[replacements.get(selected[key], selected[key])] for key in task['check_ids']}
            local_logs = {command: logs[replacements.get(command, command)] for command in local}
            if task.get('kind') == 'integration':
                accepted['target_hashes'] = self._function_target_hashes
            else:
                accepted['symbol_hash'] = self._function_target_hashes[task['file']]
        except KeyError as exc:
            raise ValueError('派单缺少对应的检查回执或源码摘要，尚不能发布。') from exc
        if task.get('edit_files'):
            snapshot = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
            accepted['file_hashes'] = {name: snapshot.get(str(self.config.cwd / name))
                                       for name in task['edit_files']}
        if local_logs:
            reviewer.watcher_result['local_tests'] = local_logs

    def _advance_function(self, staged, review_record):
        task = self.function_tasks.current
        if self._function_gate:
            self._local_test_paths = list(dict.fromkeys([*self._local_test_paths, *task.get('test_files', [])]))
        self.function_tasks = staged
        self._function_issue_ids = []
        if self._deferred_replan:
            self._deferred_replan = ''
        else:
            self._dispatch_directions = list(self._direction_ids['reviewer'])
        review_record.watcher_result['function'] = {'status': 'accepted', 'task_id': task['id'],
                                                     'tree': review_record.tree}
        self._progress_reset |= self.quality.milestone('function:' + task['id'])
        self._event('function_accepted', task_id=task['id'], tree=review_record.tree)
        from auto_moniter.experience import Experience
        try:
            Experience(self.config.cwd,self.run_dir).confirm(task['id'],review_record.tree,
                summary=review_record.verdict.summary,evidence=review_record.verdict.evidence)
        except (OSError, ValueError, LockError) as exc:
            self.log('Verified experience confirmation deferred: ' + redact(str(exc)))
        if staged.current:
            self._record_assignment(staged.current)
