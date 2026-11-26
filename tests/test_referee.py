"""The exceptional Referee recovers scheduling, never substitutes for acceptance."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import referee
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task, verification_task
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

    def test_successful_task_never_invokes_referee(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [planning(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))
        self.assertFalse((self.run_dir / 'referee-decisions.json').exists())

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
        reviewer = ScriptedProvider('reviewer', [planning(first, [first, second]),
            result('FIRST', 'accepted', file='a.py'), decision('replan'), planning(verification),
            result('VERIFY_SECOND', 'accepted', file='b.py', status='done')])
        monitor = self.monitor(worker, reviewer)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual([item['task']['id'] for item in monitor.function_tasks.accepted], ['FIRST', 'VERIFY_SECOND'])
        self.assertEqual(len(worker.calls), 4)
        self.assertTrue(worker.calls[-1]['read_only'])
        self.assertEqual((self.root / 'a.py').read_text(), 'def parse_input():\n    return 42\n')
        self.assertIn('Referee has directed replanning', reviewer.calls[3]['prompt'])

    def test_arbitration_exhaustion_is_durable_and_never_repeats_implementation(self):
        worker = self.worker()
        turns = [planning(final_task())]
        for _ in range(referee.MAX_INCIDENT_DECISIONS):
            turns.extend([review_failure(), review_failure(), decision('retry_reviewer')])
        turns.extend([review_failure(), review_failure()])
        reviewer = ScriptedProvider('reviewer', turns)
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(self.journal()), referee.MAX_INCIDENT_DECISIONS)
        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [review_failure(), review_failure()])
        report = self.monitor(worker, reviewer, resume=True, referee=None).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(self.journal()), referee.MAX_INCIDENT_DECISIONS)

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
