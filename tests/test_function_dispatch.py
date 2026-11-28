import json
import importlib.util
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.delivery_diagnosis import diagnose
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF, approve_requirement_coverage
from tests.test_supervisor import ScriptedProvider, _verdict


def task(ident, file, *, depends=(), scope='function'):
    return {'id':ident,'module_id':'main','file':file,'symbol':'parse_input',
            'objective':'Implement '+ident,'inputs':'Raw input','outputs':'Validated input',
            'boundaries':['Reject malformed values'],'verification':['Valid and invalid inputs produce distinct outcomes'],
            'depends_on':list(depends),'delivery_scope':scope,
            'criterion_ids':['main:1', 'req:1:2' if scope == 'requirement' else 'req:1:1'],
            'check_ids':['check:1'],'test_files':[]}


CHECK = "python3 -c 'from pathlib import Path; assert any(\"def parse_input\" in p.read_text() for p in Path(\".\").glob(\"*.py\"))'"


def verification_task(ident='VERIFY', file='app.py', *, depends=()):
    return {**task(ident,file,scope='requirement',depends=depends),
            'kind':'verification','criterion_ids':['main:1','req:1:1','req:1:2']}


class FunctionWorker(ScriptedProvider):
    def _command(self, session_id, prompt_file, read_only):
        script = self.scripts[0]
        command = super()._command(session_id, prompt_file, read_only)
        script_path = Path(command.argv[-1])
        if 'source' in script or 'writes' in script:
            writes = script['writes'] if 'writes' in script else {script.get('touch', 'app.py'):script['source']}
            append = '\nfrom pathlib import Path\n'
            for name, value in writes.items():
                path = str(prompt_file.parent.parent / name)
                append += (f'Path({path!r}).unlink()\n' if value is None
                           else f'Path({path!r}).write_text({value!r})\n')
            source = script_path.read_text().replace('sys.exit(', append + 'sys.exit(', 1)
            script_path.write_text(source)
        elif script.get('touch'):
            script_path.write_text(script_path.read_text().replace("changed\\n", "def parse_input():\\n    return 42\\n"))
        return command


def result(ident, decision, *, status='continue', file='app.py', next_task=None, gaps=()):
    return {'verdict':{**_verdict(status, gaps=list(gaps), evidence=[file]),
                       'modules':[], 'task_id':ident,'task_status':decision,'next_task':next_task}}


def plan(first):
    return {'verdict':{},'text':json.dumps({**BRIEF,'next_task':first,
            'modules':_verdict('continue')['modules']})}

class DispatchTests(unittest.TestCase):
    def test_waiting_browser_does_not_lock_an_unchanged_read_only_assignment(self):
        from types import SimpleNamespace
        from auto_moniter import field_handoff, candidate_checks
        from auto_moniter.storage import write_json

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            from auto_moniter.function_tasks import validate

            waiting = validate({**verification_task('WAITING'), 'delivery_scope': 'function'})
            independent = validate({**verification_task('LOCAL'), 'delivery_scope': 'function'})
            dependent = {**independent, 'id': 'DEPENDENT', 'depends_on': ['WAITING']}
            monitor = object.__new__(Supervisor)
            monitor.run_dir = run
            monitor.config = SimpleNamespace(full_function_plan=True)
            monitor._function_gate = True
            monitor.function_tasks = SimpleNamespace(current=waiting, accepted=[])
            monitor.function_plan = {'tasks': [waiting, independent, dependent]}
            write_json(run / field_handoff.FILE, {
                'binding': {'task_sha256': candidate_checks.digest(waiting), 'tree': 'same'},
                'items': [{'kind': 'browser'}],
            })
            with (patch.object(monitor, '_complete_function_plan_ready', return_value=True),
                  patch.object(monitor, '_assignment_repository_conflict', return_value=''),
                  patch.object(monitor, '_missing_dispatch_inputs', return_value=[])):
                self.assertTrue(monitor._independent_verification(
                    {**independent, 'review_test': None},
                    [independent, {**waiting, 'review_test': None}], tree='same'))
                self.assertFalse(monitor._independent_verification(dependent, [dependent, waiting], tree='same'))
                self.assertFalse(monitor._independent_verification(independent, [independent], tree='same'))
                self.assertFalse(monitor._independent_verification(independent, [independent, waiting], tree='changed'))

    def run_monitor(self, root, worker, reviewer, **kwargs):
        kwargs.setdefault('checks', [CHECK])
        kwargs.setdefault('review_every', 0)
        kwargs.setdefault('git_history', 'off')
        return Supervisor(RunConfig('ORIGINAL BACKLOG MUST NOT REACH WORKER',root,worker,reviewer=reviewer,
            workflow='review-first',function_dispatch=True,run_dir=root/'run',
            retry_delay_s=0,**kwargs),log=lambda _:None).run()

    def test_coverage_revision_releases_only_the_independently_approved_function(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            initial = {**verification_task('INITIAL'), 'delivery_scope': 'function'}
            blocked = task('BLOCKED', 'other.py', depends=['INITIAL'])
            final = verification_task('FINAL', depends=['BLOCKED'])
            proposal = {**json.loads(plan(initial)['text']), 'function_plan': [initial, blocked, final]}
            coverage = {
                'status': 'revise',
                'summary': '最终路径需要补齐证据',
                'findings': [{'source_id': 'original', 'quote': 'ORIGINAL BACKLOG', 'gap': '缺少后续证据'}],
                'independent_next_task': 'INITIAL',
                'affected_tasks': [{'task_ids': ['BLOCKED'], 'evidence_needed': '构建记录', 'owner': 'Watcher'}],
            }
            worker = FunctionWorker('worker', [result('INITIAL', 'done')])
            blocked_proposal = {**BRIEF, 'status': 'needs_input', 'blockers': ['构建记录未齐'],
                                'next_task': None, 'modules': [], 'function_plan': []}
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(proposal)},
                {'verdict': {}, 'text': json.dumps(coverage)},
                result('INITIAL', 'accepted', next_task=blocked),
                *[{'verdict': {}, 'text': json.dumps(blocked_proposal)} for _ in range(2)],
            ])
            with patch('auto_moniter.requirement_coverage.requires_review', return_value=True):
                report = self.run_monitor(root, worker, reviewer, full_function_plan=True)
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual(len(worker.calls), 1)
            state = json.loads((root/'run/state.json').read_text())
            self.assertIsNone(state['function_tasks']['current'])
            self.assertEqual([row['task']['id'] for row in state['function_tasks']['accepted']], ['INITIAL'])
            self.assertIn('approved', reviewer.calls[2]['prompt'].lower())
            self.assertIn('构建记录', reviewer.calls[3]['prompt'])

    def test_field_handoff_reopens_only_missing_evidence(self):
        from auto_moniter import field_handoff

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            original = {'revision':'first', 'binding':{'round':6}, 'resume':{'status':'launched'},
                        'items':[{'id':'1', 'kind':'deployment', 'receipt':{'version':'1.2.3'}}]}
            field_handoff.reopen(run, original, ['缺少缓存读数'], [{
                'kind':'observation', 'title':'读取原标签页缓存',
                'owner':'浏览器执行器', 'evidence_needed':'保存实际版本读数',
            }])
            current = field_handoff.read(run)
            self.assertFalse(field_handoff.ready(current))
            self.assertEqual([item['kind'] for item in current['items']], ['observation'])
            self.assertNotIn('resume', current)
            self.assertEqual(current['retained_evidence'][0]['receipt']['version'], '1.2.3')
            self.assertIn('handoff:first:1', field_handoff.citations(current))
            self.assertTrue((run/'field-handoff-first.json').is_file())

    def test_missing_input_for_implicit_fixed_check_defers_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            verify = {**verification_task('VERIFY'), 'delivery_scope': 'function', 'check_ids': ['check:2']}
            build = {**task('BUILD', 'build.py'), 'check_ids': ['check:2']}
            final = verification_task('FINAL', depends=['VERIFY', 'BUILD'])
            initial = json.loads(plan(verify)['text'])
            initial['function_plan'] = [verify, build, final]
            worker = FunctionWorker('worker', [{**result('BUILD', 'done', file='build.py'), 'touch': 'build.py'}])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(initial)}, result('BUILD', 'accepted', file='build.py'),
            ])
            report = self.run_monitor(root, worker, reviewer, full_function_plan=True,
                checks=['test -f backend/input.json', CHECK],
                quality_policy={'check_inputs': {'check:1': ['backend/input.json']}})
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('check:1 backend/input.json', report.summary)
            self.assertEqual(len(worker.calls), 1)
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'], 'VERIFY')
            self.assertEqual([row['task']['id'] for row in state['function_tasks']['accepted']], ['BUILD'])
            self.assertEqual(state['checks'][0], 'test -f backend/input.json')

            (root/'backend').mkdir()
            (root/'backend/input.json').write_text('{}\n')
            worker = FunctionWorker('worker', [result('VERIFY', 'done')])
            reviewer = ScriptedProvider('reviewer', [result('VERIFY', 'accepted')])
            resumed = self.run_monitor(root, worker, reviewer, resume=True, full_function_plan=True,
                                       checks=[], max_rounds=2)
            self.assertEqual(resumed.status, 'max_rounds', resumed.summary)
            self.assertEqual(len(worker.calls), 1)
            record = next(row for row in resumed.records if row.role == 'worker' and row.index == 2)
            receipt = record.watcher_result['candidate_checks']
            self.assertTrue(receipt['passed'])
            self.assertIn('test -f backend/input.json', receipt['context']['commands'])

    def test_operator_preflight_cannot_enter_formal_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preflight = task('MANUAL-PREFLIGHT', 'app.py')
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [plan(preflight), plan(preflight)])
            report = self.run_monitor(root, worker, reviewer,
                quality_policy={'operator_preflights': ['MANUAL-PREFLIGHT']})
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('MANUAL-PREFLIGHT 是管理端预检', report.summary)
            self.assertFalse(worker.calls)

    def test_resume_reviews_checked_candidate_before_pending_replan(self):
        from auto_moniter.directions import Directions

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root/'run'
            assignment = task('F1', 'app.py')
            submission = result('F1', 'incomplete', gaps=['Waiting for handoff evidence'])
            worker = FunctionWorker('worker', [{**submission, 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [
                plan(assignment), result('F1', 'changes_required', gaps=['Waiting for handoff evidence']),
            ])
            self.assertEqual(self.run_monitor(root, worker, reviewer, max_rounds=1,
                                              review_every=1).status, 'max_rounds')
            checkpoint = run/'state.json'
            state = json.loads(checkpoint.read_text())
            state['status'] = 'action_required'
            state['referee'] = {'enabled': True, 'model': '', 'replan': True, 'decision': {
                'action': 'replan', 'summary': 'Review the remaining plan',
                'instructions': 'Verify the handoff before continuing.',
                'evidence': ['Current receipt passed'], 'id': 1, 'cycle': 1,
                'incident': 'planning', 'task_id': 'F1',
            }}
            checkpoint.write_text(json.dumps(state))
            Directions(run).append('The handoff evidence is now available.')
            log = run/'001-check-01.log'
            original_log = log.read_bytes()
            log.write_bytes(original_log + b'changed log\n')
            stale_worker = FunctionWorker('worker', [])
            stale_reviewer = ScriptedProvider('reviewer', [])
            stale = self.run_monitor(root, stale_worker, stale_reviewer, resume=True, review_saved_round=1)
            self.assertEqual(stale.status, 'action_required')
            self.assertIn('原检查回执已变化', stale.summary)
            self.assertFalse(stale_reviewer.calls)
            log.write_bytes(original_log)
            worker = FunctionWorker('worker', [])
            accepted = result('F1', 'accepted')
            accepted['verdict']['issue_resolutions'] = [
                {'id': 'AUD-1', 'summary': 'Handoff verified', 'evidence': ['check:001-check-01.log']},
            ]
            reviewer = ScriptedProvider('reviewer', [accepted])

            def stop_at_replan(supervisor, records, *, next_function=False):
                self.assertTrue(next_function)
                self.assertIsNone(supervisor.function_tasks.current)
                supervisor.request_stop()
                return False

            with patch.object(Supervisor, '_elaborate_requirement', stop_at_replan):
                report = self.run_monitor(root, worker, reviewer, resume=True, review_saved_round=1)
            self.assertEqual(report.status, 'stopped', report.summary)
            self.assertFalse(worker.calls)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertIn('The handoff evidence is now available.', reviewer.calls[0]['prompt'])
            state = json.loads(checkpoint.read_text())
            self.assertEqual([item['task']['id'] for item in state['function_tasks']['accepted']], ['F1'])
            self.assertTrue(state['referee']['replan'])
            self.assertFalse(state['deferred_replan'])

    def test_fixed_check_source_alias_uses_current_candidate_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixed_check = 'test "$(basename "$FRONTEND_ROOT")" = frontend_tig && test "$FRONTEND_ROOT/app.py" -ef app.py'
            assignment = {**task('F1', 'app.py'), 'check_ids': ['check:2']}
            empty = FunctionWorker('worker', [])
            planner = ScriptedProvider('reviewer', [plan(assignment)])
            monitor = Supervisor(RunConfig(
                'Implement input parsing', root, empty, reviewer=planner, workflow='review-first',
                function_dispatch=True, git_history='off', run_dir=root/'run',
                checks=[CHECK, fixed_check], retry_delay_s=0,
            ), log=lambda _: None)
            observe = monitor._event

            def stop_after_planning(event, **fields):
                observe(event, **fields)
                if event == 'requirements_reviewed':
                    monitor.request_stop()

            monitor._event = stop_after_planning
            self.assertEqual(monitor.run().status, 'stopped')
            worker = FunctionWorker('worker', [{**result('F1', 'done'), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [result('F1', 'accepted')])
            resumed = self.run_monitor(root, worker, reviewer, resume=True, checks=[], max_rounds=1,
                                       check_source_aliases=['check:2=frontend_tig'])
            self.assertEqual(resumed.status, 'max_rounds', resumed.summary)
            record = next(row for row in resumed.records if row.role == 'worker')
            receipt = record.watcher_result['candidate_checks']
            self.assertTrue(receipt['passed'])
            self.assertEqual(receipt['context']['commands'], [fixed_check])
            self.assertEqual(receipt['context']['check_source_aliases'], ['check:2=frontend_tig'])
            log = root/'run'/receipt['checks'][0]['log']
            self.assertIn(f'FRONTEND_ROOT={root}/run/check-source-2/frontend_tig', log.read_text())

    def test_declaration_only_change_requires_primary_file_in_edit_scope(self):
        from auto_moniter.function_gate import baseline, scope_problems
        from auto_moniter.workspace import file_snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            source = root / 'schema.py'
            original = 'class Schema:\n    def validate(self):\n        return True\n'
            source.write_text(original)
            assignment = {**task('FIELDS', 'schema.py'), 'symbol': 'Schema.validate'}
            checkpoint = baseline(root.parent / 'baseline.gz', root, assignment, file_snapshot(root), missing_ok=True)
            source.write_text(original.replace('    def validate', '    negated: bool = False\n    def validate'))
            snapshot = file_snapshot(root)
            self.assertTrue(scope_problems(root, assignment, checkpoint, snapshot)[0])
            authorized = {**assignment, 'edit_files': ['schema.py']}
            gaps, hashes = scope_problems(root, authorized, checkpoint, snapshot)
            self.assertEqual(gaps, [])
            self.assertEqual(hashes['schema.py'], checkpoint['targets']['schema.py']['body'])
            source.write_text('class Schema:\n    negated: bool = False\n')
            self.assertTrue(scope_problems(root, authorized, checkpoint, file_snapshot(root))[0])

    def test_completed_external_assignment_unblocks_frontend_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend, backend = root/'frontend', root/'backend'
            frontend.mkdir()
            backend.mkdir()
            (frontend/'app.py').write_text('def parse_input(): return 42\n')
            (backend/'model.py').write_text('def parse_input(): return 0\n')
            subprocess.run(['git', 'init', '-q', str(backend)], check=True)
            subprocess.run(['git', '-C', str(backend), 'add', 'model.py'], check=True)
            subprocess.run(['git', '-C', str(backend), '-c', 'user.name=Test',
                '-c', 'user.email=test@local', 'commit', '-qm', 'Initial'], check=True)
            unrelated = backend/'local-notes.txt'
            unrelated.write_text('Existing local notes\n')
            test_path = 'test_model.py'
            test_source = ('import unittest\nfrom model import parse_input\n\n'
                           'class ModelTests(unittest.TestCase):\n'
                           '    def test_model(self):\n        self.assertEqual(parse_input(), 42)\n')
            foreign = {**task('BACKEND', 'model.py'), 'test_files': [test_path], 'check_ids': ['auto:1'],
                       'review_test': {'path': test_path, 'source': test_source}}
            milestone = {**verification_task('MILESTONE', 'model.py', depends=['BACKEND']),
                         'delivery_scope': 'module', 'check_ids': ['regression:1']}
            repair = {**task('REPAIR', 'model.py', depends=['MILESTONE']),
                      'verification': ['The revised implementation still returns 42']}
            refreshed = {**milestone, 'id': 'REFRESHED', 'depends_on': ['REPAIR']}
            backend_final = verification_task('DONE', 'model.py', depends=['REFRESHED'])
            frontend_final = verification_task('FINAL', depends=['MILESTONE'])
            candidate = result('BACKEND', 'done', file='model.py')
            candidate['touch'] = 'model.py'
            verification = result('DONE', 'done', status='done', file='model.py')

            class Reviewer(ScriptedProvider):
                def _command(self, session, prompt_file, read_only):
                    match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt_file.read_text())
                    if match:
                        self.scripts[0]['verdict']['evidence'].append(match[1])
                    return super()._command(session, prompt_file, read_only)

            repair_candidate = {**result('REPAIR', 'done', file='model.py'), 'touch': 'model.py',
                                'source': 'def parse_input():\n    return 40 + 2\n'}
            verify_milestone = result('MILESTONE', 'done', status='done', file='model.py')
            verify_milestone['verdict']['scope'] = 'module'
            approve_milestone = result('MILESTONE', 'accepted', status='done', file='model.py', next_task=repair)
            approve_milestone['verdict']['scope'] = 'module'
            verify_refresh = result('REFRESHED', 'done', status='done', file='model.py')
            verify_refresh['verdict']['scope'] = 'module'
            approve_refresh = result('REFRESHED', 'accepted', status='done', file='model.py', next_task=backend_final)
            approve_refresh['verdict']['scope'] = 'module'
            worker = FunctionWorker('worker', [candidate, verify_milestone, repair_candidate, verify_refresh, verification])
            proposed = {**foreign, 'verification': ['Paraphrased by receiving reviewer']}
            child_plan = json.loads(plan(proposed)['text'])
            child_plan['function_plan'] = [proposed, milestone, repair, refreshed, backend_final]
            reviewer = Reviewer('reviewer', [{'verdict':{}, 'text':json.dumps(child_plan)},
                result('BACKEND', 'accepted', file='model.py', next_task=milestone), approve_milestone,
                result('REPAIR', 'accepted', file='model.py', next_task=refreshed), approve_refresh,
                result('DONE', 'accepted', status='done', file='model.py')])
            backend_run = backend/'run'
            from auto_moniter.assignment_delivery import contracts
            child = Supervisor(RunConfig('Verify backend behavior', backend, worker, reviewer=reviewer,
                workflow='review-first', function_dispatch=True, full_function_plan=True, git_history='required',
                run_dir=backend_run, checks=[CHECK], retry_delay_s=0, assignment_contracts=contracts([foreign])),
                log=lambda _:None).run()
            self.assertEqual(child.status, 'done', child.summary)
            self.assertEqual(unrelated.read_text(), 'Existing local notes\n')
            self.assertNotIn(unrelated.name, subprocess.check_output(
                ['git', '-C', str(backend), 'ls-tree', '--name-only', 'HEAD'], text=True).splitlines())

            initial = json.loads(plan(foreign)['text'])
            initial['function_plan'] = [foreign, milestone, frontend_final]
            frontend_worker = FunctionWorker('worker', [])
            stopped = self.run_monitor(frontend, frontend_worker, ScriptedProvider('reviewer', [
                {'verdict':{}, 'text':json.dumps(initial)}, approve_requirement_coverage()]), full_function_plan=True,
                assignment_workspaces=['model.py=' + str(backend)])
            self.assertEqual(stopped.status, 'action_required', stopped.summary)
            self.assertFalse(frontend_worker.calls)
            run = frontend/'run'
            handoff = json.loads((run/'assignment-handoff-BACKEND.json').read_text())
            self.assertEqual(handoff['workspace'], str(backend))
            frontend_worker = FunctionWorker('worker', [result('FINAL', 'done', status='done')])
            reviewer = ScriptedProvider('reviewer', [result('FINAL', 'accepted', status='done')])
            config = RunConfig('', frontend, frontend_worker, reviewer=reviewer,
                workflow='review-first', resume=True, git_history='off', run_dir=run, retry_delay_s=0,
                assignment_deliveries=['BACKEND=' + str(backend_run), 'MILESTONE=' + str(backend_run)])
            receiver = Supervisor(config, log=lambda _:None)
            event = receiver._event

            def stop_after_handoff(kind, **details):
                event(kind, **details)
                if kind == 'external_assignment_accepted' and details['task_id'] == 'MILESTONE':
                    receiver.request_stop()

            with patch.object(receiver, '_event', side_effect=stop_after_handoff):
                interrupted = receiver.run()
                self.assertEqual(interrupted.status, 'stopped', interrupted.summary)
            resumed = Supervisor(config, log=lambda _:None).run()
            self.assertEqual(resumed.status, 'done', resumed.summary)
            self.assertEqual(len(frontend_worker.calls), 1)
            self.assertEqual(len(reviewer.calls), 1)
            state = json.loads((run/'state.json').read_text())
            accepted = state['function_tasks']['accepted']
            self.assertEqual([item['task']['id'] for item in accepted], ['BACKEND', 'MILESTONE', 'FINAL'])
            self.assertEqual(accepted[0]['external_delivery']['workspace'], str(backend))
            self.assertFalse((frontend/'model.py').exists())
            worker_record = next(row for row in state['rounds'] if row['role']=='worker')
            local_checks = worker_record['watcher_result']['candidate_checks']['context']['commands']
            self.assertFalse(any(test_path in command for command in local_checks))
            from auto_moniter.assignment_delivery import verified
            child_state = json.loads((backend_run/'state.json').read_text())
            self.assertIsNotNone(verified(backend_run, backend, milestone, child_state['modules'][0],
                                          child_state['requirements_review']))
            final = next(row for row in reversed(child_state['rounds']) if row['role']=='worker')
            receipt = final['watcher_result']['candidate_checks']
            final_log = backend_run / next(check['log'] for check in receipt['checks']
                                           if test_path in check['command'])
            previous = final_log.read_bytes()
            final_log.write_text('Replaced final check output')
            with self.assertRaisesRegex(ValueError, '检查记录'):
                verified(backend_run, backend, foreign, child_state['modules'][0], child_state['requirements_review'])
            final_log.write_bytes(previous)
            (backend_run/'001-check-01.log').write_text('Replaced check output')
            with self.assertRaisesRegex(ValueError, '检查记录'):
                verified(backend_run, backend, foreign, child_state['modules'][0], child_state['requirements_review'])

    def test_foreign_repository_assignment_waits_while_local_work_advances(self):
        self.assert_foreign_repository_assignment()

    def test_reviewer_can_dispatch_local_work_added_while_waiting_for_external_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root, backend, external = parent/'frontend', parent/'backend', parent/'external'
            for path in (root, backend, external):
                path.mkdir()
            (root/'app.py').write_text('def parse_input(): return 0\n')
            (backend/'model.py').write_text('def parse_input(): return 0\n')
            subprocess.run(['git', 'init', '-q', str(backend)], check=True)
            (external/'state.json').write_text(json.dumps({'cwd': str(backend), 'status': 'running'}))
            same_name = 'test_remote.py'
            (root/same_name).write_text('# Local reference copy\n')
            foreign = {**task('BACKEND', 'model.py'), 'test_files': [same_name]}
            local = {**task('FRONTEND', 'app.py'), 'verification': ['Empty input returns 42']}
            first = json.loads(plan(foreign)['text'])
            first['function_plan'] = [foreign, verification_task('FINAL', depends=['BACKEND'])]
            paraphrased = {**foreign, 'verification': ['Paraphrased while replanning']}
            second = {**first, 'modules': [], 'next_task': local, 'function_plan': [local, paraphrased,
                verification_task('FINAL', depends=['BACKEND', 'FRONTEND'])]}
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(first)}, approve_requirement_coverage(),
                {'verdict': {}, 'text': json.dumps(second)}, approve_requirement_coverage(),
                result('FRONTEND', 'accepted', next_task=foreign),
            ])
            worker = FunctionWorker('worker', [{**result('FRONTEND', 'done'), 'touch': 'app.py'}])
            marker = 'Repair the independent frontend behavior'

            def external_running(supervisor, assignment):
                if supervisor.directions.messages():
                    return len(worker.calls) == 0
                supervisor.directions.append(marker)
                return True

            with patch.object(Supervisor, '_external_assignment_running', autospec=True, side_effect=external_running):
                report = self.run_monitor(root, worker, reviewer, full_function_plan=True, max_rounds=3,
                    assignment_workspaces=['model.py=' + str(backend)],
                    assignment_deliveries=['BACKEND=' + str(external)])
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual(len(worker.calls), 1)
            self.assertIn(marker, reviewer.calls[2]['prompt'])
            self.assertNotIn(marker, worker.calls[0]['prompt'])
            self.assertIn('FRONTEND', worker.calls[0]['prompt'])
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual([row['task']['id'] for row in state['function_tasks']['accepted']], ['FRONTEND'])
            self.assertEqual(state['function_tasks']['current']['verification'], foreign['verification'])

    def assert_foreign_repository_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent/'frontend'
            backend = parent/'services'/'backend'
            root.mkdir()
            backend.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(backend)], check=True)
            (backend/'model.py').write_text('def parse_input(): return 0\n')
            (root/'app.py').write_text('def parse_input(): return 0\n')
            foreign = task('BACKEND', 'model.py')
            local = {**task('FRONTEND', 'app.py'), 'verification':['Empty input returns 42']}
            final = verification_task('FINAL', depends=['BACKEND', 'FRONTEND'])
            initial = json.loads(plan(foreign)['text'])
            initial['function_plan'] = [foreign, local, final]
            worker = FunctionWorker('worker', [
                {**result('FRONTEND', 'done'), 'touch':'app.py'},
            ])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict':{}, 'text':json.dumps(initial)},
                approve_requirement_coverage(),
                result('FRONTEND', 'accepted', next_task=foreign),
            ])
            bindings = ['model.py=' + str(backend)]
            report = self.run_monitor(root, worker, reviewer, max_rounds=3, full_function_plan=True,
                                      assignment_workspaces=bindings)
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn(str(backend), report.summary)
            self.assertIn('model.py', report.summary)
            self.assertEqual(len(worker.calls), 1)
            self.assertIn('FRONTEND', worker.calls[0]['prompt'])
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'], 'BACKEND')
            self.assertEqual([row['task']['id'] for row in state['function_tasks']['accepted']], ['FRONTEND'])
            self.assertFalse((root/'model.py').exists())
            self.assertIn('return 0', (backend/'model.py').read_text())
            self.assertEqual(state['assignment_workspaces'], bindings)
            resumed_worker = FunctionWorker('worker', [])
            resumed = Supervisor(RunConfig('', root, resumed_worker, reviewer=ScriptedProvider('reviewer', []),
                workflow='review-first', resume=True, git_history='off', run_dir=root/'run',
                retry_delay_s=0), log=lambda _:None).run()
            self.assertEqual(resumed.status, 'action_required', resumed.summary)
            self.assertIn(str(backend), resumed.summary)
            self.assertFalse(resumed_worker.calls)

    def test_reviewer_authored_test_is_run_again_after_function_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_file = 'test_parse_input.py'
            test_code = (
                'import unittest\nfrom app import parse_input\n\n'
                'class TestParseInput(unittest.TestCase):\n'
                '    def test_approved_behavior(self):\n        self.assertEqual(parse_input(), 42)\n'
            )
            assignment = {**task('F1', 'app.py'), 'test_files': [test_file],
                          'review_test': {'path': test_file, 'source': test_code}}
            worker = FunctionWorker('worker', [
                {**result('F1', 'done'), 'source': 'def parse_input(): return 0\n'},
                {**result('F1', 'done'), 'touch': 'app.py'},
            ])
            defect = 'main/parse_input() returned 0 instead of 42 in the reviewer test'
            approved = result('F1', 'accepted')
            approved['verdict']['issue_resolutions'] = [{'id': 'AUD-1', 'summary': 'Verified against the original test',
                                                          'evidence': ['check:002-check-02.log']}]
            reviewer = ScriptedProvider('reviewer', [
                plan(assignment), result('F1', 'changes_required', gaps=[defect]), approved,
            ])
            report = self.run_monitor(root, worker, reviewer, max_rounds=2, review_every=1)
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual((root / test_file).read_text(), test_code)
            self.assertIn(defect, worker.calls[1]['prompt'])
            self.assertIn('using the filename from this receipt', reviewer.calls[2]['prompt'])
            self.assertIn('002-check-02.log', reviewer.calls[2]['prompt'])
            self.assertNotIn('Review evidence from the current candidate', worker.calls[1]['prompt'])
            receipts = [record.watcher_result['candidate_checks'] for record in report.records if record.role == 'worker']
            self.assertEqual([receipt['checks'][1]['status'] for receipt in receipts], ['check_failed', 'passed'])
            state = json.loads((root / 'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['id'], 'F1')

    def test_worker_cannot_replace_reviewer_authored_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_file = 'test_parse_input.py'
            assignment = {**task('F1', 'app.py'), 'test_files': [test_file],
                          'review_test': {'path': test_file, 'source': 'assert False\n'}}
            worker = FunctionWorker('worker', [{**result('F1', 'done'),
                'writes': {'app.py': 'def parse_input(): return 42\n', test_file: 'assert True\n'}}])
            reviewer = ScriptedProvider('reviewer', [
                plan(assignment), result('F1', 'changes_required', gaps=['Original test was changed']),
                {'verdict': {}, 'text': json.dumps({'approved': False, 'summary': '原断言已被放宽'})},
            ])
            report = self.run_monitor(root, worker, reviewer, max_rounds=2, review_every=1)
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('原断言已被放宽', report.summary)
            self.assertEqual(len(worker.calls), 1)
            candidate = next(record for record in report.records if record.role == 'worker')
            receipt = candidate.watcher_result['candidate_checks']
            self.assertFalse(receipt['passed'])
            self.assertFalse(receipt['checks'])
            self.assertIn(test_file, receipt['scope_gaps'][0])
            state = json.loads((root / 'run/state.json').read_text())
            self.assertFalse(state['function_tasks']['accepted'])
            self.assertEqual(state['function_tasks']['current']['review_test'], assignment['review_test'])

    def test_resumed_test_setup_repair_keeps_the_approved_plan_and_reruns_checks(self):
        self.assert_resumed_test_setup_repair(pending_replan=False)

    def test_pending_referee_replan_runs_after_the_current_test_repair_is_accepted(self):
        self.assert_resumed_test_setup_repair(pending_replan=True)

    def test_approved_test_repair_is_preserved_when_replanning_must_happen_first(self):
        self.assert_resumed_test_setup_repair(pending_replan=True, scope_conflict=True)

    def assert_resumed_test_setup_repair(self, *, pending_replan, scope_conflict=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_file = 'test_parse_input.py'
            original = ('import unittest\nfrom app import parse_input\n'
                        'class TestBehavior(unittest.TestCase):\n'
                        '    def test_result(self): self.assertEqual(parse_input(), 42)\n')
            revised = original.replace('import unittest', 'import unittest\n# Keep setup in this test module.')
            assignment = {**task('F1', 'app.py'), 'test_files': [test_file],
                          'review_test': {'path': test_file, 'source': original}}
            initial = json.loads(plan(assignment)['text'])
            final = verification_task('FINAL', depends=['F1'])
            initial['function_plan'] = [assignment, final]
            first_worker = FunctionWorker('worker', [{**result('F1', 'done'),
                'writes': {'app.py': 'def parse_input(): return 0\n', test_file: revised}}])
            initial_reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(initial)},
                result('F1', 'changes_required', gaps=['Repair the implementation and verify the reviewed test setup.']),
            ])
            self.assertEqual(self.run_monitor(root, first_worker, initial_reviewer,
                max_rounds=1, full_function_plan=True).status, 'max_rounds')
            checkpoint = root/'run/state.json'
            state = json.loads(checkpoint.read_text())
            saved_plan = state['function_plan']
            state['requirements_review'] = {'status': 'reviewing', 'cycle': 1}
            approval = {'approved': True, 'summary': 'Same assertion retained'}
            later_reviews = []
            if pending_replan:
                from auto_moniter import requirement_context, requirement_coverage
                from auto_moniter.directions import Directions
                run = root/'run'
                approved_file = run/'reviewed-requirement.json'
                saved_brief = json.loads(approved_file.read_text())
                saved_brief.update(status='needs_input', blockers=['Other work awaits a prerequisite'])
                approved_file.write_text(json.dumps(saved_brief))
                (run/'2000001-requirement-coverage.json').write_text(json.dumps({
                    'status': 'pass',
                    'sources_sha256': requirement_coverage.digest({
                        'original': 'ORIGINAL BACKLOG MUST NOT REACH WORKER', 'acceptance': '',
                    }),
                    'checks_sha256': requirement_context.checks_digest([CHECK]),
                    'plan_sha256': requirement_coverage.digest({**initial, 'function_plan': saved_plan['tasks']}),
                }))
                state['status'] = 'action_required'
                checkpoint.write_text(json.dumps(state))
                state['referee'] = {'enabled': True, 'model': '', 'replan': True, 'decision': {
                    'action': 'replan', 'summary': 'Review the verification plan',
                    'instructions': 'Keep F1. Revise the remaining verification after F1.',
                    'evidence': ['The final verification needs review'], 'id': 1, 'cycle': 1,
                    'incident': 'planning', 'task_id': 'F1',
                }}
                approval['finish_current_first'] = not scope_conflict
                Directions(root/'run').append('After F1, review the remaining verification.')
                later_plan = {**BRIEF, 'modules': [], 'next_task': final, 'function_plan': [final]}
                later_reviews.append({'verdict': {}, 'text': json.dumps(later_plan)})
                later_reviews.append(approve_requirement_coverage())
            checkpoint.write_text(json.dumps(state))
            accepted = result('F1', 'accepted')
            accepted['verdict']['issue_resolutions'] = [
                {'id': 'AUD-1', 'summary': 'Behavior verified', 'evidence': ['check:002-check-02.log']},
            ]
            worker = FunctionWorker('worker', [{**result('F1', 'done'), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(approval)}, accepted, *later_reviews,
            ])
            if scope_conflict:
                def stop_at_replan(supervisor, records, *, next_function=False):
                    self.assertTrue(next_function)
                    self.assertTrue(supervisor._referee_replan)
                    supervisor.request_stop()
                    return False

                with patch.object(Supervisor, '_elaborate_requirement', stop_at_replan):
                    report = self.run_monitor(root, worker, reviewer, resume=True,
                                              max_rounds=2, full_function_plan=True)
                self.assertEqual(report.status, 'stopped', report.summary)
                self.assertTrue((state := json.loads(checkpoint.read_text()))['referee']['replan'])
                self.assertFalse(state['deferred_replan'])
                self.assertEqual(state['function_tasks']['current']['review_test']['source'], revised)
                self.assertEqual(worker.calls, [])
                return

            report = self.run_monitor(root, worker, reviewer, resume=True,
                                      max_rounds=2, full_function_plan=True)
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertIn('只读复核当前派单的验收测试改动', reviewer.calls[0]['prompt'])
            self.assertTrue(reviewer.calls[0]['read_only'])
            self.assertEqual(len(reviewer.calls), 4 if pending_replan else 2)
            self.assertEqual((root/test_file).read_text(), revised)
            state = json.loads(checkpoint.read_text())
            if pending_replan:
                self.assertIn('After F1, review the remaining verification.', reviewer.calls[0]['prompt'])
                self.assertNotIn('Revise the remaining verification after F1.', worker.calls[0]['prompt'])
                self.assertIn('The Referee has directed replanning', reviewer.calls[2]['prompt'])
                self.assertEqual(state['function_tasks']['current']['id'], 'FINAL')
                self.assertFalse(state['referee']['replan'])
                self.assertFalse(state['deferred_replan'])
                self.assertIn('approved_function_brief_recovered', (root/'run/events.jsonl').read_text())
            else:
                self.assertEqual(state['function_plan']['tasks'][1:], saved_plan['tasks'][1:])
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['review_test']['source'], revised)
            worker_record = next(row for row in report.records if row.role == 'worker' and row.index == 2)
            self.assertEqual(worker_record.watcher_result['candidate_checks']['checks'][1]['status'], 'passed')

    def test_unchanged_function_failure_does_not_repeat_independent_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_file = 'test_parse_input.py'
            source = ('import unittest\nfrom app import parse_input\n'
                      'class TestBehavior(unittest.TestCase):\n'
                      '    def test_result(self): self.assertEqual(parse_input(), 42)\n')
            assignment = {**task('F1', 'app.py'), 'test_files': [test_file],
                          'review_test': {'path': test_file, 'source': source}}
            worker = FunctionWorker('worker', [
                {**result('F1', 'done'), 'source': 'def parse_input(): return 0\n'},
                result('F1', 'done'),
            ])
            reviewer = ScriptedProvider('reviewer', [plan(assignment),
                result('F1', 'changes_required', gaps=['main/parse_input() returned 0 instead of 42'])])
            report = self.run_monitor(root, worker, reviewer, max_rounds=3, review_every=1)
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('same reviewer-authored test', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 2))
            self.assertEqual((root / test_file).read_text(), source)

    def test_approved_manifest_candidate_is_committed_without_a_second_evidence_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            (root / 'styles.css').write_text('.grid { display: grid; }\n')
            (root / 'helper.py').write_text('VALUE = 0\n')
            assignment = {**task('F1', 'app.py'), 'edit_files': ['styles.css', 'helper.py']}
            worker = FunctionWorker('worker', [{
                **result('F1', 'done'),
                'writes': {'app.py': 'def parse_input(): return 42\n', 'helper.py': 'VALUE = 42\n'},
            }])
            approval = result('F1', 'accepted', next_task=task('F2', 'next.py', depends=['F1']))
            reviewer = ScriptedProvider('reviewer', [plan(assignment), approval])
            monitor = Supervisor(RunConfig('Implement the approved assignment', root, worker, reviewer=reviewer,
                workflow='review-first', function_dispatch=True, git_history='required', run_dir=root/'run',
                checks=[CHECK], max_rounds=1, review_every=0, retry_delay_s=0), log=lambda _:None)
            report = monitor.run()
            state = json.loads((root / 'run/state.json').read_text())
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['id'], 'F1')
            self.assertEqual(state['function_tasks']['current']['id'], 'F2')
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))
            self.assertEqual(subprocess.check_output(['git', 'show', 'HEAD:helper.py'], cwd=root), b'VALUE = 42\n')
            self.assertEqual((root / 'styles.css').read_text(), '.grid { display: grid; }\n')

    def test_next_assignment_rejection_survives_resume_without_repeating_accepted_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = task('F2','next.py')
            invalid['test_files'] = ['docs/not_a_test.py']
            worker = FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',next_task=invalid)])
            control = Supervisor._process_control
            def stop_after_acceptance(monitor, active=False):
                if not active and monitor.function_tasks.accepted:
                    (root/'run/control.json').write_text(json.dumps({'action':'stop'}))
                return control(monitor,active)
            with patch.object(Supervisor,'_process_control',stop_after_acceptance):
                report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'stopped',report.summary)
            saved = json.loads((root/'run/state.json').read_text())
            self.assertEqual(saved['planning_rejection']['task_id'],'F2')
            self.assertIn('docs/not_a_test.py',saved['planning_rejection']['reason'])
            self.assertEqual(len(saved['function_tasks']['accepted']),1)
            (root/'run/control.json').unlink()
            worker = FunctionWorker('worker',[
                {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            assignment = task('F2','next.py',scope='requirement',depends=['F1'])
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            reviewer = ScriptedProvider('reviewer',[
                plan(assignment),result('F2','accepted',status='done',file='next.py')])
            report = Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',
                resume=True,git_history='off',run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(len(worker.calls),1)
            self.assertIn('docs/not_a_test.py',reviewer.calls[0]['prompt'])
            self.assertIn('Do not repeat accepted implementation',reviewer.calls[0]['prompt'])
            self.assertIsNone(json.loads((root/'run/state.json').read_text())['planning_rejection'])

    def test_replanning_preserves_canonical_criteria_without_an_extra_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            final=task('F2','next.py',scope='requirement',depends=['F1'])
            final['criterion_ids']=['main:1','req:1:1','req:1:2']
            rewritten=plan(final)
            brief=json.loads(rewritten['text']);brief['acceptance_criteria']=['Paraphrased and reordered criteria']
            rewritten['text']=json.dumps(brief)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'},
                {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted'),
                rewritten,result('F2','accepted',status='done',file='next.py')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,4))
            saved=json.loads((root/'run/state.json').read_text())
            self.assertEqual(saved['requirements_review']['acceptance_criteria'],BRIEF['acceptance_criteria'])
            self.assertIn('requirement_criteria_preserved',(root/'run/events.jsonl').read_text())

    def test_replanning_can_append_criteria_without_a_spurious_repair_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            final=task('F2','next.py',scope='requirement',depends=['F1'])
            final['criterion_ids']=['main:1','req:1:1','req:1:2','req:1:3']
            updated=plan(final)
            brief=json.loads(updated['text']);brief['acceptance_criteria'].append('Boundary input is validated')
            updated['text']=json.dumps(brief)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'},
                {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted'),
                updated,result('F2','accepted',status='done',file='next.py')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,4))
            saved=json.loads((root/'run/state.json').read_text())
            self.assertEqual(saved['requirements_review']['acceptance_criteria'],brief['acceptance_criteria'])

    def test_failed_planning_resume_recovers_approved_criteria_before_worker_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted'),
                {'verdict':{},'text':'invalid next planning output'}])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'action_required',report.summary)
            final=task('F2','next.py',scope='requirement',depends=['F1'])
            final['criterion_ids']=['main:1','req:1:1','req:1:2']
            rewritten=plan(final);brief=json.loads(rewritten['text'])
            brief['acceptance_criteria']=['Cannot replace canonical criteria on resume']
            rewritten['text']=json.dumps(brief)
            worker=FunctionWorker('worker',[{**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            reviewer=ScriptedProvider('reviewer',[rewritten,result('F2','accepted',status='done',file='next.py')])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            saved=json.loads((root/'run/state.json').read_text())
            self.assertEqual(saved['requirements_review']['acceptance_criteria'],BRIEF['acceptance_criteria'])

    def test_format_check_alone_cannot_accept_a_function(self):
        from auto_moniter.function_gate import validate_task
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'requires a behavioral check'):
                validate_task(task('F1','app.py'),Path(directory),
                    {'main':{'acceptance':['Correct behavior']}},['git diff --check'],BRIEF)

    def test_operator_appended_check_reuses_candidate_and_reviews_new_results(self):
        from console.tasks import TaskStore
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'project';root.mkdir()
            ready=parent/'dependency';checked=parent/'new-check-ran'
            original="test -f '"+str(ready)+"' || exit 127"
            assignment=task('F1','app.py',scope='requirement')
            assignment['criterion_ids']=['main:1','req:1:1','req:1:2']
            worker=FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,checks=[original],max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            store=TaskStore(parent/'console',root)
            queued=store.create({'title':'Resume candidate','requirement':'saved','workflow':'legacy'})
            meta=store._get(queued['id']);meta['run_dir']=str(root/'run');store._save(meta)
            added="python3 -c \"from pathlib import Path; Path('"+str(checked)+"').write_text('verified')\""
            with patch.object(store,'_resume_args',return_value=['run','--resume']):
                store.action(queued['id'],'checks',{'additional_checks':[added]})
            ready.touch()
            worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[result('F1','accepted',status='done')])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=1,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(checked.read_text(),'verified')
            self.assertFalse(worker.calls)
            self.assertEqual(len(reviewer.calls),1)
            saved=json.loads((root/'run/state.json').read_text())
            self.assertEqual(saved['check_changes'][0]['added'],[added])
            self.assertEqual(saved['usage']['agent_calls'],3)

    def test_pause_during_next_function_planning_prevents_another_worker_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = FunctionWorker('worker',[
                {**result('F1','done'),'touch':'app.py'},
                {**result('F2','done',file='next.py'),'touch':'next.py'}])
            reviewer = ScriptedProvider('reviewer',[
                plan(task('F1','app.py')), result('F1','accepted'), plan(task('F2','next.py'))])
            elaborate = Supervisor._elaborate_requirement
            beat = Supervisor._beat
            boundaries = []
            def pause_after_plan(monitor, records, **kwargs):
                ok = elaborate(monitor, records, **kwargs)
                if kwargs.get('next_function'):
                    (root/'run/control.json').write_text(json.dumps({'action':'pause'}))
                return ok
            def stop_at_pause(monitor, note):
                if 'paused at a turn boundary' in note:
                    boundaries.append((len(worker.calls), len(reviewer.calls)))
                    (root/'run/control.json').write_text(json.dumps({'action':'stop'}))
                return beat(monitor, note)
            with patch.object(Supervisor, '_elaborate_requirement', pause_after_plan), \
                    patch.object(Supervisor, '_beat', stop_at_pause):
                report = self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'stopped',report.summary)
            self.assertEqual(boundaries,[(1,3)])
            self.assertFalse((root/'next.py').exists())
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['next_round'],2)
            self.assertEqual(state['function_tasks']['current']['id'],'F2')

    def test_chinese_criterion_citation_passes_handoff_without_extra_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = task('F1','app.py',scope='requirement')
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            implementation = result('F1','done',status='done')
            implementation['verdict']['evidence'] = [
                'QUOTES / main:1、req:1:3：app.py:86 在证券校验后恢复合法失败文字；报价校验保持不变。']
            worker = FunctionWorker('worker',[{**implementation,'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            self.assertTrue((root/'run/001-check-01.log').exists())
            self.assertEqual(report.records[-1].watcher_result['function']['status'],'accepted')

    def test_reviewer_approval_without_a_function_citation_is_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = task('F1','app.py',scope='requirement')
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            worker = FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(assignment),
                result('F1','accepted',status='done',file='not-the-target.py')])
            report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            self.assertTrue(all(call['read_only'] for call in reviewer.calls))

    def test_resume_reuses_approval_rejected_by_the_old_citation_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = task('F1','app.py',scope='requirement')
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            implementation = result('F1','done',status='done')
            implementation['verdict']['evidence'] = ['main:1、req:1:3：app.py:86 已修复']
            worker = FunctionWorker('worker',[{**implementation,'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            with patch.object(Supervisor, '_review_function', side_effect=ValueError(
                    'worker must cite every changed assignment file; missing: app.py')):
                report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual(len(worker.calls),1)
            worker = FunctionWorker('worker',[])
            reviewer = ScriptedProvider('reviewer',[])
            report = Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',
                resume=True,git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(len(worker.calls),0)
            self.assertFalse(reviewer.calls)
            self.assertTrue((root/'run/001-check-01.log').exists())

    def test_environment_repair_reuses_worker_and_reviews_results_after_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'project';root.mkdir();dependency=parent/'dependency-ready'
            check="test -f '"+str(dependency)+"' || exit 127"
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            next_task=task('F2','next.py',depends=['F1'],scope='requirement')
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',next_task=next_task)])
            report=self.run_monitor(root,worker,reviewer,checks=[check],max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            state=json.loads((root/'run/state.json').read_text())
            original_calls=state['usage']['agent_calls']
            self.assertIn('candidate_checks',state['rounds'][-1]['watcher_result'])
            self.assertEqual(state['rounds'][-1]['role'],'worker')
            dependency.touch()
            worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[result('F1','accepted',next_task=next_task)])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=1,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'max_rounds',report.summary)
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'],'F2')
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['id'],'F1')
            self.assertEqual(state['usage']['agent_calls'],original_calls+1)
            self.assertFalse(worker.calls)
            self.assertEqual(len(reviewer.calls),1)

    def test_changed_source_prevents_environment_review_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted')])
            report=self.run_monitor(root,worker,reviewer,checks=['exit 127'],max_rounds=1)
            self.assertEqual(report.status,'action_required')
            (root/'app.py').write_text('def parse_input(): return 0\n')
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[result('F1','accepted')])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual(len(worker.calls),1)
            self.assertEqual(len(reviewer.calls),0)

    def test_verification_closes_existing_implementation_without_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source='def parse_input():\n    return 42\n'
            (root/'app.py').write_text(source)
            worker=FunctionWorker('worker',[result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=1,
                checks=["python3 -c 'from app import parse_input; assert parse_input() == 42'"])
            self.assertEqual(report.status,'done',report.summary)
            self.assertTrue(worker.calls[0]['read_only'])
            self.assertTrue(all(c['read_only'] for c in reviewer.calls))
            self.assertEqual((root/'app.py').read_text(),source)
            accepted=json.loads((root/'run/state.json').read_text())['function_tasks']['accepted']
            self.assertEqual(accepted[0]['task']['kind'],'verification')
            self.assertEqual(accepted[0]['verification'],{'check:1':'001-check-01.log'})

    def test_verification_after_function_acceptance_does_not_require_new_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'},
                result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',next_task=verification_task(depends=['F1'])),
                result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual([c['read_only'] for c in worker.calls],[False,True])
            self.assertIn('Verification-only',worker.calls[-1]['prompt'])

    def test_verification_modification_or_failed_assertion_never_delivers(self):
        for change,check in [(True,CHECK),(False,'false')]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'app.py').write_text('def parse_input(): return 0\n')
                worker=FunctionWorker('worker',[{**result('VERIFY','done',status='done'),
                    **({'touch':'app.py'} if change else {})}])
                reviewer=ScriptedProvider('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
                report=self.run_monitor(root,worker,reviewer,max_rounds=1,checks=[check])
                self.assertNotEqual(report.status,'done')
                state=json.loads((root/'run/state.json').read_text())
                self.assertEqual(state['function_tasks']['accepted'],[])

    def test_failed_verification_dispatches_a_bounded_implementation_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'app.py').write_text('def parse_input(): return 0\n')
            implementation=task('REPAIR','app.py',scope='requirement')
            implementation['criterion_ids']=['main:1','req:1:1','req:1:2']
            worker=FunctionWorker('worker',[result('VERIFY','incomplete',gaps=['Return value is incorrect']),
                {**result('REPAIR','done',status='done'),'touch':'app.py'}])
            missing_assignment=result('VERIFY','changes_required',gaps=['main/Return value must be 42'])
            rejected=result('VERIFY','changes_required',gaps=['main/Return value must be 42'],next_task=implementation)
            accepted=result('REPAIR','accepted',status='done')
            accepted['verdict']['issue_resolutions']=[{'id':'AUD-1','summary':'Verified return value is 42','evidence':['app.py']}]
            def complete_plan(assignment):
                response = json.loads(plan(assignment)['text'])
                response['function_plan'] = [assignment]
                return {'verdict': {}, 'text': json.dumps(response)}
            reviewer=ScriptedProvider('reviewer',[complete_plan(verification_task()), missing_assignment,
                rejected, accepted])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2,review_every=1,full_function_plan=True)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual([call['read_only'] for call in worker.calls],[True,False])
            self.assertNotIn('Continue the authorized task now', worker.calls[0]['prompt'])
            self.assertTrue(worker.calls[1]['prompt'].startswith('Continue the authorized task now'))
            self.assertIn('complete independent work within the current scope', worker.calls[1]['prompt'])
            self.assertIn('Respect tool permissions and safety requirements', worker.calls[1]['prompt'])
            self.assertEqual(len(reviewer.calls),4)
            self.assertIn('For an accepted result, use this exact next_task or null',reviewer.calls[1]['prompt'])
            self.assertIn('Provide a bounded implementation next_task',reviewer.calls[2]['prompt'])
            ledger=json.loads((root/'run/state.json').read_text())['function_tasks']
            self.assertEqual([item['task']['id'] for item in ledger['accepted']],['REPAIR'])

    def test_verification_repair_with_invalid_criteria_retries_reviewer_on_same_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 0\n')
            repair = task('REPAIR', 'app.py', scope='requirement')
            repair['criterion_ids'] = ['main:1', 'req:1:1', 'req:1:2']
            invalid = {**repair, 'criterion_ids': ['req:1:1']}
            finding = ['main/Return value must be 42']
            worker = FunctionWorker('worker', [
                result('VERIFY', 'incomplete', gaps=finding),
                {**result('REPAIR', 'done', status='done'), 'touch': 'app.py'},
            ])
            approved = result('REPAIR', 'accepted', status='done')
            approved['verdict']['issue_resolutions'] = [
                {'id': 'AUD-1', 'summary': 'Verified return value is 42', 'evidence': ['app.py']}]
            reviewer = ScriptedProvider('reviewer', [
                plan(verification_task()),
                result('VERIFY', 'changes_required', gaps=finding, next_task=invalid),
                result('VERIFY', 'changes_required', gaps=finding, next_task=repair),
                approved,
            ])
            report = self.run_monitor(root, worker, reviewer, max_rounds=2, review_every=1)
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual([call['read_only'] for call in worker.calls], [True, False])
            self.assertTrue(reviewer.calls[2]['read_only'])
            self.assertIn('Invalid verification repair REPAIR', reviewer.calls[2]['prompt'])
            self.assertIn('must reference its module criteria', reviewer.calls[2]['prompt'])

    def test_watcher_rejects_protected_edit_in_reviewer_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root/'validation.py'
            protected.write_text('KEEP = True\n')
            assignment = task('IMPLEMENT', 'app.py', scope='requirement')
            assignment['criterion_ids'] = ['main:1', 'req:1:1', 'req:1:2']
            proposal = {**assignment, 'edit_files': ['validation.py']}
            worker = FunctionWorker('worker', [
                {**result('IMPLEMENT', 'done', status='done'), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [
                plan(proposal), plan(assignment), result('IMPLEMENT', 'accepted', status='done')])
            report = self.run_monitor(root, worker, reviewer, max_rounds=1,
                quality_policy={'validation_paths':['validation.py'], 'verified_progress':False})
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual(len(worker.calls), 1)
            self.assertIn('cannot edit protected files: validation.py', reviewer.calls[1]['prompt'])
            self.assertEqual(protected.read_text(), 'KEEP = True\n')

    def test_changed_verification_target_returns_to_reviewer_before_worker_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'app.py'
            source.write_text('def parse_input(): return 42\n')
            initial = verification_task()
            corrected = {**initial, 'symbol': 'parse_value'}
            worker = FunctionWorker('worker', [result('VERIFY', 'done', status='done')])
            reviewer = ScriptedProvider('reviewer', [
                plan(initial), plan(corrected), result('VERIFY', 'accepted', status='done'),
            ])
            monitor = Supervisor(RunConfig('Verify the current parser', root, worker, reviewer=reviewer,
                workflow='review-first', function_dispatch=True, git_history='off',
                run_dir=root/'run', checks=['test -f app.py'], max_rounds=1, retry_delay_s=0),
                log=lambda _:None)
            def change_after_planning():
                source.write_text('def parse_value(): return 42\n')
                return ''
            with patch.object(monitor, '_assignment_whitespace_preflight', side_effect=change_after_planning):
                report = monitor.run()
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual(len(worker.calls), 1)
            self.assertIn('parse_value', worker.calls[0]['prompt'])
            self.assertIn('app.py#parse_input', reviewer.calls[1]['prompt'])
            events = [json.loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
            names = [entry['event'] for entry in events]
            self.assertLess(names.index('function_dispatch_preflight_rejected'),
                            names.index('function_dispatch_validated'))

    def test_supplemental_check_reuses_candidate_and_fixed_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            (root/'test_export.py').write_text(
                'import unittest\n\nclass Export(unittest.TestCase):\n'
                '    def test_value(self): self.assertEqual(42, 42)\n')
            worker = FunctionWorker('worker', [result('VERIFY', 'done', status='done')])
            requested = result('VERIFY', 'changes_required')
            requested['verdict']['supplemental_checks'] = [
                {'path': 'test_export.py', 'criterion_id': 'main:1', 'audit_id': 'AUD-1'}]
            approved = result('VERIFY', 'accepted', status='done')
            approved['verdict']['issue_resolutions'] = [
                {'id':'AUD-1', 'summary':'The exact candidate passed', 'evidence':['check:001-check-02.log']}]
            reviewer = ScriptedProvider('reviewer', [plan(verification_task()), requested, approved])
            monitor = Supervisor(RunConfig('Verify export', root, worker, reviewer=reviewer,
                workflow='review-first', function_dispatch=True, git_history='off', run_dir=root/'run',
                checks=['test -f app.py && echo fixed >> run/check-count'],
                quality_policy={'protect_tests':True, 'verified_progress':False},
                max_rounds=1, retry_delay_s=0), log=lambda _:None)
            ids = monitor.audit.record(['Need current export test evidence'], 1, ['main'])
            monitor.audit.resolve([{'id': ids[0], 'summary': 'Prior candidate passed',
                                   'evidence': ['test_export.py']}], 1, 'a'*64, None)
            report = monitor.run()
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual(len(worker.calls), 1)
            self.assertEqual((root/'run/check-count').read_text(), 'fixed\n')
            receipt = next(item for item in report.records if item.role == 'worker').watcher_result['candidate_checks']
            self.assertTrue(receipt['passed'])
            self.assertEqual(receipt['context']['supplemental_checks'][0]['audit_id'], 'AUD-1')
            self.assertIn('001-check-02.log', reviewer.calls[-1]['prompt'])

    def test_verification_replans_when_amendment_arrives_during_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            requested = result('VERIFY', 'changes_required')
            requested['verdict'].update(
                blockers=['需要当前候选的专项回执'],
                supplemental_checks=[{'path': 'test_export.py', 'criterion_id': 'main:1', 'audit_id': 'AUD-1'}])
            first = verification_task()
            worker = FunctionWorker('worker', [result('VERIFY', 'done', status='done')] * 2)
            approved = result('VERIFY', 'accepted', status='done')
            reviewer = ScriptedProvider('reviewer', [plan(first), requested,
                plan(first), approve_requirement_coverage(), approved])
            monitor = Supervisor(RunConfig('Verify export', root, worker, reviewer=reviewer,
                workflow='review-first', function_dispatch=True, git_history='off', run_dir=root/'run',
                checks=[CHECK], max_rounds=2, retry_delay_s=0), log=lambda _:None)
            invoke = monitor._invoke

            def amend_during_review(*args, **kwargs):
                turn = invoke(*args, **kwargs)
                if kwargs['role'] == 'reviewer' and kwargs['index'] == 1:
                    monitor.directions.append('Include the persisted export result in verification.')
                return turn

            with patch.object(monitor, '_invoke', side_effect=amend_during_review):
                report = monitor.run()
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual(len(worker.calls), 2)
            events = (root/'run/events.jsonl').read_text()
            self.assertIn('verification_replanning', events)
            self.assertNotIn('verification_waiting', events)

    def test_field_wait_runs_independent_checks_then_resumes_original_review(self):
        from auto_moniter import field_handoff

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', 'app.py'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                            'commit', '-qm', '初始版本'], cwd=root, check=True)
            waiting = {**verification_task('FIELD'), 'delivery_scope': 'function'}
            local = {**verification_task('LOCAL'), 'delivery_scope': 'function'}
            followup = {**verification_task('FOLLOWUP', depends=['LOCAL']), 'delivery_scope': 'function'}
            final = verification_task('FINAL', depends=['FIELD', 'FOLLOWUP'])
            initial = json.loads(plan(waiting)['text'])
            initial['function_plan'] = [waiting, local, followup, final]
            blocked = result('FIELD', 'changes_required')
            blocked['verdict']['blockers'] = ['现场版本需要核对']
            worker = FunctionWorker('worker', [result(key, 'done') for key in ('FIELD', 'LOCAL', 'FOLLOWUP')])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(initial)}, blocked,
                result('LOCAL', 'accepted'), result('FOLLOWUP', 'accepted'),
            ])
            report = self.run_monitor(root, worker, reviewer, full_function_plan=True,
                                      keep_rounds=1, git_history='auto')
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual(len(worker.calls), 3)
            self.assertEqual(len(reviewer.calls), 4)
            run = root/'run'
            state = json.loads((run/'state.json').read_text())
            self.assertEqual([row['task']['id'] for row in state['function_tasks']['accepted']], ['LOCAL', 'FOLLOWUP'])
            handoff = field_handoff.read(run)
            checkpoint = field_handoff.binding(state)
            self.assertEqual(checkpoint, handoff['binding'])
            self.assertEqual(checkpoint['round'], 1)
            field_handoff.submit(run, state, {'binding': checkpoint, 'revision': handoff['revision'],
                'id': '1', 'receipt': {'result': '现场版本已核对'}})
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [result('FIELD', 'accepted')])
            report = self.run_monitor(root, worker, reviewer, full_function_plan=True,
                                      resume=True, max_rounds=3, git_history='auto')
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual(len(worker.calls), 0)
            self.assertIn('现场版本已核对', reviewer.calls[0]['prompt'])
            state = json.loads((run/'state.json').read_text())
            self.assertEqual([row['task']['id'] for row in state['function_tasks']['accepted']], ['LOCAL', 'FOLLOWUP', 'FIELD'])

    def test_verification_waits_for_an_external_prerequisite_without_redispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            worker=FunctionWorker('worker',[result('VERIFY','incomplete',gaps=['Browser unavailable'])])
            blocked=result('VERIFY','changes_required')
            blocked['verdict']['blockers']=['The required browser environment is unavailable.']
            reviewer=ScriptedProvider('reviewer',[plan(verification_task()),blocked])
            report=self.run_monitor(root,worker,reviewer,max_rounds=3,review_every=1)
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertIn('browser environment is unavailable',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'],'VERIFY')
            self.assertEqual(state['function_tasks']['accepted'],[])

            from auto_moniter import field_handoff
            from datetime import datetime, timezone
            run = root/'run'
            self.assertTrue(field_handoff.read(run)['automatic'])
            handoff = field_handoff.configure(run, state, [
                {'kind': 'deployment', 'owner': '现场执行者', 'title': '部署初测候选',
                 'expected_version': '1.2.3', 'expected_manifest_sha256': 'a'*64,
                 'expected_target':'example.test', 'expected_resources':{'app.js':'b'*64}},
                {'kind': 'browser', 'owner': '现场测试者', 'title': '保留原标签页',
                 'url': 'https://example.test/login', 'expected_version': '1.2.3', 'browser_proof':True},
            ])
            request = {key: handoff[key] for key in ('binding', 'revision')}
            deployment = {'version': '1.2.3', 'manifest_sha256': 'a'*64, 'rollback': 'previous',
                          'result': '部署版本和资源匹配现场记录'}
            import threading
            from console.tasks import TaskError, TaskStore
            store = object.__new__(TaskStore)
            store._lock = threading.RLock()
            meta = {'id':'example','run_dir':str(run),'managed':True}
            store._get = lambda ident: meta
            store._catalog = lambda: {'example':meta}
            store._view = lambda *args, **kwargs: {'status':'action_required','actions':['resume']}
            with patch.object(store, 'action') as resume:
                browser = {'version': '1.2.3', 'session_reference': 'saved-tab-1', 'result': '已保留初测原标签页'}
                with self.assertRaisesRegex(TaskError, '先提交同一候选'):
                    store.field_handoff('example', {**request, 'id':'2', 'receipt': browser})
                with self.assertRaisesRegex(TaskError, '交接版本已变化'):
                    store.field_handoff('example', {**request, 'id':'1', 'revision':'old', 'receipt': deployment})
                with self.assertRaisesRegex(TaskError, '实际版本'):
                    store.field_handoff('example', {**request, 'id': '1', 'receipt': {**deployment, 'version':'old'}})
                claim = store.field_handoff('example', {**request, 'id':'1', 'action':'claim', 'name':'现场人员'})
                self.assertNotIn('key_sha256', claim['items'][0]['claim'])
                with self.assertRaisesRegex(TaskError, '原认领凭据'):
                    store.field_handoff('example', {**request, 'id':'1', 'receipt': deployment})
                submission = {**request, 'id':'1', 'receipt':deployment, 'claim_key':claim['claim_key']}
                with self.assertRaisesRegex(TaskError, '哈希不一致'):
                    store.field_handoff('example', {**submission, 'proof':{'target':'example.test','resources':{'app.js':'c'*64}}})
                partial = store.field_handoff('example', {**submission,
                    'proof':{'target':'example.test','resources':{'app.js':'b'*64}}})
                resume.assert_not_called()
                browser_return = {**request, 'id':'2', 'receipt':browser, 'proof':{
                    'url':'https://example.test/home', 'session_reference':'saved-tab-1',
                    'observed_at':datetime.now(timezone.utc).isoformat()}}
                invalid = {**browser_return, 'proof':{**browser_return['proof'],'url':'https://other.test/home'}}
                with self.assertRaisesRegex(TaskError, '不同源'):
                    store.field_handoff('example', invalid)
                # Simulate the console exiting after persisting the last receipt, before starting the child.
                complete = field_handoff.submit(run, state, browser_return)
                (run/field_handoff.FILE).write_text(json.dumps({**complete, 'fallback': {
                    'revision': handoff['revision'], 'role': 'referee', 'failures': {'2': '旧浏览器错误'},
                }}))
                resume.assert_not_called()
                store.recover_field_handoffs()
                store.recover_field_handoffs()
                store.field_handoff('example', browser_return)
                resume.assert_called_once_with('example', 'resume')
            self.assertFalse(field_handoff.ready(partial))
            self.assertTrue(field_handoff.ready(complete))
            self.assertEqual(field_handoff.read(run)['resume']['status'], 'launched')
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [result('VERIFY', 'accepted', status='done')])
            report = Supervisor(RunConfig('', root, worker, reviewer=reviewer, workflow='review-first',
                resume=True, referee=True, git_history='off', run_dir=root/'run', retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, 'done', report.summary)
            self.assertFalse(worker.calls)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertIn('saved-tab-1', reviewer.calls[0]['prompt'])
            self.assertIn(f"handoff:{handoff['revision']}:2", reviewer.calls[0]['prompt'])
            resumed = json.loads((root/'run/state.json').read_text())
            self.assertEqual([item['task']['id'] for item in resumed['function_tasks']['accepted']], ['VERIFY'])

    def test_waiting_handoff_dispatches_only_explicitly_linked_tasks_once(self):
        from auto_moniter import field_handoff
        from console.tasks import TaskError, TaskStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = {'id':'parent','cwd':str(root/'parent'),'run_dir':str(root)}
            children = {
                'build': {'id':'build','cwd':str(root/'build'),'run_dir':str(root/'build-run'),
                          'managed':True,'task_type':'deployment'},
                'local': {'id':'local','cwd':str(root/'local'),'managed':True,'task_type':'code'},
                'browser': {'id':'browser','cwd':str(root/'browser'),'run_dir':str(root/'browser-run'),
                            'managed':True,'task_type':'documentation'},
                'overlap': {'id':'overlap','cwd':str(root/'parent'/'child'),'managed':True,'task_type':'code'},
            }
            store = object.__new__(TaskStore)
            store._get = children.__getitem__
            store._view = lambda *args, **kwargs: {'status':'queued'}
            with self.assertRaisesRegex(TaskError, '独立工作区'):
                store._validate_handoff_task(parent, 'overlap', deployment=False)
            handoff = {'binding':{'round':6},'revision':'current',
                       'items':[{'id':'1','kind':'deployment','executor_task':'build',
                                 'artifact_manifest':'/approved/inputs.json'},
                                {'id':'2','kind':'browser','executor_task':'browser'}],
                       'independent_tasks':['local']}
            with patch.object(store, 'action') as start:
                first = store._dispatch_handoff_tasks(parent, handoff)
                store._dispatch_handoff_tasks(parent, first)
                self.assertEqual([(call.args) for call in start.call_args_list],
                                 [('local','start'),('build','start')])
                prompt = field_handoff.executor_prompt(root/'build',root/'build-run')
                self.assertIn('/approved/inputs.json',prompt)
                self.assertIn('verify',prompt)
                self.assertFalse((root/'browser-run'/field_handoff.EXECUTOR_REQUEST).exists())
                with self.assertRaisesRegex(ValueError, '已绑定另一份'):
                    field_handoff.prepare_executor(root/'build-run','parent',
                        {**handoff,'revision':'other'},handoff['items'][0])
                first['items'][0]['receipt'] = {'version':'123'}
                store._dispatch_handoff_tasks(parent, first)
                self.assertEqual(start.call_args.args, ('browser','start'))
                self.assertIn('session_reference',field_handoff.executor_prompt(root/'browser',root/'browser-run'))

    def test_field_result_collection_rejects_stale_executor_output_and_resumes_once(self):
        from auto_moniter import field_handoff
        from auto_moniter.workspace import workspace_fingerprint
        from console.tasks import TaskStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, cwd = root/'run', root/'browser'
            run.mkdir()
            cwd.mkdir()
            task_def = {'id':'VERIFY','kind':'verification'}
            tree = 'a'*64
            state = {'function_tasks':{'current':task_def}, 'rounds':[
                {'role':'worker','index':6,'tree':tree,'watcher_result':{'candidate_checks':{
                    'digest':'b'*64,'context':{'task':task_def}}}},
                {'role':'reviewer','index':6,'task_status':'changes_required',
                 'blockers':['浏览器证据待取得'],'watcher_result':{'status':'blocked'}},
            ]}
            deployment_id, browser_id = '1'*32, '2'*32
            meta = {'id':'parent','cwd':str(root/'parent'),'run_dir':str(run)}
            deployment_run, browser_run = root/'deploy-run', root/'browser-run'
            browser_run.mkdir()
            children = {
                deployment_id:{'id':deployment_id,'cwd':str(root/'deployment'),'run_dir':str(deployment_run),
                               'managed':True,'task_type':'deployment'},
                browser_id:{'id':browser_id,'cwd':str(cwd),'run_dir':str(browser_run),
                            'managed':True,'task_type':'documentation'},
            }
            value = field_handoff.configure(run,state,[
                {'kind':'deployment','owner':'部署执行器','title':'部署','executor_task':deployment_id},
                {'kind':'browser','owner':'浏览器执行器','title':'浏览器观察','executor_task':browser_id},
            ])
            request = {key:value[key] for key in ('binding','revision')}
            deployment = {**request, 'id':'1', 'receipt':{
                'version':'123','manifest_sha256':'c'*64,'rollback':'backup','result':'已部署'}}
            browser = {**request, 'id':'2', 'receipt':{
                'version':'123','session_reference':'tab-1','result':'已保留原标签页'}}
            result_path = cwd/field_handoff.EXECUTOR_RESULT
            store = object.__new__(TaskStore)
            store._get = children.__getitem__
            store._view = lambda *_args, **_kwargs: {'status':'done'}
            read_result = store._executor_field_result
            with patch.object(store, '_executor_field_result', side_effect=lambda child, item:
                              deployment if child['id'] == deployment_id else read_result(child, item)):
                for revision in ('stale', request['revision']):
                    result_path.write_text(json.dumps({**browser,'revision':revision}))
                    (browser_run/'state.json').write_text(json.dumps({
                        'status':'done','tree':workspace_fingerprint(cwd,ignore_dir=browser_run)}))
                    value = store._collect_field_results(meta, run, state, value)
                    self.assertEqual(field_handoff.ready(value), revision == request['revision'])
                self.assertEqual(value['items'][1]['receipt']['executor_task'], browser_id)
            with patch.object(store, 'action') as resume:
                store._resume_field_handoff('parent', run, value)
                store._resume_field_handoff('parent', run, field_handoff.read(run))
                resume.assert_called_once_with('parent', 'resume')

    def test_final_verification_environment_retry_only_reviews_completed_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'project';root.mkdir();dependency=parent/'ready'
            (root/'app.py').write_text('def parse_input(): return 42\n')
            worker=FunctionWorker('worker',[result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,checks=["test -f '"+str(dependency)+"' || exit 127"])
            self.assertEqual(report.status,'action_required',report.summary)
            before=json.loads((root/'run/state.json').read_text())
            dependency.touch()
            worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[result('VERIFY','accepted',status='done')])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertFalse(worker.calls)
            self.assertEqual(len(reviewer.calls),1)
            after=json.loads((root/'run/state.json').read_text())
            self.assertEqual(after['usage']['agent_calls'],before['usage']['agent_calls']+1)

    def test_new_human_direction_prevents_reusing_an_environment_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted')])
            self.run_monitor(root,worker,reviewer,checks=['exit 1'],max_rounds=1)
            state=json.loads((root/'run/state.json').read_text())
            from auto_moniter.supervisor import _record_from_state
            monitor=Supervisor(RunConfig('',root,FunctionWorker('worker',[]),reviewer=ScriptedProvider('reviewer',[]),
                workflow='review-first',run_dir=root/'run',resume=True),log=lambda _:None)
            monitor._load_resume()
            monitor._records=[_record_from_state(item) for item in state['rounds']]
            monitor.config.requirement=(root/'run/requirement.md').read_text().strip()
            record=_record_from_state(state['rounds'][-1])
            self.assertIsNotNone(monitor._check_retry_review(record))
            with patch.object(monitor,'_pending_directions',return_value=True):
                self.assertIsNone(monitor._check_retry_review(record))

    def test_verification_of_committed_code_does_not_create_a_fake_code_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            subprocess.run(['git','add','app.py'],cwd=root,check=True)
            subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','initial'],cwd=root,check=True)
            head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            class Reviewer(ScriptedProvider):
                def _command(self, session, prompt_file, read_only):
                    match=re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt_file.read_text())
                    if match:self.scripts[0]['verdict']['evidence'].append(match[1])
                    return super()._command(session,prompt_file,read_only)
            worker=FunctionWorker('worker',[result('VERIFY','done',status='done')])
            reviewer=Reviewer('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
            report=Supervisor(RunConfig('Verify existing implementation',root,worker,reviewer=reviewer,
                workflow='review-first',function_dispatch=True,git_history='required',run_dir=root/'run',
                checks=[CHECK],retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),head)
            publication=report.records[-1].git_checkpoint['business_commit']
            self.assertFalse(publication['created'])

    def test_evidence_only_verification_advances_without_publishing_pending_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'app.py').write_text('def parse_input(): return 0\n')
            subprocess.run(['git', 'add', 'app.py'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
                            'commit', '-qm', 'initial'], cwd=root, check=True)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            (root/'app.py').write_text('def parse_input(): return 42\n')
            evidence = {**verification_task('EVIDENCE'), 'delivery_scope':'function'}
            module = {**verification_task('MODULE', depends=['EVIDENCE']), 'delivery_scope':'module'}
            final = verification_task('FINAL', depends=['MODULE'])
            initial = json.loads(plan(evidence)['text'])
            initial['function_plan'] = [evidence, module, final]

            class Reviewer(ScriptedProvider):
                def _command(self, session, prompt_file, read_only):
                    match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt_file.read_text())
                    if match:
                        self.scripts[0]['verdict']['evidence'].append(match[1])
                    return super()._command(session, prompt_file, read_only)

            worker = FunctionWorker('worker', [result('EVIDENCE', 'done')])
            reviewer = Reviewer('reviewer', [{'verdict':{}, 'text':json.dumps(initial)},
                                             result('EVIDENCE', 'accepted', next_task=module)])
            report = Supervisor(RunConfig('Verify existing implementation', root, worker, reviewer=reviewer,
                workflow='review-first', function_dispatch=True, full_function_plan=True,
                git_history='required', run_dir=root/'run', checks=[CHECK], max_rounds=1,
                retry_delay_s=0), log=lambda _:None).run()
            self.assertEqual(report.status, 'max_rounds', report.summary)
            ledger = json.loads((root/'run/state.json').read_text())['function_tasks']
            self.assertEqual([item['task']['id'] for item in ledger['accepted']], ['EVIDENCE'])
            self.assertEqual(ledger['current']['id'], 'MODULE')
            self.assertEqual(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
                                                     text=True).strip(), head)
            self.assertIn('return 42', (root/'app.py').read_text())

    def test_blocked_module_handoff_replans_read_only_evidence_before_later_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            early = {**verification_task('EVIDENCE'), 'delivery_scope':'module'}
            module = {**verification_task('MODULE', depends=['EVIDENCE']), 'delivery_scope':'module'}
            final = verification_task('FINAL', depends=['MODULE'])
            initial = json.loads(plan(early)['text'])
            initial['function_plan'] = [early, module, final]
            blocked = result('EVIDENCE', 'changes_required')
            blocked['verdict']['blockers'] = ['Module-wide validation is scheduled for MODULE.']
            worker = FunctionWorker('worker', [result('EVIDENCE', 'incomplete', gaps=['Module review is pending']),
                                               result('EVIDENCE', 'done')])
            reviewer = ScriptedProvider('reviewer', [{'verdict':{}, 'text':json.dumps(initial)},
                blocked, result('EVIDENCE', 'accepted', next_task=module)])
            report = self.run_monitor(root, worker, reviewer, max_rounds=2, review_every=1,
                                      full_function_plan=True)
            self.assertEqual(report.status, 'max_rounds', report.summary)
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['delivery_scope'], 'function')
            self.assertEqual(state['function_tasks']['current']['id'], 'MODULE')
            self.assertEqual(state['function_tasks']['current']['delivery_scope'], 'module')

    def test_review_repair_stays_on_first_function_then_dispatches_next(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=task('F1','app.py');second=task('F2','next.py',depends=['F1'],scope='requirement')
            w=FunctionWorker('worker',[
                {**result('F1','done'),'touch':'app.py'}, result('F1','done'),
                {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            final=result('F2','accepted',status='done',file='next.py')
            final['verdict']['issue_resolutions']=[{'id':'AUD-1','summary':'Revalidated after integration change','evidence':['app.py']}]
            r=ScriptedProvider('reviewer',[plan(first),
                result('F1','changes_required',gaps=['app.py: invalid data must be checked']),
                {**result('F1','accepted',next_task=second),'verdict':{
                    **result('F1','accepted',next_task=second)['verdict'],
                    'issue_resolutions':[{'id':'AUD-1','summary':'Verified malformed input behavior','evidence':['app.py']}]}},
                final])
            report=self.run_monitor(root,w,r,max_rounds=3)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(w.calls),len(r.calls)),(3,4))
            self.assertNotIn('F2',w.calls[0]['prompt'])
            self.assertIn('invalid data must be checked',w.calls[1]['prompt'])
            self.assertNotIn('invalid data must be checked',w.calls[2]['prompt'])
            self.assertTrue(all('ORIGINAL BACKLOG' not in call['prompt'] for call in w.calls))
            self.assertTrue(all(call['read_only'] for call in r.calls))
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual([i['task']['id'] for i in state['function_tasks']['accepted']],['F1','F2'])
            self.assertTrue((root/'run/003-check-01.log').exists())
            self.assertTrue((root/'run/001-check-01.log').exists())
            self.assertEqual(state['function_tasks']['accepted'][0]['verification'], {'check:1':'002-check-01.log'})

    def test_missing_function_or_unaccepted_dependency_never_launches_worker(self):
        for response in [{'verdict':{},'text':json.dumps(BRIEF)},plan(task('F2','app.py',depends=['F1']))]:
            with tempfile.TemporaryDirectory() as d:
                w=ScriptedProvider('worker',[]);r=ScriptedProvider('reviewer',[response,response])
                report=self.run_monitor(Path(d),w,r,max_rounds=1)
                self.assertEqual(report.status,'action_required',report.summary)
                self.assertEqual(w.calls,[])

    def test_reviewer_approval_authorizes_delivery_with_an_incomplete_worker_report(self):
        with tempfile.TemporaryDirectory() as d:
            w=FunctionWorker('worker',[{**result('F1','incomplete',gaps=['unfinished']),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',next_task=task('F2','next.py',depends=['F1']))])
            report=self.run_monitor(Path(d),w,r,max_rounds=1,quality_policy={'review_changed_files':1})
            self.assertEqual(report.status,'max_rounds',report.summary)
            state=json.loads((Path(d)/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'],'F2')
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['id'],'F1')
            self.assertEqual((len(w.calls),len(r.calls)),(1,2))

    def test_checkpoint_keeps_next_function_across_resume(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);second=task('F2','next.py',depends=['F1'],scope='requirement')
            w=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted',next_task=second)])
            s=Supervisor(RunConfig('task',root,w,reviewer=r,workflow='review-first',function_dispatch=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0,checks=[CHECK]),log=lambda _:None)
            emit=s._event
            def stop(kind,**data):
                emit(kind,**data)
                if kind=='function_assigned' and data['task_id']=='F2':s.request_stop()
            s._event=stop
            self.assertEqual(s.run().status,'stopped')
            w=FunctionWorker('worker',[{**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            r=ScriptedProvider('reviewer',[result('F2','accepted',status='done',file='next.py')])
            report=Supervisor(RunConfig('',root,w,reviewer=r,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertIn('Implement F2',w.calls[0]['prompt'])
            self.assertNotIn('Implement F1',w.calls[0]['prompt'])
            self.assertEqual(len(r.calls),1)


    def test_runtime_handoff_records_roles_and_current_function(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=task('F1','app.py');second=task('F2','next.py')
            w=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(first),result('F1','accepted',next_task=second)])
            s=Supervisor(RunConfig('task',root,w,reviewer=r,workflow='review-first',function_dispatch=True,
                git_history='off',run_dir=root/'run',max_runtime_s=600,retry_delay_s=0,checks=[CHECK]),log=lambda _:None)
            emit=s._event
            def advance_clock(kind,**data):
                emit(kind,**data)
                if kind=='function_accepted':s._elapsed=lambda:580
            s._event=advance_clock
            report=s.run()
            self.assertEqual(report.status,'budget_exceeded',report.summary)
            saved=json.loads((root/'run/report.json').read_text())
            d=saved['delivery_diagnosis']
            self.assertEqual(d['code'],'candidate_delivered_remaining_work_pending')
            self.assertEqual(d['current_function']['id'],'F2')
            self.assertEqual((d['worker']['task_id'],d['reviewer']['task_id']),('F1','F1'))
            self.assertEqual(d['last_call']['role'],'reviewer')
            self.assertEqual(len(w.calls),1)

    def test_empty_check_catalog_still_requires_a_valid_reviewer_test_plan(self):
        with tempfile.TemporaryDirectory() as d:
            worker=ScriptedProvider('worker', [])
            reviewer=ScriptedProvider('reviewer', [plan(task('F1','app.py')) for _ in range(2)])
            report = self.run_monitor(Path(d), worker, reviewer, checks=[])
            self.assertEqual(report.status, 'action_required')
            self.assertIn('approved check IDs or auto:N', report.summary)
            self.assertFalse(worker.calls)

    def test_reviewer_declared_tests_run_without_any_user_supplied_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            assignment=task('F1','app.py',scope='requirement')
            assignment['criterion_ids']=['main:1','req:1:1','req:1:2']
            assignment['test_files']=['test_app.py'];assignment['check_ids']=['auto:1']
            test_source=('import unittest\nfrom app import parse_input\n'
                         'class Behavior(unittest.TestCase):\n'
                         ' def test_real_behavior(self): self.assertEqual(parse_input(),42)\n')
            worker=FunctionWorker('worker',[{**result('F1','done',status='done'),
                'writes':{'app.py':'def parse_input(): return 42\n','test_app.py':test_source}}])
            reviewer=ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,checks=[],max_rounds=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            saved=json.loads((root/'run/state.json').read_text())
            self.assertIn('auto:1',saved['function_tasks']['accepted'][0]['verification'])
            self.assertIn('test_real_behavior',(root/'run/001-check-01.log').read_text())

    def test_final_review_approval_does_not_call_worker_for_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            assignment=task('F1','app.py',scope='requirement')
            assignment['criterion_ids']=['main:1','req:1:1','req:1:2']
            incomplete=result('F1','incomplete',gaps=['Historical watcher configuration failure'])
            worker=FunctionWorker('worker',[{**incomplete,'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=1,review_every=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual([c['read_only'] for c in worker.calls],[False])
            self.assertEqual(len(reviewer.calls),2)

    def test_declared_regression_failure_is_not_hidden_by_passing_fixed_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            assignment=task('F1','app.py');assignment['test_files']=['test_app.py']
            worker=FunctionWorker('worker',[{**result('F1','done'),
                'writes':{'app.py':'def parse_input(): return 41\n',
                    'test_app.py':'import unittest\nfrom app import parse_input\nclass Behavior(unittest.TestCase):\n def test_result(self): self.assertEqual(parse_input(),42)\n'}}])
            reviewer=ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted'),result('F1','accepted')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            saved=json.loads((root/'run/state.json').read_text())
            self.assertFalse(saved['function_tasks']['accepted'])
            self.assertEqual(saved['function_tasks']['current']['id'],'F1')
            self.assertIn('41 != 42',(root/'run/001-check-02.log').read_text())

    def test_accepted_local_tests_are_reloaded_and_rerun_at_final_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'project';root.mkdir();counter=parent/'runs.txt'
            first=task('F1','app.py');first['test_files']=['test_app.py'];first['check_ids']=['auto:1']
            test_source=('import unittest\nfrom pathlib import Path\nfrom app import parse_input\n'
                         'class Behavior(unittest.TestCase):\n def test_result(self):\n'
                         '  self.assertEqual(parse_input(),42)\n'
                         f'  p=Path({str(counter)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n')
            worker=FunctionWorker('worker',[{**result('F1','done'),
                'writes':{'app.py':'def parse_input(): return 42\n','test_app.py':test_source}}])
            reviewer=ScriptedProvider('reviewer',[plan(first),result('F1','accepted')])
            process=Supervisor._process_control
            def stop_after_acceptance(monitor, active=False):
                if not active and monitor.function_tasks.accepted:
                    (root/'run/control.json').write_text(json.dumps({'action':'stop'}))
                return process(monitor,active)
            with patch.object(Supervisor,'_process_control',stop_after_acceptance):
                report=self.run_monitor(root,worker,reviewer)
            self.assertEqual(report.status,'stopped',report.summary)
            self.assertEqual(counter.read_text(),'1')
            (root/'run/control.json').unlink()
            final=task('F2','next.py',scope='requirement',depends=['F1'])
            final['criterion_ids']=['main:1','req:1:1','req:1:2']
            worker=FunctionWorker('worker',[{**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(final),result('F2','accepted',status='done',file='next.py')])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(counter.read_text(),'2')

    def test_automatic_checks_allow_final_read_only_verification_without_manual_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            first=task('F1','app.py');first['test_files']=['test_app.py'];first['check_ids']=['auto:1']
            final=verification_task(depends=['F1']);final['check_ids']=['regression:1']
            source='import unittest\nfrom app import parse_input\nclass Test(unittest.TestCase):\n def test_real(self): self.assertEqual(parse_input(),42)\n'
            worker=FunctionWorker('worker',[{**result('F1','done'),
                'writes':{'app.py':'def parse_input(): return 42\n','test_app.py':source}},
                result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(first),result('F1','accepted',next_task=final),
                result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,checks=[],max_rounds=2)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual([c['read_only'] for c in worker.calls],[False,True])
            state=json.loads((root/'run/state.json').read_text())
            self.assertIn('regression:1',state['function_tasks']['accepted'][-1]['verification'])

    def test_portable_swift_regression_path_is_accepted_as_a_new_test(self):
        from auto_moniter.function_gate import validate_task
        with tempfile.TemporaryDirectory() as directory:
            assignment=task('F1','app.py')
            assignment['test_files']=['StockTianjiApp/PortableTests/QuoteObservationFieldRegression/main.swift']
            validate_task(assignment,Path(directory),{'main':{'acceptance':['works']}},[CHECK],BRIEF)

    def test_missing_symbol_or_outside_edit_cannot_advance(self):
        for source, reason in [('unrelated = 1\n', 'Implement or change'),
                ('outside = 2\ndef parse_input():\n    return 42\n', 'Restore edits outside')]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / 'app.py').write_text('outside = 1\ndef parse_input():\n    return 0\n')
                worker = FunctionWorker('worker', [{**result('F1','done'), 'source': source}])
                reviewer = ScriptedProvider('reviewer', [plan(task('F1','app.py')), result('F1','accepted')])
                self.run_monitor(root, worker, reviewer, max_rounds=1)
                state = json.loads((root/'run/state.json').read_text())
                self.assertEqual(state['function_tasks']['accepted'], [])
                self.assertIn(reason, state['rounds'][-1]['watcher_result']['reasons'][0])
                self.assertFalse((root/'run/001-check-01.log').exists())

    def test_regression_failure_requires_explicit_repair_before_same_assignment_runs_again(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worker = FunctionWorker('worker', [
                {**result('F1','done'), 'source': 'def parse_input():\n    return 0\n'},
                {**result('F1','done'), 'source': 'def parse_input():\n    return 42\n'}])
            reviewer = ScriptedProvider('reviewer', [plan(task('F1','app.py')),
                result('F1','accepted'), result('F1','accepted',next_task=task('F2','next.py'))])
            report = self.run_monitor(root, worker, reviewer, max_rounds=2,
                checks=["python3 -c 'from app import parse_input; assert parse_input() == 42'"])
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,3))
            blocked = json.loads((root/'run/state.json').read_text())
            self.assertEqual(blocked['function_tasks']['current']['id'],'F1')
            self.assertEqual(blocked['function_tasks']['accepted'],[])
            # An explicit source repair invalidates the prior approval. Both
            # implementation assessment and independent review must run again.
            (root/'app.py').write_text('def parse_input():\n    return 42\n')
            worker = FunctionWorker('worker',[{**result('F1','done'),'source':(root/'app.py').read_text()}])
            reviewer = ScriptedProvider('reviewer',[result('F1','accepted',next_task=task('F2','next.py'))])
            report = Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,1))
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'], 'F2')
            self.assertEqual(state['function_tasks']['accepted'][0]['verification'], {'check:1':'002-check-01.log'})

    def test_premature_final_plan_never_launches_worker(self):
        with tempfile.TemporaryDirectory() as d:
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py', scope='requirement')) for _ in range(2)])
            report = self.run_monitor(Path(d), worker, reviewer)
            self.assertEqual(report.status, 'action_required')
            self.assertIn('uncovered criteria: req:1:1', report.summary)
            self.assertFalse(worker.calls)

    @unittest.skipUnless(shutil.which('node'), 'Node required for inline JavaScript scope verification')
    def test_inline_javascript_scope_checks_the_named_function(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = '<p>保留 😀</p><script>function parse_input(){return 0;} function other(){return 1;}</script>'
            (root / 'app.html').write_text(old)
            worker = FunctionWorker('worker', [
                {**result('F1','done',file='app.html'), 'touch':'app.html', 'source':old.replace('return 1', 'return 2')},
                {**result('F1','done',file='app.html'), 'touch':'app.html', 'source':old.replace('return 0', 'return 42')}])
            reviewer = ScriptedProvider('reviewer', [plan(task('F1','app.html')),
                result('F1','accepted',file='app.html'), result('F1','accepted',file='app.html')])
            self.run_monitor(root, worker, reviewer, max_rounds=2,
                checks=["python3 -c 'from pathlib import Path; assert \"return 42\" in Path(\"app.html\").read_text()'"])
            state = json.loads((root/'run/state.json').read_text())
            self.assertIn('Implement or change the assigned function', worker.calls[1]['prompt'])
            self.assertEqual(state['function_tasks']['accepted'][0]['verification'], {'check:1':'002-check-01.log'})

    @unittest.skipUnless(importlib.util.find_spec('tree_sitter_language_pack'),
                         'Install auto-moniter[languages] for tree-sitter scope verification')
    def test_registered_language_scope_is_selected_from_file_extension(self):
        from auto_moniter.function_gate import target_state
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = 'package api\n\nfunc Run() int { return 1 }\n'
            path = root / 'api.go'
            path.write_text(source)
            before = target_state(path, 'Run')
            path.write_text(source.replace('return 1', 'return 42'))
            after = target_state(path, 'Run')
            self.assertNotEqual(before['body'], after['body'])
            self.assertEqual(before['outside'], after['outside'])


class IntegrationDispatchTests(unittest.TestCase):
    CHECK = "python3 -c 'from client import load_items; assert load_items() == [42]'"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'app.py').write_text('def list_items():\n    return [1]\n')
        (self.root / 'client.py').write_text('from app import list_items\n\ndef load_items():\n    return list_items()\n')
        self.modules = _verdict('continue')['modules']
        self.scope = {'id':'pagination', 'module_id':'main', 'check_ids':['check:1'],
                      'targets':[{'file':'app.py','symbol':'list_items'},
                                 {'file':'client.py','symbol':'load_items'}]}

    def assignment(self, **overrides):
        item = task('I1', 'app.py')
        del item['file'], item['symbol']
        return {**item, 'kind':'integration', 'integration_scope':'pagination',
                'targets':self.scope['targets'], 'atomic_reason':'The client expects the new response structure',
                **overrides}

    def response(self, decision, **kwargs):
        outcome = result('I1', decision, **kwargs)
        outcome['verdict']['evidence'] = ['app.py', 'client.py']
        return outcome

    def monitor(self, worker, reviewer, *, approved=True, resume=False, run_dir=None, git_history='off', **kwargs):
        return Supervisor(RunConfig('' if resume else 'Paginate the client and server together', self.root,
            worker, reviewer=reviewer, workflow='review-first', function_dispatch=not resume, git_history=git_history,
            run_dir=run_dir or self.root/'run', review_every=0, retry_delay_s=0, resume=resume,
            modules=[] if resume else self.modules, checks=[] if resume else [self.CHECK],
            quality_policy={'integration_scopes':[self.scope]} if approved and not resume else {}, **kwargs),
            log=lambda _:None)

    def state(self):
        return json.loads((self.root/'run/state.json').read_text())

    def test_partial_extra_and_failed_regression_keep_the_same_assignment(self):
        server = 'def list_items():\n    return {"items": [42]}\n'
        prefix = 'from app import list_items\n\ndef load_items():\n'
        client = prefix + '    return list_items()["items"]\n'
        worker = FunctionWorker('worker', [
            {**self.response('done'), 'writes':{'app.py':server}},
            {**self.response('done'), 'writes':{'client.py':client, 'third.py':'extra = True\n'}},
            {**self.response('done'), 'writes':{'client.py':prefix + '    return []\n', 'third.py':None}},
            {**self.response('done'), 'writes':{'client.py':client}},
        ])
        next_task = task('F2','next.py',depends=['I1'],scope='requirement')
        reviewer = ScriptedProvider('reviewer', [plan(self.assignment()), self.response('accepted'),
            self.response('accepted'), self.response('accepted'), self.response('accepted', next_task=next_task)])
        report = self.monitor(worker, reviewer, max_rounds=4).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(3,5))
        self.assertIn('assigned source file did not change: client.py',worker.calls[1]['prompt'])
        self.assertIn('Restore changes outside this assignment: third.py',worker.calls[2]['prompt'])
        self.assertEqual(self.state()['function_tasks']['accepted'],[])
        self.assertEqual(self.state()['function_tasks']['current']['id'],'I1')
        (self.root/'client.py').write_text(client)
        worker = FunctionWorker('worker',[{**self.response('done'),'writes':{'client.py':client}}])
        reviewer = ScriptedProvider('reviewer',[self.response('accepted',next_task=next_task)])
        report = self.monitor(worker,reviewer,resume=True,max_rounds=4).run()
        self.assertEqual(report.status,'max_rounds',report.summary)
        ledger = self.state()['function_tasks']
        self.assertEqual(ledger['current']['id'],'F2')
        self.assertEqual([a['task']['id'] for a in ledger['accepted']],['I1'])
        self.assertEqual(set(ledger['accepted'][0]['target_hashes']), {'app.py','client.py'})
        self.assertEqual(ledger['accepted'][0]['verification'], {'check:1':'004-check-01.log'})
        self.assertEqual(sorted(p.name for p in (self.root/'run').glob('*-check-*.log')),
                         ['003-check-01.log','004-check-01.log'])

    def test_missing_or_mismatched_approval_never_starts_worker(self):
        cases = [(False, {}), (True, {'targets':[self.scope['targets'][0],
                 {'file':'client.py','symbol':'another_function'}]}), (True, {'check_ids':['check:2']})]
        for i, (approved, overrides) in enumerate(cases):
            with self.subTest(approved=approved, overrides=overrides):
                worker = FunctionWorker('worker', [])
                reviewer = ScriptedProvider('reviewer', [plan(self.assignment(**overrides)) for _ in range(2)])
                report = self.monitor(worker, reviewer, approved=approved, run_dir=self.root/f'run-{i}').run()
                self.assertEqual(report.status, 'action_required', report.summary)
                self.assertIn('matching preapproved scope', report.summary)
                self.assertFalse(worker.calls)

    def test_resume_keeps_original_baseline_and_approved_targets(self):
        assignment = self.assignment(delivery_scope='requirement', criterion_ids=['main:1','req:1:1','req:1:2'])
        worker = FunctionWorker('worker', [{**self.response('done',status='done'),
            'writes':{'app.py':'def list_items():\n    return {"items": [42]}\n'}}])
        reviewer = ScriptedProvider('reviewer', [plan(assignment), self.response('accepted',status='done')])
        monitor = self.monitor(worker, reviewer)
        emit = monitor._event
        def stop_on_rejection(kind, **payload):
            emit(kind, **payload)
            if kind == 'function_gate_rejected':
                monitor.request_stop()
        monitor._event = stop_on_rejection
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.state()['function_tasks']['accepted'], [])

        worker = FunctionWorker('worker', [{**self.response('done',status='done'),
            'writes':{'client.py':'from app import list_items\n\ndef load_items():\n    return list_items()["items"]\n'}}])
        reviewer = ScriptedProvider('reviewer', [self.response('accepted',status='done')])
        report = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'done', report.summary)
        entry, = self.state()['function_tasks']['accepted']
        self.assertEqual(entry['task']['id'], 'I1')
        self.assertEqual(set(entry['target_hashes']), {'app.py','client.py'})
        self.assertEqual(entry['verification'], {'check:1':'002-check-01.log'})

    def test_final_integration_publishes_both_files_in_one_commit(self):
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()
        git('init','-q')
        git('add','app.py','client.py')
        git('-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','initial')
        class GitReviewer(ScriptedProvider):
            def _command(self, session_id, prompt_file, read_only):
                match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt_file.read_text())
                if match:
                    self.scripts[0]['verdict']['evidence'].append(match[1])
                return super()._command(session_id, prompt_file, read_only)
        assignment = self.assignment(delivery_scope='requirement', criterion_ids=['main:1','req:1:1','req:1:2'])
        worker = FunctionWorker('worker',[{**self.response('done',status='done'), 'writes':{
            'app.py':'def list_items():\n    return {"items": [42]}\n',
            'client.py':'from app import list_items\n\ndef load_items():\n    return list_items()["items"]\n'}}])
        reviewer = GitReviewer('reviewer',[plan(assignment),self.response('accepted',status='done')])
        report = self.monitor(worker, reviewer, git_history='required').run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(git('rev-list','--count','HEAD'),'2')
        self.assertEqual(set(git('diff-tree','--no-commit-id','--name-only','-r','HEAD').splitlines()),
                         {'app.py','client.py'})


class DeliveryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.w={'role':'worker','index':2,'tree':'tree','scope':'module','module_ids':['M'],'status':'done'}
        self.r={**self.w,'role':'reviewer','status':'continue'}

    def test_stale_or_different_scope_is_not_disagreement(self):
        for other in [{'index':1},{'tree':'older'},{'module_ids':['N']},{'scope':'requirement'}]:
            self.assertEqual(diagnose([self.w,{**self.r,**other}])['code'],'no_comparable_review')

    def test_comparable_claims_and_watcher_result_are_separate(self):
        self.assertEqual(diagnose([self.w,self.r])['code'],'worker_reviewer_disagree')
        self.assertEqual(diagnose([{**self.w,'status':'continue'},self.r])['code'],'both_incomplete')
        self.assertEqual(diagnose([self.w,{**self.r,'status':'done'}])['code'],'joint_claim_delivery_unconfirmed')
        check={'round':2,'tree':'tree','checks':[{'status':'check_failed','exit_code':1}]}
        self.assertEqual(diagnose([self.w,{**self.r,'status':'done'}],acceptance=check)['code'],'watcher_gate_rejected')
        self.assertEqual(diagnose([self.w,{**self.r,'status':'done'}],acceptance={**check,'round':1})['code'],
                         'joint_claim_delivery_unconfirmed')
