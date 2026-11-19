"""Recovery preflights preserve evidence and keep publication free of model calls."""
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from auto_moniter import evolution
from auto_moniter.evolution import Evolution, git
from auto_moniter.processes import process_identity, recover_process
from auto_moniter.storage import write_json
from auto_moniter.supervisor import Supervisor
from auto_moniter.workspace import LockError, file_lock
from tests import test_evolution_phase_execution as phase_fixtures


class EvolutionRecoveryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(self.phases.doCleanups)

    def fixture(self):
        return self.phases.fixture()

    def recovery(self, fixture):
        return Evolution(fixture.config, evidence_roots=[fixture.evidence],
                         directory=fixture.engine.directory, log=lambda _: None)

    def ready(self, fixture):
        self.phases.interrupt(fixture, 'ready')
        return fixture.engine.path.parent

    def test_implementation_requires_explicit_opt_in_and_resumes_original_supervisor(self):
        fixture = self.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        engine = self.recovery(fixture)
        original = fixture.engine.path.read_bytes()
        checkpoint = (monitor / 'state.json').read_bytes()
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('default dispatched a model')):
            with self.assertRaisesRegex(ValueError, 'implementation recovery is disabled'):
                engine.recover(monitor.parent)
        self.assertEqual(fixture.engine.path.read_bytes(), original)
        self.assertEqual((monitor / 'state.json').read_bytes(), checkpoint)
        self.assertEqual(engine.record, {})
        configurations = []

        def controller(config, **kwargs):
            configurations.append(config)
            return Supervisor(config, **kwargs)

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller):
            result = engine.recover(monitor.parent, allow_implementation=True)
        self.assertEqual(result['phase'], 'promoted', result.get('summary'))
        self.assertEqual(len(configurations), 1)
        self.assertTrue(configurations[0].resume)
        self.assertEqual(configurations[0].run_dir, monitor)
        self.assertEqual(engine.path, fixture.engine.path)
        self.assertEqual(result['workspace'], str(monitor.parent / 'workspace'))
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))
        accepted = json.loads((monitor / 'state.json').read_text())['function_tasks']['accepted']
        self.assertEqual([item['task']['id'] for item in accepted], ['F1'])

    def test_selected_baseline_and_validating_route_to_original_cycle(self):
        for phase in ('selected', 'baseline', 'validating'):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                path = self.phases.interrupt(fixture, phase)
                engine = self.recovery(fixture)
                result = engine.recover(path.parent, allow_implementation=True)
                self.phases.assert_promoted_once(fixture, engine, result, path)

    def test_default_publication_and_completed_fast_forward_are_idempotent_without_models(self):
        fixture = self.fixture()
        directory = self.ready(fixture)
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('publication dispatched a model')):
            result = engine.recover(directory)
            self.assertEqual(result['phase'], 'promoted', result.get('summary'))
            head = git(fixture.source, 'rev-parse', 'HEAD')
            original = engine.path.read_bytes()
            with patch.object(engine, '_activate', side_effect=AssertionError('completed activation replayed')):
                again = engine.recover(directory)
        self.assertEqual(again['phase'], 'promoted')
        self.assertEqual(engine.path.read_bytes(), original)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))

    def test_fast_forward_completed_before_interruption_is_not_committed_again(self):
        fixture = self.fixture()
        original_git = evolution.git

        def interrupt(cwd, *args):
            result = original_git(cwd, *args)
            if cwd == fixture.source and args[:1] == ('merge',):
                raise OSError('interrupted after fast forward')
            return result

        with patch('auto_moniter.evolution.git', side_effect=interrupt):
            stopped = fixture.engine.run()
        self.assertEqual(stopped['phase'], 'recovery_required')
        engine = self.recovery(fixture)
        head = git(fixture.source, 'rev-parse', 'HEAD')
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('publication dispatched a model')):
            result = engine.recover(fixture.engine.path.parent)
        self.assertEqual(result['phase'], 'promoted', result.get('summary'))
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '2')

    def test_interrupted_rollback_reuses_saved_commit_before_and_after_its_merge(self):
        for after_merge in (False, True):
            with self.subTest(after_merge=after_merge):
                fixture = self.fixture()
                directory = self.ready(fixture)
                engine = self.recovery(fixture)
                original_git = evolution.git
                merges = []

                def interrupt(cwd, *args):
                    is_merge = cwd == fixture.source and args[:1] == ('merge',)
                    if is_merge:
                        merges.append(args)
                        if len(merges) == 2 and not after_merge:
                            raise OSError('interrupted before rollback merge')
                    value = original_git(cwd, *args)
                    if is_merge and len(merges) == 2 and after_merge:
                        raise OSError('interrupted after rollback merge')
                    return value

                with patch.object(engine, '_activate', return_value=False), \
                     patch('auto_moniter.evolution.git', side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, 'rollback merge'):
                        engine.recover(directory)
                saved = json.loads(engine.path.read_text())
                rollback = saved['rollback_commit']
                with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('rollback dispatched a model')):
                    result = engine.recover(directory)
                    self.assertEqual(result['phase'], 'rolled_back')
                    original = engine.path.read_bytes()
                    with patch.object(engine, '_activate', side_effect=AssertionError('completed rollback replayed')):
                        again = engine.recover(directory)
                self.assertEqual(again['phase'], 'rolled_back')
                self.assertEqual(engine.path.read_bytes(), original)
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), rollback)
                self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '3')
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD^{tree}'), git(fixture.source, 'rev-parse', fixture.base + '^{tree}'))

    def test_tampered_publication_evidence_is_rejected_without_changing_checkpoints(self):
        for kind in ('review', 'log', 'candidate', 'configuration', 'supervisor_config', 'process', 'rollback'):
            with self.subTest(kind=kind):
                fixture = self.fixture()
                directory = self.ready(fixture)
                record = json.loads(fixture.engine.path.read_text())
                monitor = directory / 'monitor'
                if kind == 'review':
                    report = json.loads((monitor / 'report.json').read_text())
                    del report['summary']
                    write_json(monitor / 'report.json', report)
                elif kind == 'log':
                    (directory / 'candidate-01.log').write_text('substituted evidence')
                elif kind == 'candidate':
                    record['candidate'] = 'f' * 40
                elif kind == 'configuration':
                    record['config_identity']['model'] = 'different model'
                elif kind == 'supervisor_config':
                    state = json.loads((monitor / 'state.json').read_text())
                    state['provider_config']['worker']['binary'] = 'different executable'
                    write_json(monitor / 'state.json', state)
                elif kind == 'process':
                    write_json(monitor / 'process.json', {'pid': True, 'started': 0})
                elif kind == 'rollback':
                    record.update(phase='rolling_back', resume_phase='rolling_back', rollback_commit=record['candidate'])
                write_json(fixture.engine.path, record)
                original = fixture.engine.path.read_bytes()
                state_before = (monitor / 'state.json').read_bytes()
                engine = self.recovery(fixture)
                with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('tampering dispatched a model')):
                    with self.assertRaises(ValueError):
                        engine.recover(directory)
                self.assertEqual(fixture.engine.path.read_bytes(), original)
                self.assertEqual((monitor / 'state.json').read_bytes(), state_before)
                self.assertEqual(engine.record, {})
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_active_supervisor_lock_refuses_implementation_without_touching_checkpoint(self):
        fixture = self.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        original = fixture.engine.path.read_bytes()
        state_before = (monitor / 'state.json').read_bytes()
        engine = self.recovery(fixture)
        with file_lock(monitor / 'supervisor.lock'), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('active monitor replayed')):
            with self.assertRaises(LockError):
                engine.recover(monitor.parent, allow_implementation=True)
        self.assertEqual(fixture.engine.path.read_bytes(), original)
        self.assertEqual((monitor / 'state.json').read_bytes(), state_before)
        self.assertEqual(engine.record, {})

    def live_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        self.addCleanup(cleanup)
        return process

    def test_owned_orphan_is_quiesced_before_resuming_and_heartbeat_is_preserved(self):
        fixture = self.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        process = self.live_process()
        write_json(monitor / 'process.json', process_identity(process.pid))
        heartbeat = (monitor / 'heartbeat').read_bytes()
        engine = self.recovery(fixture)

        def controller(config, **kwargs):
            self.assertIsNotNone(process.poll())
            self.assertEqual((monitor / 'heartbeat').read_bytes(), heartbeat)
            self.assertTrue(config.resume)
            return Supervisor(config, **kwargs)

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller):
            result = engine.recover(monitor.parent, allow_implementation=True)
        self.assertEqual(result['phase'], 'promoted', result.get('summary'))
        self.assertFalse((monitor / 'process.json').exists())
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))

    def test_wrong_orphan_identity_is_not_signalled_and_preserves_evidence(self):
        fixture = self.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        process = self.live_process()
        identity = process_identity(process.pid)
        identity['started'] += 1
        write_json(monitor / 'process.json', identity)
        original = fixture.engine.path.read_bytes()
        process_before = (monitor / 'process.json').read_bytes()
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('unverified process replayed')):
            with self.assertRaisesRegex(ValueError, 'process ownership'):
                engine.recover(monitor.parent, allow_implementation=True)
        self.assertIsNone(process.poll())
        self.assertEqual((monitor / 'process.json').read_bytes(), process_before)
        self.assertEqual(fixture.engine.path.read_bytes(), original)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_completed_legacy_publication_is_read_without_fabricating_identity(self):
        fixture = self.fixture()
        promoted = fixture.engine.run()
        self.assertEqual(promoted['phase'], 'promoted')
        legacy = copy.deepcopy(promoted)
        legacy['version'] = 1
        for key in ('config_identity', 'usage', 'resume_phase', 'stop_reason'):
            legacy.pop(key)
        write_json(fixture.engine.path, legacy)
        original = fixture.engine.path.read_bytes()
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('legacy publication dispatched a model')):
            result = engine.recover(fixture.engine.path.parent)
        self.assertEqual(result, legacy)
        self.assertEqual(fixture.engine.path.read_bytes(), original)
        self.assertNotIn('config_identity', result)
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '2')

    def test_bad_completed_review_is_rejected_before_orphan_cleanup(self):
        fixture = self.fixture()
        monitor = self.phases.finished_child(fixture)
        report = json.loads((monitor / 'report.json').read_text())
        del report['summary']
        write_json(monitor / 'report.json', report)
        process = self.live_process()
        write_json(monitor / 'process.json', process_identity(process.pid))
        original = fixture.engine.path.read_bytes()
        process_before = (monitor / 'process.json').read_bytes()
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('bad review dispatched a model')):
            with self.assertRaisesRegex(ValueError, 'review'):
                engine.recover(monitor.parent, allow_implementation=True)
        self.assertIsNone(process.poll())
        self.assertEqual((monitor / 'process.json').read_bytes(), process_before)
        self.assertEqual(fixture.engine.path.read_bytes(), original)

    def test_bad_baseline_log_is_rejected_before_creating_a_monitor(self):
        fixture = self.fixture()
        path = self.phases.interrupt(fixture, 'implementing')
        (path.parent / 'baseline-01.log').write_text('changed after baseline passed')
        original = path.read_bytes()
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('bad evidence dispatched a model')):
            with self.assertRaisesRegex(ValueError, 'evidence'):
                engine.recover(path.parent, allow_implementation=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((path.parent / 'monitor').exists())
        self.assertFalse(fixture.worker.calls or fixture.reviewer.calls)

    def test_previous_boot_pid_reuse_reaches_kernel_without_signalling_new_process(self):
        fixture = self.fixture()
        monitor, _, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        process = self.live_process()
        saved = process_identity(process.pid)
        saved.update(boot='00000000-0000-0000-0000-000000000000', started=saved['started'] + 1)
        write_json(monitor / 'process.json', saved)
        results = []
        original_killpg = os.killpg

        def kernel(identity):
            result = recover_process(identity)
            results.append(result)
            return result

        def signal_group(group, signal):
            self.assertNotEqual(group, process.pid, 'previous-boot PID must never be signalled')
            return original_killpg(group, signal)

        engine = self.recovery(fixture)
        configurations = []

        def controller(config, **kwargs):
            configurations.append(config)
            self.assertIsNone(process.poll())
            return Supervisor(config, **kwargs)

        with patch('auto_moniter.supervisor.recover_process', side_effect=kernel), \
             patch('auto_moniter.processes.os.killpg', side_effect=signal_group), \
             patch('auto_moniter.evolution.Supervisor', side_effect=controller):
            result = engine.recover(monitor.parent, allow_implementation=True)
        self.assertEqual(results, ['previous_boot'])
        self.assertIsNone(process.poll())
        self.assertEqual(result['phase'], 'promoted', result.get('summary'))
        self.assertEqual(len(configurations), 1)
        self.assertTrue(configurations[0].resume)
        self.assertEqual(configurations[0].run_dir, monitor)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))

    def test_all_invalid_execution_settings_are_rejected_before_cleanup_or_dispatch(self):
        fixture = self.fixture()
        monitor, state, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        process = self.live_process()
        write_json(monitor / 'process.json', process_identity(process.pid))
        cycle_before = fixture.engine.path.read_bytes()
        process_before = (monitor / 'process.json').read_bytes()
        cases = (('idle_timeout_s', -1), ('turn_timeout_s', True), ('retry_delay_s', '5'),
                 ('check_timeout_s', 0), ('max_output_bytes', 1.5),
                 ('idle_timeout_s', float('inf')), ('unknown_setting', 1))
        for key, value in cases:
            with self.subTest(key=key, value=value):
                damaged = copy.deepcopy(state)
                damaged['execution_settings'][key] = value
                write_json(monitor / 'state.json', damaged)
                state_before = (monitor / 'state.json').read_bytes()
                engine = self.recovery(fixture)
                with patch.object(evolution._RecoverySupervisor, '_recover_interrupted_run',
                                  side_effect=AssertionError('invalid configuration reached cleanup')) as cleanup, \
                     patch('auto_moniter.evolution.Supervisor',
                           side_effect=AssertionError('invalid configuration constructed Supervisor')) as actor:
                    with self.assertRaisesRegex(ValueError, 'execution settings'):
                        engine.recover(monitor.parent, allow_implementation=True)
                    cleanup.assert_not_called()
                    actor.assert_not_called()
                self.assertEqual(fixture.engine.path.read_bytes(), cycle_before)
                self.assertEqual((monitor / 'state.json').read_bytes(), state_before)
                self.assertEqual((monitor / 'process.json').read_bytes(), process_before)
                self.assertIsNone(process.poll())
                self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def completed_no_change(self, fixture):
        monitor = self.phases.finished_child(fixture)
        work = monitor.parent / 'workspace'
        # Model the independently reviewed unchanged result with a full, real
        # Supervisor checkpoint, pinned to the unchanged worktree's exact head.
        git(work, 'reset', '--hard', fixture.base)
        for name in ('state.json', 'report.json'):
            evidence = json.loads((monitor / name).read_text())
            for key in ('business_head', 'last_business_commit'):
                evidence['git_history'][key]['commit'] = fixture.base
            evidence['summary'] = 'Independent review confirmed no runtime change'
            write_json(monitor / name, evidence)
        return monitor

    def test_completed_no_change_review_resumes_without_models_validation_or_publication(self):
        fixture = self.fixture()
        monitor = self.completed_no_change(fixture)
        source_head = git(fixture.source, 'rev-parse', 'HEAD')
        commits = git(fixture.source, 'rev-list', '--count', 'HEAD')
        checkpoint = (monitor / 'state.json').read_bytes()
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('completed no-change model replayed')), \
             patch.object(engine, '_validate', side_effect=AssertionError('no-change candidate validated')), \
             patch.object(engine, '_promote', side_effect=AssertionError('no-change candidate published')):
            result = engine.recover(monitor.parent, allow_implementation=True)
        self.assertEqual(result['phase'], 'no_change', result.get('summary'))
        self.assertEqual(engine.path, fixture.engine.path)
        self.assertEqual(result['workspace'], str(monitor.parent / 'workspace'))
        self.assertEqual(result['monitor_run'], str(monitor))
        self.assertEqual((monitor / 'state.json').read_bytes(), checkpoint)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), source_head)
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), commits)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))

    def test_no_change_with_damaged_review_preserves_checkpoint_and_refuses_recovery(self):
        fixture = self.fixture()
        monitor = self.completed_no_change(fixture)
        report = json.loads((monitor / 'report.json').read_text())
        del report['summary']
        write_json(monitor / 'report.json', report)
        original = fixture.engine.path.read_bytes()
        report_before = (monitor / 'report.json').read_bytes()
        engine = self.recovery(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('bad no-change review replayed')), \
             patch.object(engine, '_recovery_quiesce', side_effect=AssertionError('bad review reached cleanup')):
            with self.assertRaisesRegex(ValueError, 'review'):
                engine.recover(monitor.parent, allow_implementation=True)
        self.assertEqual(fixture.engine.path.read_bytes(), original)
        self.assertEqual((monitor / 'report.json').read_bytes(), report_before)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)


if __name__ == '__main__':
    unittest.main()
