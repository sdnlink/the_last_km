"""Exercise damaged budget evidence and control changes during automatic recovery."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_moniter.evolution import git
from auto_moniter.storage import write_json
from auto_moniter.workspace import file_lock
from tests import test_evolution_phase_execution as phase_fixtures


class EvolutionContinuationBarrierTests(unittest.TestCase):
    def setUp(self):
        self.phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(self.phases.doCleanups)

    def test_invalid_budget_timestamps_block_read_only_plan_and_execution(self):
        fixture = self.phases.fixture()
        monitor, state, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        engine, _ = self.phases.resumer(fixture)
        state_path = monitor / 'state.json'
        heartbeat = monitor / 'heartbeat'
        original_state, original_heartbeat = state_path.read_bytes(), heartbeat.read_bytes()

        for field in ('heartbeat.at', 'usage.recorded_at'):
            with self.subTest(field=field):
                if field == 'heartbeat.at':
                    write_json(heartbeat, {'at': 'invalid'})
                else:
                    write_json(state_path, {**state, 'usage': {**state['usage'], 'recorded_at': 'invalid'}})
                before = {p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                          for p in fixture.root.rglob('*')}
                records = list(engine.directory.glob('*/evolution.json'))
                with patch.object(engine, '_recovery_quiesce', side_effect=AssertionError('cleanup started')), \
                     patch.object(engine, '_run', side_effect=AssertionError('evolution dispatched')), \
                     patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('Supervisor dispatched')):
                    plan = engine.run(plan_only=True)
                    self.assertEqual(before, {
                        p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                        for p in fixture.root.rglob('*')})
                    result = engine.run()
                self.assertEqual(plan['phase'], 'deferred', plan)
                self.assertEqual(result['phase'], 'deferred', result)
                self.assertIn('timestamp', result['summary'].lower())
                self.assertEqual(list(engine.directory.glob('*/evolution.json')), records)
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
                for path in (*records, state_path, heartbeat):
                    self.assertEqual(path.read_bytes(), before[path][1])
            state_path.write_bytes(original_state)
            heartbeat.write_bytes(original_heartbeat)

    def test_missing_heartbeat_allows_read_only_recovery_plan(self):
        fixture = self.phases.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        (monitor / 'heartbeat').unlink()
        engine, _ = self.phases.resumer(fixture)
        self.assertEqual(engine.run(plan_only=True)['action'], 'recover')

    def test_cycle_control_arriving_during_cleanup_blocks_implementation_and_publication(self):
        for stage in ('implementing', 'ready'):
            for action, phase in (('stop', 'stopped'), ('pause', 'deferred')):
                with self.subTest(stage=stage, action=action):
                    fixture = self.phases.fixture()
                    if stage == 'implementing':
                        self.phases.running_child_with_heartbeat(fixture, 0)
                    else:
                        self.phases.interrupt(fixture, 'ready')
                    engine, context = self.phases.resumer(fixture)
                    record = context['path']
                    before = record.read_bytes()
                    control = context['directory'] / 'control.json'
                    original_quiesce = engine._recovery_quiesce
                    written_control = []

                    def request_during_cleanup(*args):
                        original_quiesce(*args)
                        write_json(control, {'action': action})
                        written_control.append(control.read_bytes())

                    with patch.object(engine, '_recovery_quiesce', side_effect=request_during_cleanup) as cleanup, \
                         patch.object(engine, '_run', side_effect=AssertionError('evolution dispatched')), \
                         patch.object(engine, '_promote', side_effect=AssertionError('publication started')), \
                         patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('Supervisor dispatched')):
                        result = engine.run()
                    cleanup.assert_called_once()
                    self.assertEqual(result['phase'], phase, result)
                    self.assertIn(action, result['summary'].lower())
                    self.assertEqual([control.read_bytes()], written_control)
                    self.assertEqual(record.read_bytes(), before)
                    self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
                    self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_cycle_control_arriving_at_implementation_lock_prevents_dispatch(self):
        for action, phase in (('stop', 'stopped'), ('pause', 'deferred')):
            with self.subTest(action=action):
                fixture = self.phases.fixture()
                monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
                engine, context = self.phases.resumer(fixture)
                record = context['path']
                before = record.read_bytes()
                control = monitor.parent / 'control.json'
                source_lock = fixture.source / '.auto-moniter/workspace.lock'
                acquisitions = 0
                written_control = []

                @contextmanager
                def request_at_handoff(path):
                    nonlocal acquisitions
                    with file_lock(path):
                        if path == source_lock:
                            acquisitions += 1
                            if acquisitions == 2:
                                write_json(control, {'action': action})
                                written_control.append(control.read_bytes())
                        yield

                with patch('auto_moniter.evolution.file_lock', side_effect=request_at_handoff), \
                     patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('Supervisor dispatched')) as supervisor:
                    result = engine.run()
                supervisor.assert_not_called()
                self.assertEqual(acquisitions, 2)
                self.assertEqual(result['phase'], phase, result)
                self.assertEqual([control.read_bytes()], written_control)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_child_budget_exhausting_at_publication_locks_prevents_fast_forward(self):
        for handoff in (2, 3):
            with self.subTest(handoff=handoff):
                fixture = self.phases.fixture()
                record = self.phases.interrupt(fixture, 'ready')
                engine, _ = self.phases.resumer(fixture)
                before = record.read_bytes()
                monitor = record.parent / 'monitor'
                state_path, report_path = monitor / 'state.json', monitor / 'report.json'
                review_evidence = state_path.read_bytes(), report_path.read_bytes()
                usage = json.loads(review_evidence[0])['usage']
                exhausted = datetime.fromisoformat(usage['recorded_at']) + timedelta(
                    seconds=usage['max_runtime_s'] - usage['elapsed_s'] + .000001)
                heartbeat = monitor / 'heartbeat'
                source_lock = fixture.source / '.auto-moniter/workspace.lock'
                acquisitions = 0
                written_heartbeat = []

                @contextmanager
                def exhaust_at_handoff(path):
                    nonlocal acquisitions
                    with file_lock(path):
                        if path == source_lock:
                            acquisitions += 1
                            if acquisitions == handoff:
                                write_json(heartbeat, {'at': exhausted.isoformat()})
                                written_heartbeat.append(heartbeat.read_bytes())
                        yield

                with patch('auto_moniter.evolution.file_lock', side_effect=exhaust_at_handoff), \
                     patch.object(engine, '_activate', return_value=True) as activate:
                    result = engine.run()
                self.assertEqual(result['phase'], 'stopped', result)
                activate.assert_not_called()
                self.assertEqual(acquisitions, handoff)
                self.assertEqual([heartbeat.read_bytes()], written_heartbeat)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual((state_path.read_bytes(), report_path.read_bytes()), review_evidence)
                self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_reused_candidate_accounts_late_child_usage_against_outer_budget(self):
        for child_elapsed, spent, expected_phase in ((89, 101, 'stopped'), (87, 99, 'promoted'),
                                                      (20, 42, 'promoted')):
            with self.subTest(child_elapsed=child_elapsed):
                fixture = self.phases.fixture()
                fixture.config = replace(fixture.config, max_runtime_s=100)
                fixture.engine.config = fixture.config
                record = self.phases.interrupt(fixture, 'candidate',
                                               lambda fields: fields.get('candidate_checks_complete') is True)
                saved = json.loads(record.read_bytes())
                saved['elapsed_s'] = saved['usage']['elapsed_s'] = 40
                write_json(record, saved)
                monitor = record.parent / 'monitor'
                state_path = monitor / 'state.json'
                state = json.loads(state_path.read_bytes())
                usage = state['usage']
                recorded = datetime.fromisoformat(usage['recorded_at'])
                usage.update(max_runtime_s=90, elapsed_s=20)
                write_json(state_path, state)
                heartbeat = monitor / 'heartbeat'
                write_json(heartbeat, {'at': recorded.isoformat()})
                engine, context = self.phases.resumer(fixture)
                source_lock = fixture.source / '.auto-moniter/workspace.lock'
                now = 1000
                acquisitions = 0

                @contextmanager
                def update_at_publication(path):
                    nonlocal acquisitions, now
                    with file_lock(path):
                        if path == source_lock:
                            acquisitions += 1
                            if acquisitions == 2:
                                now += 1
                            if acquisitions == 3:
                                now += 1
                                write_json(heartbeat, {'at': (recorded + timedelta(seconds=child_elapsed - 20)).isoformat()})
                        yield

                with patch('auto_moniter.evolution.time', SimpleNamespace(monotonic=lambda: now)), \
                     patch('auto_moniter.evolution.file_lock', side_effect=update_at_publication), \
                     patch.object(engine, '_activate', return_value=True) as activate, \
                     patch.object(engine, '_validate', side_effect=AssertionError('checks replayed')), \
                     patch('auto_moniter.evolution.Supervisor') as supervisor:
                    result = engine.run()
                supervisor.assert_not_called()
                self.assertEqual(result['phase'], expected_phase, result.get('summary'))
                self.assertEqual(result['elapsed_s'], spent)
                self.assertEqual(result['agent_calls'], usage['agent_calls'])
                head = context['record']['candidate'] if expected_phase == 'promoted' else fixture.base
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), head)
                self.assertEqual(activate.call_count, int(expected_phase == 'promoted'))
                self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])

    def test_removed_review_evidence_at_final_publication_lock_defers_fast_forward(self):
        fixture = self.phases.fixture()
        record = self.phases.interrupt(fixture, 'ready')
        engine, _ = self.phases.resumer(fixture)
        before = record.read_bytes()
        monitor = record.parent / 'monitor'
        archived = record.parent / 'monitor-retained'
        names = ['state.json', 'report.json']
        review = [(monitor / name).read_bytes() for name in names]
        source_lock = fixture.source / '.auto-moniter/workspace.lock'
        acquisitions = 0

        @contextmanager
        def remove_evidence_at_publication(path):
            nonlocal acquisitions
            with file_lock(path):
                if path == source_lock:
                    acquisitions += 1
                    if acquisitions == 3:
                        monitor.rename(archived)
                yield

        with patch('auto_moniter.evolution.file_lock', side_effect=remove_evidence_at_publication), \
             patch.object(engine, '_activate', return_value=True) as activate:
            result = engine.run()
        self.assertEqual(result['phase'], 'deferred', result.get('summary'))
        activate.assert_not_called()
        self.assertEqual(acquisitions, 3)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual([(archived / name).read_bytes() for name in names], review)
        self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_removed_supervisor_at_implementation_lock_defers_dispatch(self):
        fixture = self.phases.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        engine, context = self.phases.resumer(fixture)
        record = context['path']
        before = record.read_bytes()
        state = (monitor / 'state.json').read_bytes()
        archived = monitor.with_name('monitor-retained')
        source_lock = fixture.source / '.auto-moniter/workspace.lock'
        acquisitions = 0

        @contextmanager
        def remove_monitor_at_handoff(path):
            nonlocal acquisitions
            with file_lock(path):
                if path == source_lock:
                    acquisitions += 1
                    if acquisitions == 2:
                        monitor.rename(archived)
                yield

        with patch('auto_moniter.evolution.file_lock', side_effect=remove_monitor_at_handoff), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('Supervisor dispatched')) as supervisor:
            result = engine.run()
        supervisor.assert_not_called()
        self.assertEqual(result['phase'], 'deferred', result.get('summary'))
        self.assertEqual(acquisitions, 2)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual((archived / 'state.json').read_bytes(), state)
        self.assertFalse(monitor.exists())
        self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_selected_cycle_can_start_its_first_supervisor(self):
        fixture = self.phases.fixture()
        record = self.phases.interrupt(fixture, 'selected')
        engine, _ = self.phases.resumer(fixture)
        monitor = record.parent / 'monitor'
        self.assertFalse(monitor.exists())

        with patch('auto_moniter.evolution.Supervisor', wraps=phase_fixtures.Supervisor) as supervisor:
            result = engine.run()

        supervisor.assert_called_once()
        config = supervisor.call_args.args[0]
        self.assertFalse(config.resume)
        self.assertEqual(config.run_dir, monitor)
        self.phases.assert_promoted_once(fixture, engine, result, record)

    def test_removed_supervisor_at_early_recovery_locks_defers_dispatch(self):
        for lock_kind, target_acquisition in (('cycle', 2), ('source', 1)):
            with self.subTest(lock=lock_kind):
                fixture = self.phases.fixture()
                monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
                engine, context = self.phases.resumer(fixture)
                record = context['path']
                before = record.read_bytes()
                state = (monitor / 'state.json').read_bytes()
                archived = monitor.with_name('monitor-retained')
                target_lock = (engine.directory / 'evolution.lock' if lock_kind == 'cycle'
                               else fixture.source / '.auto-moniter/workspace.lock')
                acquisitions = 0

                @contextmanager
                def remove_monitor_at_handoff(path):
                    nonlocal acquisitions
                    with file_lock(path):
                        if path == target_lock:
                            acquisitions += 1
                            if acquisitions == target_acquisition:
                                monitor.rename(archived)
                        yield

                with patch('auto_moniter.evolution.file_lock', side_effect=remove_monitor_at_handoff), \
                     patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('Supervisor dispatched')) as supervisor:
                    result = engine.run()
                supervisor.assert_not_called()
                self.assertEqual(result['phase'], 'deferred', result.get('summary'))
                self.assertIn('previously observed', result['summary'].lower())
                self.assertEqual(acquisitions, target_acquisition)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual((archived / 'state.json').read_bytes(), state)
                self.assertFalse(monitor.exists())
                self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_pending_transaction_at_final_publication_lock_defers_fast_forward(self):
        for transaction in ('publication.json', 'work-checkpoint.json'):
            with self.subTest(transaction=transaction):
                fixture = self.phases.fixture()
                record = self.phases.interrupt(fixture, 'ready')
                engine, _ = self.phases.resumer(fixture)
                before = record.read_bytes()
                marker = record.parent / 'monitor' / transaction
                contents = b'{"fixture": "pending transaction"}\n'
                source_lock = fixture.source / '.auto-moniter/workspace.lock'
                acquisitions = 0

                @contextmanager
                def leave_transaction_at_handoff(path):
                    nonlocal acquisitions
                    with file_lock(path):
                        if path == source_lock:
                            acquisitions += 1
                            if acquisitions == 3:
                                marker.write_bytes(contents)
                        yield

                with patch('auto_moniter.evolution.file_lock', side_effect=leave_transaction_at_handoff), \
                     patch.object(engine, '_activate', return_value=True) as activate:
                    result = engine.run()
                self.assertEqual(result['phase'], 'deferred', result.get('summary'))
                activate.assert_not_called()
                self.assertEqual(acquisitions, 3)
                self.assertEqual(marker.read_bytes(), contents)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual(list(engine.directory.glob('*/evolution.json')), [record])
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
