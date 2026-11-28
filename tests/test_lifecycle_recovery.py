import errno
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from auto_moniter import candidate_checks, dispatch_inputs, field_handoff, recovery_wait, referee
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.supervisor_dispatch import FunctionDispatch
from console import lifecycle
from console.tasks import TaskError, TaskStore
from tests.test_function_dispatch import FunctionWorker
from tests.test_referee import decision, final_task, planning, review_failure
from tests.test_supervisor import ScriptedProvider, _verdict
from tests.test_function_dispatch import CHECK, result, verification_task


class LifecycleRecoveryTests(unittest.TestCase):
    def test_input_wait_wakes_an_independent_ready_handoff_and_keeps_old_receipts(self):
        from auto_moniter.workspace import workspace_fingerprint

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            run.mkdir()
            store = TaskStore(root / 'console', root)
            meta = {'id': 'test', 'run_dir': str(run), 'managed': True, 'cwd': str(root)}
            store._catalog = lambda: {'test': meta}
            store._view = lambda *a, **kw: {'status': 'action_required', 'actions': ['resume']}
            current, deferred = [{**verification_task(key), 'delivery_scope': 'function'}
                                 for key in ('WAITING', 'DEFERRED')]
            tree = workspace_fingerprint(root, ignore_dir=run)
            receipt = candidate_checks.create(run, {'task': deferred, 'commands': []}, {'checks': []}, True)
            summary = '仍在等待工作区输入'
            state = {'status': 'action_required', 'cwd': str(root), 'checks': [], 'quality_policy': {},
                     'function_tasks': {'current': current, 'accepted': []}, 'summary': summary,
                     'function_plan': {'tasks': [current, deferred]}, 'rounds': [
                         {'index': 1, 'role': 'worker', 'tree': tree, 'watcher_result': {'candidate_checks': receipt}},
                         {'index': 1, 'role': 'reviewer', 'blockers': ['等待执行回执'],
                          'task_status': 'changes_required', 'watcher_result': {'status': 'blocked'}},
                     ]}
            prior = {**state, 'function_tasks': {**state['function_tasks'], 'current': deferred}}
            binding = field_handoff.binding(prior)
            revision = 'a' * 32
            filename = f'field-handoff-{revision}.json'
            request = {'binding': binding, 'revision': revision, 'id': '1',
                       'receipt': {'result': '原候选已核对'}}
            handoff = {'binding': binding, 'revision': revision, 'items': [
                {'id': '1', 'kind': 'observation', 'executor_task': 'existing-executor'},
            ]}
            write_json(run / filename, handoff)
            write_json(run / 'state.json', state)
            write_json(run / dispatch_inputs.WAIT_FILE, {'binding': dispatch_inputs.binding(current, root, [], {}, []),
                       'inputs': ['missing.json'], 'summary': summary})
            wait = recovery_wait.schedule(run, 'dispatch_inputs', 'resume', detail=summary)
            write_json(run / recovery_wait.FILE, {**wait, 'retry_at': 0})
            field_handoff.submit(run, prior, request, executor='existing-executor', filename=filename)
            with patch.object(store, 'action') as action:
                store.recover_field_handoffs()
                action.assert_called_once_with('test', 'resume', {'deferred_handoff': True})
            write_json(run / filename, handoff)
            (root / 'app.py').write_text('new workspace contents\n')
            store._validate_handoff_task = Mock(return_value={'id': 'existing-executor'})
            store._executor_field_result = Mock(return_value=request)
            store._view = lambda child, *a, **kw: (
                {'status': 'done'} if child['id'] == 'existing-executor'
                else {'status': 'action_required', 'actions': ['resume']})
            with (patch.object(store, 'action') as action,
                  patch('console.lifecycle.field_executors.bind') as bind):
                store.recover_field_handoffs()
                action.assert_not_called()
                bind.assert_not_called()
            self.assertTrue(field_handoff.ready(field_handoff.read(run, filename)))

    def test_incomplete_referee_decision_resumes_without_another_referee_call(self):
        class SimulatedCrash(BaseException):
            pass

        for failure in ('process_exit', 'session_cleanup'):
            with self.subTest(failure=failure):
                self._check_referee_resume(failure, SimulatedCrash)

    def _check_referee_resume(self, failure, crash):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            (root / 'app.py').write_text('def parse_input(): return 0\n')
            worker = FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [
                planning(final_task()), review_failure(), review_failure(), decision('retry_reviewer')])
            settings = dict(referee=True, workflow='review-first', function_dispatch=True,
                            full_function_plan=True, git_history='off', run_dir=run,
                            checks=[CHECK], retry_delay_s=0, max_rounds=5)
            monitor = Supervisor(RunConfig('Complete the original requirement', root, worker, reviewer=reviewer,
                                            **settings), log=lambda _: None)
            if failure == 'process_exit':
                with patch.object(monitor, '_apply_recovery_decision', side_effect=crash), self.assertRaises(crash):
                    monitor.run()
            else:
                with patch.object(monitor, '_close_delivered_sessions', return_value=['previous session is busy']):
                    self.assertEqual(monitor.run().status, 'recovery_failed')
                state = json.loads((run / 'state.json').read_text())
                self.assertIsNone(state['referee']['decision'])
                self.assertEqual(json.loads((run / recovery_wait.FILE).read_text())['condition'], 'referee_cleanup')
            self.assertEqual(referee.Journal(run).read()['decisions'][0]['status'], 'captured')
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [result('F1', 'accepted', status='done')])
            resumed = Supervisor(RunConfig('Complete the original requirement', root, worker, reviewer=reviewer,
                                            resume=True, **settings), log=lambda _: None).run()
            self.assertEqual(resumed.status, 'done', resumed.summary)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertFalse(worker.calls)
            self.assertEqual(referee.Journal(run).read()['decisions'][0]['status'], 'applied')

    def test_committed_delivery_resumes_cleanup_without_another_model_call(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
            settings = dict(run_dir=run, git_history='off', retry_delay_s=0)
            monitor = Supervisor(RunConfig('Deliver the original requirement', root, worker,
                                            reviewer=reviewer, **settings), log=lambda _: None)
            with patch.object(monitor, '_close_delivered_sessions', return_value=['cleanup interrupted']):
                self.assertEqual(monitor.run().status, 'recovery_failed')
            self.assertTrue((run / 'delivery-cleanup.json').is_file())
            self.assertEqual(json.loads((run / recovery_wait.FILE).read_text())['condition'], 'delivery_cleanup')
            worker = ScriptedProvider('worker', [])
            reviewer = ScriptedProvider('reviewer', [])
            monitor = Supervisor(RunConfig('Deliver the original requirement', root, worker,
                                            reviewer=reviewer, resume=True, **settings), log=lambda _: None)
            report = monitor.run()
            self.assertEqual(report.status, 'done', report.summary)
            self.assertFalse(worker.calls or reviewer.calls)
            self.assertFalse((run / recovery_wait.FILE).exists())

    def test_cleanup_rechecks_only_changed_evidence_in_both_workflows(self):
        for function_dispatch in (False, True):
            with self.subTest(function_dispatch=function_dispatch), TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / 'run'
                log = lambda _: None
                if function_dispatch:
                    worker = FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'touch': 'app.py'}])
                    reviewer = ScriptedProvider('reviewer', [planning(final_task()), result('F1', 'accepted', status='done')])
                    (root / 'app.py').write_text('def parse_input(): return 0\n')
                else:
                    worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
                    reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
                first = CHECK if function_dispatch else 'test -f app.py'
                settings = dict(run_dir=run, git_history='off', retry_delay_s=0,
                                checks=[first, 'printf second'], function_dispatch=function_dispatch)
                if function_dispatch:
                    settings.update(workflow='review-first', full_function_plan=True)
                monitor = Supervisor(RunConfig('Deliver the requirement', root, worker, reviewer=reviewer, **settings), log=log)
                with patch.object(monitor, '_close_delivered_sessions', return_value=['cleanup interrupted']):
                    self.assertEqual(monitor.run().status, 'recovery_failed')
                (run / '001-check-01.log').write_text('replaced evidence')
                second = run / '001-check-02.log'
                original_second = second.stat().st_mtime_ns
                worker, reviewer = ScriptedProvider('worker', []), ScriptedProvider('reviewer', [])
                monitor = Supervisor(RunConfig('Deliver the requirement', root, worker,
                    reviewer=reviewer, resume=True, **settings), log=log)
                report = monitor.run()
                self.assertEqual(report.status, 'done', report.summary)
                self.assertEqual(second.stat().st_mtime_ns, original_second)
                self.assertTrue((run / '001-check-01-attempt-001.log').is_file())
                evidence = json.loads((run / 'report.json').read_text())['delivery_checks']
                self.assertEqual(list(evidence['logs']), ['001-check-01-attempt-001.log', '001-check-02.log'])
                self.assertFalse(worker.calls or reviewer.calls)

    def test_failed_delivery_recheck_cannot_finalize_accepted_work(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / 'work'
            root.mkdir()
            run, gate = root / 'run', root.parent / 'gate'
            gate.touch()
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
            settings = dict(run_dir=run, git_history='off', retry_delay_s=0, checks=[f'test -f {gate}'])
            monitor = Supervisor(RunConfig('Deliver the requirement', root, worker, reviewer=reviewer, **settings),
                                 log=lambda _: None)
            with patch.object(monitor, '_close_delivered_sessions', return_value=['busy']):
                self.assertEqual(monitor.run().status, 'recovery_failed')
            (run / '001-check-01.log').unlink()
            gate.unlink()
            worker, reviewer = ScriptedProvider('worker', []), ScriptedProvider('reviewer', [])
            resumed = Supervisor(RunConfig('Deliver the requirement', root, worker, reviewer=reviewer,
                                            resume=True, **settings), log=lambda _: None).run()
            self.assertEqual(resumed.status, 'action_required', resumed.summary)
            self.assertTrue((run / '001-check-01-attempt-001.log').is_file())
            self.assertFalse(worker.calls or reviewer.calls)

    def test_unsealed_terminal_intent_and_interrupted_launch_are_reconciled(self):
        class SimulatedCrash(BaseException):
            pass

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
            monitor = Supervisor(RunConfig('Deliver the requirement', root, worker, reviewer=reviewer,
                                            run_dir=run, git_history='off', retry_delay_s=0), log=lambda _: None)
            with patch.object(monitor, '_close_delivered_sessions', return_value=['busy']), \
                 patch.object(monitor, '_schedule_lifecycle_recovery', side_effect=SimulatedCrash), \
                 self.assertRaises(SimulatedCrash):
                monitor.run()
            self.assertEqual(json.loads((run / recovery_wait.FILE).read_text())['phase'], 'prepared')
            store = object.__new__(TaskStore)
            store._view = Mock(return_value={'status': 'recovery_failed', 'actions': ['resume']})
            state = json.loads((run / 'state.json').read_text())

            def crash_after_start(*_):
                write_json(run / 'state.json', {**state, 'status': 'running', 'updated_at': 'new'})
                raise SimulatedCrash()

            store.action = Mock(side_effect=crash_after_start)
            with patch('auto_moniter.recovery_wait.time.time', return_value=10**12):
                with self.assertRaises(SimulatedCrash):
                    lifecycle.recover(store, {'id': 'task'}, run)
                recovery_wait.adopted(run)
                self.assertEqual(json.loads((run / recovery_wait.FILE).read_text())['phase'], 'adopted')
                store._view.return_value = {'status': 'interrupted', 'actions': ['resume']}
                store.action.side_effect = None
                write_json(run / 'control.json', {'action': 'pause'})
                self.assertTrue(lifecycle.recover(store, {'id': 'task'}, run))
                self.assertEqual(store.action.call_count, 1)
                (run / 'control.json').unlink()
                self.assertTrue(lifecycle.recover(store, {'id': 'task'}, run))
                self.assertEqual(store.action.call_count, 2)

    def test_orphaned_delivery_is_resumed_with_exhausted_model_budget(self):
        class SimulatedCrash(BaseException):
            pass

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
            monitor = Supervisor(RunConfig('Deliver', root, worker, reviewer=reviewer, run_dir=run,
                                            git_history='off', max_agent_calls=2), log=lambda _: None)
            with patch.object(monitor, '_close_delivered_sessions', side_effect=SimulatedCrash):
                with self.assertRaises(SimulatedCrash):
                    monitor.run()
            self.assertFalse((run / recovery_wait.FILE).exists())
            store = TaskStore(root / 'console', root)
            meta = {'id': 'a' * 32, 'run_dir': str(run), 'managed': True}
            with patch.object(store, '_get', return_value=meta), \
                 patch.object(store, '_view', return_value={'status': 'interrupted', 'actions': ['resume']}), \
                 patch.object(store, '_check_start'), patch.object(store, 'detail'), \
                 patch.object(store, '_resume_args', return_value=['run', '--cwd', str(root), '--max-agent-calls', '2']), \
                 patch.object(store, '_launch') as launch:
                self.assertTrue(lifecycle.recover(store, meta, run))
                launch.assert_called_once()
            self.assertEqual(json.loads((run / recovery_wait.FILE).read_text())['phase'], 'starting')

    def test_final_recheck_retries_when_the_check_environment_recovers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / 'work'
            root.mkdir()
            run, gate = root / 'run', root.parent / 'dependency'
            gate.touch()
            settings = dict(run_dir=run, git_history='off', retry_delay_s=0, checks=[f'test -f {gate} || exit 127'])
            worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
            monitor = Supervisor(RunConfig('Deliver', root, worker, reviewer=reviewer, **settings), log=lambda _: None)
            with patch.object(monitor, '_close_delivered_sessions', return_value=['busy']):
                self.assertEqual(monitor.run().status, 'recovery_failed')
            (run / '001-check-01.log').unlink()
            gate.unlink()
            worker, reviewer = ScriptedProvider('worker', []), ScriptedProvider('reviewer', [])

            def resume(*_):
                return Supervisor(RunConfig('Deliver', root, worker, reviewer=reviewer, resume=True, **settings),
                                  log=lambda _: None).run()

            self.assertEqual(resume().status, 'action_required')
            wait = json.loads((run / recovery_wait.FILE).read_text())
            self.assertEqual(wait['condition'], 'check_environment')
            gate.touch()
            store = object.__new__(TaskStore)
            store._view = Mock(return_value={'status': 'action_required', 'actions': ['resume']})
            report = []
            store.action = lambda *args: report.append(resume(*args))
            with patch('auto_moniter.recovery_wait.time.time', return_value=wait['retry_at'] + 1):
                self.assertTrue(lifecycle.recover(store, {'id': 'task'}, run))
            self.assertEqual(report[0].status, 'done', report[0].summary)
            self.assertFalse(worker.calls or reviewer.calls)

    def test_paused_time_is_excluded_from_runtime_and_restored_after_exit(self):
        from auto_moniter.run_timing import restore

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            run.mkdir()
            monitor = Supervisor(RunConfig('Wait for continuation', root, ScriptedProvider('worker', []),
                                            run_dir=run, git_history='off', max_runtime_s=5), log=lambda _: None)
            monitor._started = 10.0
            write_json(run / 'state.json', {})
            write_json(run / 'control.json', {'action': 'pause'})
            now = [20.0]

            def advance(_):
                now[0] += 3600
                usage = json.loads((run / 'state.json').read_text())['usage']
                self.assertTrue(usage['paused'])
                self.assertEqual(restore(usage, monitor._heartbeat), 10.0)
                self.assertEqual(monitor._elapsed(), 10.0)
                write_json(run / 'control.json', {'action': 'continue'})

            with patch('auto_moniter.run_timing.time.monotonic', side_effect=lambda: now[0]), \
                 patch.object(monitor._stop_event, 'wait', side_effect=advance):
                monitor._process_control()
                self.assertEqual(monitor._elapsed(), 10.0)
            self.assertFalse(json.loads((run / 'state.json').read_text())['usage']['paused'])

    def test_start_wait_is_durable_and_retries_after_release(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            store = object.__new__(TaskStore)
            store._view = Mock(return_value={'status': 'queued', 'actions': ['start']})
            store.action = Mock(side_effect=[TaskError('工作区暂时占用', 409, retryable=True), {}])
            store._save = Mock()
            with patch('auto_moniter.recovery_wait.time.time', return_value=100) as clock:
                recovery_wait.schedule(run, 'startup', 'start')
                self.assertFalse(store._recover_lifecycle({'id': 'task'}, run))
                clock.return_value = 115
                self.assertTrue(store._recover_lifecycle({'id': 'task'}, run))
                self.assertEqual(store.action.call_count, 1)
                self.assertFalse(store._recover_lifecycle({'id': 'task'}, run))
                clock.return_value = 145
                self.assertTrue(store._recover_lifecycle({'id': 'task'}, run))
            self.assertEqual(store.action.call_count, 2)

    def test_auto_start_intent_survives_console_exit_before_spawning(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / 'console', root)
            task = store.create({'title': 'Queued App', 'cwd': str(root), 'requirement': 'Validate the app'})
            meta = store._get(task['id'])
            meta['auto_start_pending'] = True
            store._save(meta)
            restarted = TaskStore(root / 'console', root)
            with patch.object(restarted, 'action') as launch:
                restarted.recover_field_handoffs()
                launch.assert_called_once_with(task['id'], 'start')

    def test_temporary_spawn_failure_keeps_start_intent_and_queued_status(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / 'console', root)
            with patch.object(store, '_check_start'), patch('console.tasks.subprocess.Popen',
                                                            side_effect=OSError(errno.EAGAIN, 'temporarily busy')):
                task = store.create({'title': 'Queued App', 'cwd': str(root), 'requirement': 'Validate the app',
                                     'auto_start': True})
            self.assertEqual(task['status'], 'queued')
            self.assertTrue(store._get(task['id'])['auto_start_pending'])
            wait = json.loads((Path(task['run_dir']) / recovery_wait.FILE).read_text())
            self.assertEqual(wait['condition'], 'startup')

    def test_new_checkpoint_invalidates_old_model_recovery_even_with_same_summary(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            state = {'status': 'action_required', 'summary': 'unavailable', 'next_round': 3}
            write_json(run / 'state.json', state)
            with patch('auto_moniter.recovery_wait.time.time', side_effect=[100, 200]):
                recovery_wait.schedule(run, 'model_transport', 'resume', detail=state['summary'])
                self.assertEqual(lifecycle.presentation(run, 'action_required', '')['retry_at'], 115)
                self.assertEqual(lifecycle.presentation(run, 'action_required', 'pause'), {})
                write_json(run / 'state.json', {**state, 'next_round': 4})
                self.assertEqual(lifecycle.presentation(run, 'action_required', ''), {})
                store = object.__new__(TaskStore)
                store.action = Mock()
                self.assertFalse(store._recover_lifecycle({'id': 'task'}, run))
                store.action.assert_not_called()

    def test_queued_requirement_changes_invalidate_pending_recovery(self):
        from auto_moniter.requirement_queue import RequirementQueue

        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            write_json(run / 'state.json', {'status': 'action_required', 'summary': 'unavailable'})
            recovery_wait.schedule(run, 'model_transport', 'resume', detail='unavailable')
            self.assertTrue(lifecycle.presentation(run, 'action_required', ''))
            RequirementQueue(run).append('保留当前任务并追加导出验证。')
            self.assertFalse(lifecycle.presentation(run, 'action_required', ''))

    def test_referee_preserves_duplicate_action_check_when_candidate_is_replaced(self):
        with TemporaryDirectory() as tmp:
            journal = referee.Journal(tmp)
            context = {'kind': 'reviewer_failure', 'tree': 'same', 'candidate': {'round': 1, 'checks': 'old'}}
            old = journal.begin(context, 'interrupted')
            captured = json.loads(decision('retry_reviewer')['text'])
            journal.update(old['id'], 'captured', decision=captured)
            latest = journal.begin({**context, 'candidate': {'round': 2, 'checks': 'new'}}, 'interrupted', resume=True)
            self.assertNotEqual(latest['id'], old['id'])
            self.assertEqual(journal.read()['decisions'][0]['status'], 'superseded')
            journal.update(latest['id'], 'applied', decision=captured)
            again = journal.begin(context, 'interrupted again')
            self.assertTrue(journal.already_applied(again, 'retry_reviewer'))

    def test_unrelated_checkout_does_not_block_creating_a_local_file(self):
        from types import SimpleNamespace

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local, other = root / 'client', root / 'server'
            local.mkdir()
            (other / '.git').mkdir(parents=True)
            (other / 'app.py').write_text('def parse_input(): return 0\n')
            dispatch = FunctionDispatch()
            dispatch.config = SimpleNamespace(cwd=local, assignment_workspaces=[])
            self.assertEqual(dispatch._assignment_repository_conflict(final_task()), '')
            dispatch.config.assignment_workspaces = [f'app.py={other}']
            self.assertIn(str(other), dispatch._assignment_repository_conflict(final_task()))

    def test_referee_uses_authorized_backup_read_only_after_transport_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            (root / 'app.py').write_text('def parse_input(): return 0\n')
            worker = FunctionWorker('worker', [
                {**result('F1', 'done', status='done'), 'touch': 'app.py'}, decision('retry_reviewer')])
            reviewer = ScriptedProvider('reviewer', [
                planning(final_task()), review_failure(), review_failure(),
                {**decision('retry_reviewer'), 'exit_code': 1, 'stderr': '502 Bad Gateway'},
                result('F1', 'accepted', status='done'),
            ])
            config = RunConfig('Complete the original requirement', root, worker, reviewer=reviewer, referee=True,
                               workflow='review-first', function_dispatch=True, full_function_plan=True,
                               git_history='off', run_dir=run, checks=[CHECK], retry_delay_s=0,
                               quality_policy={'referee_transport_fallback': True})
            report = Supervisor(config, log=lambda _: None).run()
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual([call['read_only'] for call in worker.calls], [False, True])
            self.assertIn('referee_provider_fallback', (run / 'events.jsonl').read_text())
