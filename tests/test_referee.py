"""The exceptional Referee recovers scheduling, never substitutes for acceptance."""
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import referee
from auto_moniter.storage import write_json
from auto_moniter.local_tests import command as local_test_command
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task, verification_task
from tests.test_review_first import approve_requirement_coverage
from tests.test_supervisor import ScriptedProvider


def decision(action, **extra):
    return {'verdict': {}, 'text': json.dumps({
        'action': action, 'summary': 'Recover the interrupted original plan',
        'instructions': 'Preserve the candidate and accepted work; diagnose the saved failure before continuing.',
        'evidence': ['Watcher incident and saved round log'], **extra})}


def planning(first, remaining=None):
    response = plan(first)
    body = json.loads(response['text'])
    body['function_plan'] = remaining or [first]
    response['text'] = json.dumps(body)
    return response


def review_failure(ident='F1'):
    return {**result(ident, 'changes_required'), 'exit_code': 1, 'text': 'connection interrupted'}


class RefereeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'project'
        self.root.mkdir()
        self.run_dir = self.root / 'run'
        (self.root / 'app.py').write_text('def parse_input(): return 0\n')
        environment = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(Path(temporary.name) / 'private')})
        environment.start()
        self.addCleanup(environment.stop)

    def monitor(self, worker, reviewer, **options):
        settings = dict(referee=True, workflow='review-first', function_dispatch=True,
            full_function_plan=True, git_history='off', run_dir=self.run_dir,
            checks=[CHECK], retry_delay_s=0, max_rounds=5, review_every=100)
        settings.update(options)
        return Supervisor(RunConfig('Implement the original requirement completely', self.root,
            worker, reviewer=reviewer, **settings), log=lambda _: None)

    def worker(self):
        return FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'touch': 'app.py'}])

    def journal(self):
        return referee.Journal(self.run_dir).read()['decisions']

    def git_tree_scenario(self):
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

        git('init', '-q')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@local')
        template = ('import unittest\nfrom app import parse_input\n'
                    'class Behavior(unittest.TestCase):\n'
                    '    def test_value(self):\n'
                    '        print(f"observed={parse_input()}")\n'
                    '        self.assertIn(parse_input(), [0, 42])\n')
        for name in ('test_behavior.py', 'test_registered.py'):
            (self.root / name).write_text(template)
        git('add', '.')
        git('commit', '-qm', 'registered candidate')
        tree = git('rev-parse', 'HEAD^{tree}')
        (self.root / 'app.py').write_text('def parse_input(): return 42\n')
        git('commit', '-qam', 'current workspace')
        head = git('rev-parse', 'HEAD')
        checks = ['git diff --check', local_test_command(self.root, 'test_behavior.py'),
                  local_test_command(self.root, 'test_registered.py') + ' --git-tree ' + shlex.quote(tree)]
        task = {**verification_task('VERIFY'), 'delivery_scope': 'function',
                'check_ids': ['check:1', 'check:2', 'check:3']}
        final = {**verification_task('FINAL', depends=['VERIFY']), 'check_ids': ['check:1', 'check:2']}
        requests = [{'check_id': 'check:1', 'git_tree': tree, 'base': head},
                    {'check_id': 'check:2', 'git_tree': tree, 'base': ''}]
        worker = FunctionWorker('worker', [result('VERIFY', 'done')])
        return worker, planning(task, [task, final]), checks, requests

    def assert_candidate_resumed(self, report, worker, reviewer, requests):
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        candidate = next(row for row in report.records if row.role == 'worker')
        receipt = candidate.watcher_result['candidate_checks']
        self.assertTrue(receipt['passed'], receipt)
        self.assertEqual([row.index for row in report.records if row.role == 'reviewer'], [1, 1])
        for request, check in zip(requests, receipt['checks'][-2:]):
            self.assertIn(request['git_tree'], check['command'])
            self.assertIn(request['check_id'], reviewer.calls[-1]['prompt'])
            self.assertIn(check['log'], reviewer.calls[-1]['prompt'])
        self.assertIn('observed=42', (self.run_dir / '001-check-02.log').read_text())
        self.assertIn('observed=0', (self.run_dir / '001-check-05.log').read_text())
        self.assertIn('source_verified=true', (self.run_dir / '001-check-05.log').read_text())
        self.assertIn('return 42', (self.root / 'app.py').read_text())

    def test_reviewer_schedules_approved_checks_on_exact_tree(self):
        worker, initial, checks, requests = self.git_tree_scenario()
        requested = result('VERIFY', 'changes_required', gaps=['候选缺少正式回执'])
        requested['verdict']['git_tree_checks'] = requests
        reviewer = ScriptedProvider('reviewer', [initial, requested, result('VERIFY', 'accepted')])
        report = self.monitor(worker, reviewer, checks=checks, git_history='required', max_rounds=1).run()
        self.assert_candidate_resumed(report, worker, reviewer, requests)

    def test_referee_corrects_unauthorized_tree_then_schedules_local_checks(self):
        worker, initial, checks, requests = self.git_tree_scenario()
        requested = result('VERIFY', 'changes_required')
        requested['verdict']['blockers'] = ['候选缺少正式回执']
        unauthorized = [{**requests[1], 'git_tree': 'a' * 40}]
        reviewer = ScriptedProvider('reviewer', [initial, requested,
            decision('run_git_tree_checks', git_tree_checks=unauthorized),
            decision('run_git_tree_checks', git_tree_checks=requests), result('VERIFY', 'accepted')])
        monitor = self.monitor(worker, reviewer, checks=checks, git_history='required', max_rounds=1)
        report = monitor.run()
        self.assert_candidate_resumed(report, worker, reviewer, requests)
        self.assertEqual(self.journal()[-1]['status'], 'applied')
        self.assertIn('未批准的检查、候选树或业务基线', reviewer.calls[-2]['prompt'])
        self.assertFalse((self.run_dir / 'field-handoff.json').exists())

    def test_duplicate_tree_check_does_not_run_again(self):
        worker, initial, checks, requests = self.git_tree_scenario()
        requested = result('VERIFY', 'changes_required', gaps=['候选缺少正式回执'])
        requested['verdict']['git_tree_checks'] = [{**requests[1], 'check_id': 'check:3'}]
        reviewer = ScriptedProvider('reviewer', [initial, requested])
        report = self.monitor(worker, reviewer, checks=checks, git_history='required', referee=False).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('已在候选树', report.summary)
        self.assertFalse((self.run_dir / '001-check-04.log').exists())

    def test_resume_uses_captured_git_tree_decision_without_calling_referee_again(self):
        worker, initial, checks, requests = self.git_tree_scenario()
        requested = result('VERIFY', 'changes_required')
        requested['verdict']['blockers'] = ['候选缺少正式回执']
        reviewer = ScriptedProvider('reviewer', [initial, requested,
            decision('run_git_tree_checks', git_tree_checks=requests)])
        options = dict(checks=checks, git_history='required', max_rounds=1)
        monitor = self.monitor(worker, reviewer, **options)
        with patch.object(monitor, '_apply_recovery_decision', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                monitor.run()
        self.assertEqual(self.journal()[-1]['status'], 'captured')
        self.assertFalse((self.run_dir / '001-check-04.log').exists())
        resumed_worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [result('VERIFY', 'accepted')])
        report = self.monitor(resumed_worker, reviewer, resume=True, **options).run()
        self.assert_candidate_resumed(report, worker, reviewer, requests)
        self.assertEqual(resumed_worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(self.journal()[-1]['status'], 'applied')

    def test_blocked_planning_can_restore_approved_assignment_to_complete_its_checks(self):
        worker, initial, checks, requests = self.git_tree_scenario()
        requested = result('VERIFY', 'changes_required')
        requested['verdict']['blockers'] = ['候选缺少正式回执']
        options = dict(checks=checks, git_history='required', max_rounds=1)
        requested['verdict']['field_handoffs'] = [
            {'kind': 'observation', 'title': '等待正式回执', 'owner': 'Watcher',
             'evidence_needed': '指定候选树的完整检查记录'}]
        reviewer = ScriptedProvider('reviewer', [initial, requested])
        report = self.monitor(worker, reviewer, **options).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        state_file = self.run_dir / 'state.json'
        state = json.loads(state_file.read_text())
        state['requirements_review']['status'] = 'failed'
        write_json(state_file, state)

        blocked = json.loads(initial['text'])
        blocked.update(status='needs_input', blockers=['候选缺少正式回执'], next_task=None, function_plan=[])
        resumed_worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [
            *([{'verdict': {}, 'text': json.dumps(blocked)}] * 2),
            decision('run_git_tree_checks', git_tree_checks=requests), result('VERIFY', 'accepted')])
        report = self.monitor(resumed_worker, reviewer, resume=True, **options).run()
        self.assert_candidate_resumed(report, worker, reviewer, requests)
        self.assertFalse(resumed_worker.calls)
        self.assertEqual(self.journal()[-1]['incident'], 'planning')

    def test_successful_task_never_invokes_referee(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))
        self.assertFalse((self.run_dir / 'referee-decisions.json').exists())

    def test_stale_read_only_candidate_returns_to_planning_before_running_checks(self):
        original = {**verification_task('VERIFY'), 'delivery_scope': 'function'}
        final = verification_task('FINAL', depends=['VERIFY'])
        blocked = result('VERIFY', 'changes_required')
        blocked['verdict']['blockers'] = ['需要现场测量']
        monitor = self.monitor(FunctionWorker('worker', [result('VERIFY', 'done')]),
            ScriptedProvider('reviewer', [planning(original, [original, final]), blocked]))
        self.assertEqual(monitor.run().status, 'action_required')
        baseline = self.run_dir / 'verification-baseline-1-0-VERIFY.json'
        original_binding = baseline.read_bytes()
        original_log = (self.run_dir / '001-check-01.log').read_bytes()
        (self.root / 'app.py').write_text('def parse_input(): return 42\n')

        stale = {**original, 'id': 'VERIFY-OLDER'}
        older_binding = self.run_dir / 'verification-baseline-1-0-VERIFY-OLDER.json'
        older_binding.write_bytes(original_binding)
        revised = {**original, 'id': 'VERIFY-CURRENT'}
        final = {**final, 'depends_on': [revised['id']]}
        worker = FunctionWorker('worker', [result(revised['id'], 'done')])
        reviewer = ScriptedProvider('reviewer', [decision('replan'),
            planning(stale, [stale, {**final, 'depends_on': [stale['id']]}]),
            planning(revised, [revised, final]), approve_requirement_coverage(),
            result(revised['id'], 'accepted')])
        monitor = self.monitor(worker, reviewer, max_rounds=2, resume=True)
        report = monitor.run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertTrue(worker.calls[0]['read_only'])
        self.assertEqual(monitor.function_tasks.accepted[-1]['task']['id'], revised['id'])
        self.assertEqual(baseline.read_bytes(), original_binding)
        self.assertEqual(older_binding.read_bytes(), original_binding)
        self.assertEqual((self.run_dir / '001-check-01.log').read_bytes(), original_log)
        rebound = json.loads((self.run_dir / 'verification-baseline-1-0-VERIFY-CURRENT.json').read_text())
        self.assertNotEqual(rebound['tree'], json.loads(original_binding)['tree'])
        self.assertTrue((self.run_dir / '002-check-01.log').is_file())
        self.assertEqual(self.journal()[-1]['decision']['action'], 'replan')
        self.assertIn('只读派单 VERIFY-OLDER 的工作区与其原始候选不一致', reviewer.calls[2]['prompt'])

    def test_referee_replans_when_supplemental_test_lacks_approval(self):
        (self.root / 'test_export.py').write_text('raise AssertionError("Unapproved test ran")\n')
        first = verification_task('VERIFY')
        revised = {**first, 'id': 'VERIFY-RECHECK'}
        requested = result(first['id'], 'changes_required')
        requested['verdict'].update(
            blockers=['需要专项回执'],
            supplemental_checks=[{'path': 'test_export.py', 'criterion_id': 'main:1', 'audit_id': 'AUD-1'}])
        worker = FunctionWorker('worker', [result(item['id'], 'done', status='done') for item in (first, revised)])
        reviewer = ScriptedProvider('reviewer', [planning(first), requested, decision('replan'),
            planning(revised), approve_requirement_coverage(), result(revised['id'], 'accepted', status='done')])
        monitor = self.monitor(worker, reviewer)
        monitor.audit.record(['Need verified export evidence'], 1, ['main'])
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(monitor.function_tasks.accepted[-1]['task']['id'], revised['id'])
        self.assertEqual(self.journal()[-1]['decision']['action'], 'replan')
        self.assertIn('这个测试尚未获准由 Watcher 补跑', reviewer.calls[2]['prompt'])
        self.assertFalse((self.run_dir / '001-check-02.log').exists())

    def test_referee_can_keep_started_verification_and_add_an_approved_check(self):
        original = {**verification_task('VERIFY'), 'delivery_scope': 'function'}
        final = verification_task('FINAL', depends=['VERIFY'])
        requested = result('VERIFY', 'changes_required')
        requested['verdict'].update(
            blockers=['需要专项回执'],
            supplemental_checks=[{'path': 'test_export.py', 'criterion_id': 'main:1', 'audit_id': 'AUD-1'}])
        revised = {**original, 'check_ids': ['check:1', 'check:2'],
                   'objective': 'Reworded description',
                   'verification': ['Verify the recorded output after reopening']}
        coverage = {'status': 'revise', 'summary': '终验仍缺外部证据',
                    'findings': [{'source_id': 'original', 'quote': 'original requirement', 'gap': '终验证据待补'}],
                    'independent_next_task': 'VERIFY',
                    'affected_tasks': [{'task_ids': ['FINAL'], 'evidence_needed': '终验证据', 'owner': 'Watcher'}]}
        worker = FunctionWorker('worker', [result('VERIFY', 'done')] * 2)
        reviewer = ScriptedProvider('reviewer', [planning(original, [original, final]), requested,
            decision('replan'), planning(revised, [revised, final]),
            {'verdict': {}, 'text': json.dumps(coverage)}, result('VERIFY', 'accepted')])
        monitor = self.monitor(worker, reviewer, checks=[CHECK, 'test -f app.py'])
        monitor.audit.record(['Need verified export evidence'], 1, ['main'])
        emit = monitor._event

        def stop_after_acceptance(kind, **fields):
            emit(kind, **fields)
            if kind == 'function_accepted':
                monitor.request_stop()

        with patch.object(monitor, '_event', side_effect=stop_after_acceptance):
            report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(len(worker.calls), 1)
        accepted = monitor.function_tasks.accepted[-1]['task']
        self.assertEqual(accepted['objective'], original['objective'])
        self.assertEqual(accepted['verification'], original['verification'] + revised['verification'])
        self.assertEqual(accepted['check_ids'], revised['check_ids'])
        events = (self.run_dir / 'events.jsonl').read_text()
        self.assertIn('coverage_independent_dispatch', events)
        receipt = next(row for row in report.records if row.role == 'worker').watcher_result['candidate_checks']
        self.assertEqual(receipt['context']['task'], accepted)
        self.assertTrue(receipt['passed'])
        self.assertFalse(monitor._can_continue_started_assignment(original, {**revised, 'symbol': 'other'}))
        self.assertFalse(monitor._can_continue_started_assignment(original, {**revised, 'check_ids': ['check:2']}))

    def archive_scenario(self):
        waiting = {**verification_task('VERIFY'), 'delivery_scope': 'function'}
        final = verification_task('FINAL', depends=['VERIFY'])
        blocked = result('VERIFY', 'changes_required')
        blocked['verdict'].update(blockers=['缺少当前候选的检查归档'], field_handoffs=[{
            'kind': 'observation', 'case_id': 'watcher-check-archive',
            'title': '归档已通过的检查', 'owner': 'Watcher 管理端', 'evidence_needed': '原始日志及候选指纹',
        }])
        return (FunctionWorker('worker', [result('VERIFY', 'done')]),
                [planning(waiting, [waiting, final]), blocked, decision('archive_checks')])

    def test_referee_archives_passing_checks_and_resumes_original_round(self):
        worker, responses = self.archive_scenario()
        reviewer = ScriptedProvider('reviewer', [*responses, result('VERIFY', 'accepted')])
        report = self.monitor(worker, reviewer, max_rounds=1).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual([r.index for r in report.records if r.role == 'reviewer'], [1, 1])
        self.assertIn('field-check-archive-', reviewer.calls[-1]['prompt'])
        saved = json.loads(next(self.run_dir.glob('field-check-archive-*.json')).read_text())
        self.assertEqual(saved['binding']['round'], 1)
        self.assertEqual(saved['checks'][0]['exit_code'], 0)
        self.assertEqual(list(saved['logs']), ['001-check-01.log'])
        entries = referee.Journal(self.run_dir, archive=True).read()['decisions']
        self.assertEqual([entry['status'] for entry in entries], ['applied'])

    def test_resume_applies_saved_archive_decision_without_another_referee_call(self):
        worker, responses = self.archive_scenario()
        reviewer = ScriptedProvider('reviewer', responses)
        monitor = self.monitor(worker, reviewer, max_rounds=1)
        with patch.object(monitor, '_apply_referee_archive', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                monitor.run()
        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [result('VERIFY', 'accepted')])
        report = self.monitor(worker, reviewer, max_rounds=1, resume=True).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertFalse(worker.calls)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIn('field-check-archive-', reviewer.calls[0]['prompt'])

    def test_repeated_archive_request_waits_without_repeating_the_referee(self):
        worker, responses = self.archive_scenario()
        reviewer = ScriptedProvider('reviewer', [*responses, responses[1]])
        report = self.monitor(worker, reviewer, max_rounds=1).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(reviewer.calls), 4)
        self.assertEqual(len(list(self.run_dir.glob('field-check-archive-*.json'))), 1)

    def test_console_resumes_parked_archive_once_without_a_receipt(self):
        import threading
        from auto_moniter import field_handoff
        from console.tasks import TaskStore

        worker, responses = self.archive_scenario()
        reviewer = ScriptedProvider('reviewer', responses[:2])
        report = self.monitor(worker, reviewer, referee=False).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state['referee']['enabled'] = True
        path.write_text(json.dumps(state))
        store = object.__new__(TaskStore)
        store._lock = threading.RLock()
        meta = {'id': 'test', 'managed': True, 'run_dir': str(self.run_dir)}
        store._catalog = lambda: {'test': meta}
        store._view = lambda *args, **kwargs: {'status': 'action_required', 'actions': ['resume']}
        with patch.object(store, 'action') as launch:
            store.recover_field_handoffs()
            store.recover_field_handoffs(retry_failed=False)
        launch.assert_called_once_with('test', 'resume')
        handoff = field_handoff.read(self.run_dir)
        self.assertFalse(field_handoff.ready(handoff))
        self.assertEqual(handoff['archive_resume']['status'], 'launched')

    def test_field_failure_is_acknowledged_after_the_referee_decision(self):
        from auto_moniter import field_handoff
        from auto_moniter.storage import write_json
        from console.tasks import TaskStore

        worker, responses = self.archive_scenario()
        responses[1]['verdict']['field_handoffs'][0].update(kind='browser', case_id='login-routes')
        reviewer = ScriptedProvider('reviewer', responses[:2])
        self.assertEqual(self.monitor(worker, reviewer, max_rounds=1).run().status, 'action_required')
        value = field_handoff.read(self.run_dir)
        value['failed_cases'] = {'1': {'case_id': 'login-routes', 'reason': '浏览器退出。', 'role': 'reviewer'}}
        write_json(self.run_dir / field_handoff.FILE, value)
        store = object.__new__(TaskStore)
        worker, reviewer = FunctionWorker('worker', []), ScriptedProvider('reviewer', [decision('wait')])
        with patch.object(store, 'action') as launch:
            store._route_browser_failure({'id': 'task'}, self.run_dir, value, referee_enabled=True)
            launched = field_handoff.read(self.run_dir)
            self.assertFalse(launched.get('fallback_history'))
            report = self.monitor(worker, reviewer, max_rounds=1, resume=True).run()
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertFalse(worker.calls)
            handled = field_handoff.read(self.run_dir)
            self.assertEqual(handled['fallback']['status'], 'handled')
            store._route_browser_failure({'id': 'task'}, self.run_dir, handled, referee_enabled=True)
            launch.assert_called_once_with('task', 'resume')

    def test_review_interruption_reuses_worker_candidate_and_requires_fresh_review(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure(), review_failure(),
            decision('retry_reviewer'), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 5))
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertIn('You are the task Referee', reviewer.calls[3]['prompt'])
        self.assertIn('Referee scheduling decision', reviewer.calls[4]['prompt'])
        self.assertEqual(self.journal()[0]['status'], 'applied')
        self.assertEqual([r.index for r in report.records if r.role == 'worker'], [1])
        self.assertEqual([r.index for r in report.records if r.role == 'reviewer'], [1, 1, 1])
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(saved['usage']['agent_calls'], 6)

    def test_repeated_worker_failures_get_one_directed_recovery(self):
        worker = FunctionWorker('worker', [
            {**result('F1', 'incomplete'), 'exit_code': 1},
            {**result('F1', 'incomplete'), 'exit_code': 1},
            {**result('F1', 'done', status='done'), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), decision('retry_worker'),
            result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 3))
        self.assertIn('Referee scheduling decision', worker.calls[2]['prompt'])
        self.assertEqual(len(self.journal()), 1)

    def test_no_progress_limit_can_trigger_a_bounded_referee_recovery(self):
        worker = FunctionWorker('worker', [result('F1', 'incomplete', gaps=['Need another approach']) for _ in range(4)]
            + [{**result('F1', 'done', status='done'), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), decision('retry_worker'),
            result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer, quality_policy={'no_progress_limit': 3}).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 5)
        self.assertEqual(self.journal()[0]['decision']['action'], 'retry_worker')

    def test_replan_keeps_accepted_work_and_verifies_the_interrupted_candidate(self):
        (self.root / 'a.py').write_text('def parse_input(): return 0\n')
        (self.root / 'b.py').write_text('def parse_input(): return 0\n')
        first = task('FIRST', 'a.py')
        second = final_task('SECOND', 'b.py', depends=['FIRST'])
        verification = {**verification_task('VERIFY_SECOND', 'b.py', depends=['FIRST'])}
        worker = FunctionWorker('worker', [
            {**result('FIRST', 'done', file='a.py'), 'touch': 'a.py'},
            {**result('SECOND', 'incomplete', file='b.py'), 'exit_code': 1,
             'writes': {'b.py': 'def parse_input(): return 42\n'}},
            {**result('SECOND', 'incomplete', file='b.py'), 'exit_code': 1},
            result('VERIFY_SECOND', 'done', file='b.py', status='done')])
        from tests.test_review_first import approve_requirement_coverage
        reviewer = ScriptedProvider('reviewer', [planning(first, [first, second]),
            result('FIRST', 'accepted', file='a.py'), decision('replan'), planning(verification),
            approve_requirement_coverage(), result('VERIFY_SECOND', 'accepted', file='b.py', status='done')])
        monitor = self.monitor(worker, reviewer)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual([item['task']['id'] for item in monitor.function_tasks.accepted], ['FIRST', 'VERIFY_SECOND'])
        self.assertEqual(len(worker.calls), 4)
        self.assertTrue(worker.calls[-1]['read_only'])
        self.assertEqual((self.root / 'a.py').read_text(), 'def parse_input():\n    return 42\n')
        self.assertIn('Referee has directed replanning', reviewer.calls[3]['prompt'])

    def test_identical_recovery_action_is_not_repeated_on_the_same_incident(self):
        worker = self.worker()
        turns = [planning(final_task()), *([review_failure(), review_failure(), decision('retry_reviewer')] * 2)]
        reviewer = ScriptedProvider('reviewer', [*turns, decision('retry_reviewer')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual([entry['status'] for entry in self.journal()], ['applied', 'failed'])
        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [review_failure(), review_failure()])
        report = self.monitor(worker, reviewer, resume=True, referee=None).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(worker.calls, [])
        self.assertEqual([entry['status'] for entry in self.journal()], ['applied', 'failed'])

    def test_failed_referee_is_not_reissued_blindly_on_resume(self):
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure(), review_failure(),
            {**decision('retry_reviewer'), 'exit_code': 1}])
        report = self.monitor(self.worker(), reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(self.journal()[0]['status'], 'failed')
        reviewer = ScriptedProvider('reviewer', [review_failure(), review_failure()])
        report = self.monitor(FunctionWorker('worker', []), reviewer, resume=True).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(len(self.journal()), 1)

    def test_budget_exhaustion_does_not_start_a_referee_or_increase_limits(self):
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure()])
        report = self.monitor(self.worker(), reviewer, max_agent_calls=3).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertFalse((self.run_dir / 'referee-decisions.json').exists())

    def test_authentication_failure_does_not_start_a_referee(self):
        reviewer = ScriptedProvider('reviewer', [planning(final_task()),
            {**review_failure(), 'text': '401 unauthorized'}])
        report = self.monitor(self.worker(), reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertFalse((self.run_dir / 'referee-decisions.json').exists())

    def test_referee_cannot_accept_delivery_or_request_worker_replay_after_review_transport_failure(self):
        for action in ('done', 'retry_worker'):
            with self.subTest(action=action):
                self.run_dir = self.root / ('run-' + action)
                (self.root / 'app.py').write_text('def parse_input(): return 0\n')
                worker = self.worker()
                reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure(), review_failure(), decision(action)])
                report = self.monitor(worker, reviewer).run()
                self.assertEqual(report.status, 'action_required', report.summary)
                self.assertEqual(len(worker.calls), 1)
                self.assertEqual(self.journal()[0]['status'], 'failed')

    def test_read_only_referee_writes_reject_the_decision(self):
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure(), review_failure(),
            {**decision('retry_reviewer'), 'touch': 'app.py'}])
        report = self.monitor(self.worker(), reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(self.journal()[0]['status'], 'failed')
        self.assertIn('inputs changed', self.journal()[0]['error'])

    def test_stop_during_referee_prevents_retry_and_preserves_the_stop(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure(), review_failure(),
            decision('retry_reviewer')])
        monitor = self.monitor(worker, reviewer)
        invoke = monitor._invoke

        def stopped(*args, **kwargs):
            turn = invoke(*args, **kwargs)
            if kwargs['role'] == 'referee':
                monitor.request_stop()
            return turn

        monitor._invoke = stopped
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(self.journal()[0]['status'], 'requested')
        self.assertIsNone(monitor._referee_decision)

    def test_referee_model_is_independent_and_preserved_on_resume(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), review_failure(), review_failure(),
            decision('retry_reviewer')])
        monitor = self.monitor(worker, reviewer, referee_model='independent-model')
        models = []
        invoke = monitor._invoke

        def observed(*args, **kwargs):
            if kwargs['role'] == 'referee':
                models.append(kwargs['model'])
            return invoke(*args, **kwargs)

        emit = monitor._event
        def stop_after_decision(kind, **fields):
            emit(kind, **fields)
            if kind == 'referee_decision':
                monitor.request_stop()

        monitor._invoke, monitor._event = observed, stop_after_decision
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(models, ['independent-model'])
        resumed = self.monitor(FunctionWorker('worker', []),
            ScriptedProvider('reviewer', [result('F1', 'accepted', status='done')]), resume=True, referee=None)
        report = resumed.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(resumed.config.referee_model, 'independent-model')
        self.assertEqual(len(self.journal()), 1)

    def test_replanning_cannot_remove_original_acceptance_criteria(self):
        incomplete_plan = verification_task('VERIFY')
        incomplete_plan['criterion_ids'] = ['main:1']
        worker = FunctionWorker('worker', [
            {**result('F1', 'incomplete'), 'exit_code': 1},
            {**result('F1', 'incomplete'), 'exit_code': 1}])
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), decision('replan'),
            planning(incomplete_plan), planning(incomplete_plan), decision('wait')])
        monitor = self.monitor(worker, reviewer)
        report = monitor.run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(monitor.function_tasks.accepted, [])
        self.assertEqual(monitor.requirements_review.get('status'), 'failed')

    def test_failed_checks_route_back_to_implementation_without_stale_approval(self):
        command = "python3 -c 'from app import parse_input; assert parse_input() == 42'"
        worker = FunctionWorker('worker', [
            {**result('F1', 'done', status='done'), 'source': 'def parse_input(): return 1\n'},
            {**result('F1', 'done', status='done'), 'source': 'def parse_input(): return 42\n'}])
        # A reviewer incorrectly accepting failed checks exhausts its normal
        # report correction, then the referee routes the preserved work to repair.
        reviewer = ScriptedProvider('reviewer', [planning(final_task()),
            result('F1', 'accepted', status='done'), result('F1', 'accepted', status='done'),
            decision('retry_worker'), result('F1', 'accepted', status='done')])
        monitor = self.monitor(worker, reviewer, checks=[command])
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(self.journal()), 1)
        self.assertEqual(monitor.function_tasks.accepted[-1]['round'], 2)
        self.assertEqual((self.root / 'app.py').read_text(), 'def parse_input(): return 42\n')

    def test_referee_cannot_retry_approval_of_a_failed_behavior_check(self):
        command = "python3 -c 'from app import parse_input; assert parse_input() == 42'"
        worker = FunctionWorker('worker', [
            {**result('F1', 'done', status='done'), 'source': 'def parse_input(): return 1\n'}])
        reviewer = ScriptedProvider('reviewer', [planning(final_task()),
            result('F1', 'accepted', status='done'), result('F1', 'accepted', status='done'),
            decision('retry_reviewer')])
        report = self.monitor(worker, reviewer, checks=[command]).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('behavioral assertions', self.journal()[0]['error'])
        self.assertEqual(len(worker.calls), 1)

    def test_acceptance_block_redirect_reloads_checkpoint_and_repairs_once(self):
        command = "python3 -c 'import sys; from app import parse_input; sys.exit(0 if parse_input() == 42 else 1)'"
        worker = FunctionWorker('worker', [
            {**result('F1', 'done', status='done'), 'source': 'def parse_input(): return 1\n'},
            {**result('F1', 'done', status='done'), 'source': 'def parse_input(): return 42\n'}])
        reviewer = ScriptedProvider('reviewer', [planning(final_task()),
            result('F1', 'accepted', status='done'), decision('retry_worker'),
            result('F1', 'accepted', status='done')])
        monitor = self.monitor(worker, reviewer, checks=[command])
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 4)
        self.assertEqual(monitor.function_tasks.accepted[-1]['round'], 2)
        events = [json.loads(line) for line in (self.run_dir / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(item['event'] == 'referee_decision' for item in events), 1)

    def test_default_turn_timeout_interrupts_a_stuck_worker_before_referee_recovery(self):
        class StuckWorker(FunctionWorker):
            def _command(inner, *args):
                command = super()._command(*args)
                if len(inner.calls) == 1:
                    path = Path(command.argv[-1])
                    path.write_text('import time; time.sleep(5)\n' + path.read_text())
                return command

        worker = StuckWorker('worker', [result('F1', 'incomplete'),
            {**result('F1', 'done', status='done'), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), decision('retry_worker'),
            result('F1', 'accepted', status='done')])
        with patch('auto_moniter.referee.TURN_LIMIT_S', .2):
            report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertTrue(next(item for item in report.records if item.role == 'worker').timed_out)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(self.journal()), 1)


class RefereeJournalTests(unittest.TestCase):
    def test_distinct_incidents_can_continue_past_the_old_fixed_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = referee.Journal(directory)
            for index in range(15):
                ticket = journal.begin({'tree': str(index)}, 'interrupted')
                journal.update(ticket['id'], 'applied')
            self.assertEqual(len(journal.read()['decisions']), 15)

    def test_interrupted_request_cannot_be_replayed_and_changed_inputs_get_a_new_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = referee.Journal(directory)
            self.assertIsNotNone(journal.begin({'tree': 'original'}, 'interrupted'))
            self.assertIsNone(referee.Journal(directory).begin({'tree': 'original'}, 'interrupted'))
            self.assertIsNotNone(journal.begin({'tree': 'changed'}, 'interrupted'))

    def test_corrupt_evidence_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'referee-decisions.json'
            path.write_text('{')
            with self.assertRaises(ValueError):
                referee.Journal(directory).begin({}, 'failure')
            self.assertEqual(path.read_text(), '{')
