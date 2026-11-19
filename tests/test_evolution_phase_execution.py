"""Continue real isolated evolution cycles without replaying accepted functions."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution, git
from auto_moniter.evolution_checkpoint import load_checkpoint
from auto_moniter.storage import write_json
from auto_moniter.supervisor import Supervisor
from tests import test_evolution as evolution_fixtures
from tests.test_function_dispatch import result as task_result
from tests.test_supervisor import ScriptedProvider


class PowerLoss(BaseException):
    pass


class EvolutionPhaseExecutionTests(unittest.TestCase):
    def fixture(self):
        fixture = evolution_fixtures.EvolutionTests('test_plan_only_reads_faults_without_writing_or_calling_models')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def interrupt(self, fixture, phase, predicate=lambda fields: True):
        original = fixture.engine._save

        def save(current, **fields):
            original(current, **fields)
            if current == phase and predicate(fields):
                raise PowerLoss(current)

        with patch.object(fixture.engine, '_save', side_effect=save):
            with self.assertRaises(PowerLoss):
                fixture.engine.run()
        return fixture.engine.path

    def resumer(self, fixture):
        context = load_checkpoint(fixture.engine.path, fixture.source)
        engine = Evolution(fixture.config, evidence_roots=[fixture.evidence],
                           directory=fixture.engine.directory, log=lambda _: None)
        return engine, context

    def continue_cycle(self, engine, context):
        return engine._run(context['record']['issue'], context['record']['base'], context=context)

    def finished_child(self, fixture):
        class FinishThenLosePower:
            def __init__(self, config, **kwargs):
                self.controller = Supervisor(config, **kwargs)

            def run(self):
                self.controller.run()
                raise PowerLoss('Supervisor finished before outer accounting')

        with patch('auto_moniter.evolution.Supervisor', FinishThenLosePower):
            with self.assertRaises(PowerLoss):
                fixture.engine.run()
        self.assertEqual(fixture.engine.record['phase'], 'implementing')
        return fixture.engine.path.parent / 'monitor'

    def assert_promoted_once(self, fixture, engine, result, original_path):
        self.assertEqual(result['phase'], 'promoted', result.get('summary'))
        self.assertEqual(engine.path, original_path)
        self.assertEqual(result['workspace'], str(original_path.parent / 'workspace'))
        self.assertEqual(result['monitor_run'], str(original_path.parent / 'monitor'))
        self.assertEqual(list(fixture.engine.directory.glob('*/evolution.json')), [original_path])
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '2')

    def test_selected_baseline_implementing_reviewed_and_validating_continue_same_cycle(self):
        for phase in ('selected', 'baseline', 'implementing', 'reviewed', 'validating'):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                path = self.interrupt(fixture, phase)
                engine, context = self.resumer(fixture)
                if phase in {'reviewed', 'validating'}:
                    with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('completed task replayed')):
                        result = self.continue_cycle(engine, context)
                else:
                    result = self.continue_cycle(engine, context)
                self.assert_promoted_once(fixture, engine, result, path)

    def test_completed_supervisor_is_accounted_without_constructing_another_supervisor(self):
        fixture = self.fixture()
        monitor = self.finished_child(fixture)
        state_before = (monitor / 'state.json').read_bytes()
        accepted = json.loads(state_before)['function_tasks']['accepted']
        self.assertTrue(accepted)
        engine, context = self.resumer(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('completed task replayed')):
            result = self.continue_cycle(engine, context)
        self.assert_promoted_once(fixture, engine, result, fixture.engine.path)
        self.assertEqual((monitor / 'state.json').read_bytes(), state_before)
        self.assertEqual(result['agent_calls'], json.loads(state_before)['usage']['agent_calls'])
        self.assertGreaterEqual(result['elapsed_s'], json.loads(state_before)['usage']['elapsed_s'])

    def test_stopped_supervisor_resumes_original_checkpoint_and_preserves_accepted_tasks(self):
        fixture = self.fixture()
        planning = json.loads(fixture.reviewer.scripts[0]['text'])
        first = copy.deepcopy(planning['next_task'])
        first.update(delivery_scope='function', criterion_ids=['main:1', 'req:1:1'])
        final = copy.deepcopy(planning['next_task'])
        final.update(id='F2', kind='verification', depends_on=['F1'], test_files=[], check_ids=['regression:1'])
        planning.update(next_task=first, function_plan=[first, final])
        fixture.reviewer.scripts = [{'verdict': {}, 'text': json.dumps(planning)},
            task_result('F1', 'accepted', file=first['file']),
            task_result('F2', 'accepted', status='done', file=final['file'])]

        class RepairThenVerify(evolution_fixtures.EvolutionWorker):
            def _command(self, session, prompt, read_only):
                if not self.calls:
                    return super()._command(session, prompt, read_only)
                return ScriptedProvider._command(self, session, prompt, read_only)

        fixture.worker = RepairThenVerify('worker', [task_result('F1', 'done', file=first['file']),
            task_result('F2', 'done', status='done', file=final['file'])])
        fixture.config.provider = fixture.worker
        original_control = Supervisor._process_control

        def stop_after_acceptance(controller, active=False):
            original_control(controller, active)
            if not active and len(controller.function_tasks.accepted) == 1:
                controller.request_stop()

        with patch.object(Supervisor, '_process_control', stop_after_acceptance):
            stopped = fixture.engine.run()
        self.assertEqual(stopped['phase'], 'stopped', stopped.get('summary'))
        monitor = fixture.engine.path.parent / 'monitor'
        state = json.loads((monitor / 'state.json').read_text())
        accepted = copy.deepcopy(state['function_tasks']['accepted'])
        self.assertTrue(accepted)
        self.assertEqual([item['task']['id'] for item in accepted], ['F1'])
        self.assertEqual(len(fixture.worker.calls), 1)
        engine, context = self.resumer(fixture)
        configurations = []

        def controller(config, **kwargs):
            configurations.append(config)
            return Supervisor(config, **kwargs)

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller):
            result = self.continue_cycle(engine, context)
        self.assertEqual(result['phase'], 'promoted', result.get('summary'))
        self.assertEqual(engine.path, fixture.engine.path)
        self.assertEqual(result['workspace'], str(monitor.parent / 'workspace'))
        self.assertEqual(result['monitor_run'], str(monitor))
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (2, 3))
        self.assertIn('task_id=F1', fixture.worker.calls[0]['prompt'])
        self.assertIn('task_id=F2', fixture.worker.calls[1]['prompt'])
        self.assertEqual((fixture.source / 'auto_moniter/supervisor.py').read_text(), evolution_fixtures.FIX)
        self.assertEqual(len(configurations), 1)
        self.assertTrue(configurations[0].resume)
        self.assertEqual(configurations[0].run_dir, monitor)
        self.assertLessEqual(configurations[0].max_runtime_s, state['usage']['max_runtime_s'])
        resumed_accepted = json.loads((monitor / 'state.json').read_text())['function_tasks']['accepted']
        self.assertEqual(resumed_accepted[:len(accepted)], accepted)
        self.assertEqual([item['task']['id'] for item in resumed_accepted], ['F1', 'F2'])

    def test_partial_baseline_checks_are_rerun(self):
        fixture = self.fixture()
        path = self.interrupt(fixture, 'baseline', lambda fields: len(fields.get('baseline_checks', [])) == 1)
        self.assertEqual(len(fixture.engine.record['baseline_checks']), 1)
        engine, context = self.resumer(fixture)
        original = engine._validate
        stages = []

        def validate(cwd, stage):
            stages.append(stage)
            return original(cwd, stage)

        with patch.object(engine, '_validate', side_effect=validate):
            result = self.continue_cycle(engine, context)
        self.assertEqual(stages, ['baseline', 'candidate'])
        self.assert_promoted_once(fixture, engine, result, path)

    def test_complete_baseline_evidence_is_reused_by_identity_and_hash(self):
        fixture = self.fixture()
        path = self.interrupt(fixture, 'implementing')
        baseline = copy.deepcopy(fixture.engine.record['baseline_checks'])
        engine, context = self.resumer(fixture)
        original = engine._validate
        stages = []

        def validate(cwd, stage):
            stages.append(stage)
            return original(cwd, stage)

        with patch.object(engine, '_validate', side_effect=validate):
            result = self.continue_cycle(engine, context)
        self.assertEqual(stages, ['candidate'])
        self.assertEqual(result['baseline_checks'], baseline)
        self.assert_promoted_once(fixture, engine, result, path)

    def test_changed_baseline_evidence_reruns_at_base_without_touching_accepted_work(self):
        fixture = self.fixture()
        monitor = self.finished_child(fixture)
        accepted_before = (monitor / 'state.json').read_bytes()
        candidate = git(fixture.engine.path.parent / 'workspace', 'rev-parse', 'HEAD')
        (fixture.engine.path.parent / 'baseline-01.log').write_text('unverifiable old evidence')
        engine, context = self.resumer(fixture)
        original = engine._validate
        observations = []

        def validate(cwd, stage):
            observations.append((stage, cwd, git(cwd, 'rev-parse', 'HEAD')))
            return original(cwd, stage)

        with patch.object(engine, '_validate', side_effect=validate), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('accepted implementation replayed')):
            result = self.continue_cycle(engine, context)
        self.assertEqual(observations[0], ('baseline', engine.path.parent / 'baseline-verification', fixture.base))
        self.assertEqual(observations[1], ('candidate', engine.path.parent / 'validation', candidate))
        self.assertEqual((monitor / 'state.json').read_bytes(), accepted_before)
        self.assert_promoted_once(fixture, engine, result, fixture.engine.path)

    def test_complete_candidate_checks_are_reused_without_replaying_validation(self):
        fixture = self.fixture()
        path = self.interrupt(fixture, 'candidate', lambda fields: fields.get('candidate_checks_complete') is True)
        engine, context = self.resumer(fixture)
        with patch.object(engine, '_validate', side_effect=AssertionError('verified checks replayed')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('accepted implementation replayed')):
            result = self.continue_cycle(engine, context)
        self.assert_promoted_once(fixture, engine, result, path)

    def test_partial_candidate_validation_reruns_and_a_failing_candidate_cannot_upgrade(self):
        fixture = self.fixture()
        self.interrupt(fixture, 'candidate', lambda fields: len(fields.get('candidate_checks', [])) == 1)
        engine, context = self.resumer(fixture)
        original = engine._validate
        stages = []

        def failing(cwd, stage):
            stages.append(stage)
            if stage == 'candidate':
                (cwd / 'tests' / 'test_resume_failure.py').write_text(
                    'import unittest\nclass Regression(unittest.TestCase):\n def test_failure(self): self.fail("resume gate")\n')
            return original(cwd, stage)

        with patch.object(engine, '_validate', side_effect=failing):
            result = self.continue_cycle(engine, context)
        self.assertEqual(stages, ['candidate'])
        self.assertEqual(result['phase'], 'candidate_failed')
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))

    def test_source_changes_defer_a_resumed_cycle_without_upgrading_or_replaying_work(self):
        fixture = self.fixture()
        self.interrupt(fixture, 'validating')
        (fixture.source / 'user-note.txt').write_text('preserve concurrent user changes')
        engine, context = self.resumer(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('completed task replayed')):
            result = self.continue_cycle(engine, context)
        self.assertEqual(result['phase'], 'deferred')
        self.assertEqual(result['resume_phase'], 'validating')
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
        self.assertEqual((fixture.source / 'user-note.txt').read_text(), 'preserve concurrent user changes')

    def test_changed_configuration_is_rejected_before_modifying_the_checkpoint(self):
        fixture = self.fixture()
        self.interrupt(fixture, 'implementing')
        engine, context = self.resumer(fixture)
        original = fixture.engine.path.read_bytes()
        fixture.config.max_agent_calls += 1
        with self.assertRaisesRegex(ValueError, 'identity, configuration'):
            self.continue_cycle(engine, context)
        self.assertEqual(fixture.engine.path.read_bytes(), original)
        self.assertFalse(fixture.worker.calls or fixture.reviewer.calls)

    def test_child_usage_spent_after_last_outer_save_does_not_get_a_fresh_runtime_budget(self):
        fixture = self.fixture()
        monitor = self.finished_child(fixture)
        state = json.loads((monitor / 'state.json').read_text())
        state['usage']['elapsed_s'] = fixture.config.max_runtime_s
        write_json(monitor / 'state.json', state)
        original = (monitor / 'state.json').read_bytes()
        engine, context = self.resumer(fixture)
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('exhausted cycle replayed')):
            result = self.continue_cycle(engine, context)
        self.assertEqual(result['phase'], 'stopped')
        self.assertGreaterEqual(result['elapsed_s'], fixture.config.max_runtime_s)
        self.assertEqual(result['agent_calls'], state['usage']['agent_calls'])
        self.assertEqual((monitor / 'state.json').read_bytes(), original)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def running_child_with_heartbeat(self, fixture, seconds):
        def controller(config, **kwargs):
            original_log = kwargs.get('log', lambda _: None)

            def lose_power(message):
                original_log(message)
                if 'worker status=' in message:
                    raise PowerLoss('worker persisted; review not yet completed')

            return Supervisor(config, **{**kwargs, 'log': lose_power})

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller):
            with self.assertRaises(PowerLoss):
                fixture.engine.run()
        monitor = fixture.engine.path.parent / 'monitor'
        state = json.loads((monitor / 'state.json').read_text())
        self.assertEqual(state['status'], 'running')
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 1))
        recorded = datetime(2001, 1, 1, tzinfo=timezone.utc)
        state['usage'].update(elapsed_s=2.0, recorded_at=recorded.isoformat())
        write_json(monitor / 'state.json', state)
        write_json(monitor / 'heartbeat', {'at': (recorded + timedelta(seconds=seconds)).isoformat(),
                                          'note': 'last active invocation before power loss'})
        return monitor, state, recorded

    def interrupt_after_accounting(self, engine, context):
        original_save = engine._save

        def save(phase, **fields):
            original_save(phase, **fields)
            if phase == 'reviewed':
                raise PowerLoss('outer accounting persisted before validation')

        with patch.object(engine, '_save', side_effect=save):
            with self.assertRaises(PowerLoss):
                self.continue_cycle(engine, context)

    def test_late_heartbeat_is_accounted_once_and_excludes_downtime(self):
        fixture = self.fixture()
        monitor, old_state, _ = self.running_child_with_heartbeat(fixture, 20)
        engine, context = self.resumer(fixture)
        configurations = []

        def controller(config, **kwargs):
            configurations.append(config)
            return Supervisor(config, **kwargs)

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller):
            self.interrupt_after_accounting(engine, context)
        usage = json.loads((monitor / 'state.json').read_text())['usage']
        expected = fixture.config.max_runtime_s - usage['max_runtime_s'] + usage['elapsed_s']
        self.assertGreaterEqual(usage['elapsed_s'], 22)
        self.assertGreaterEqual(engine.record['elapsed_s'] + .002, expected)
        self.assertLessEqual(engine.record['elapsed_s'], expected + 2)
        self.assertEqual(len(configurations), 1)
        self.assertTrue(configurations[0].resume)
        self.assertGreaterEqual(configurations[0].max_runtime_s, old_state['usage']['max_runtime_s'] - 2)
        previous = engine.record['elapsed_s']
        repeated, repeated_context = self.resumer(fixture)
        started = time.monotonic()
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('completed Supervisor replayed')):
            self.interrupt_after_accounting(repeated, repeated_context)
        self.assertGreaterEqual(repeated.record['elapsed_s'], previous)
        self.assertLessEqual(repeated.record['elapsed_s'] - previous, time.monotonic() - started + .05)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_supervisor_return_reconciles_new_time_before_candidate_validation(self):
        fixture = self.fixture()
        monitor, _, recorded = self.running_child_with_heartbeat(fixture, 0)
        engine, context = self.resumer(fixture)
        original_validate = engine._validate
        remaining_at_validation = []

        def controller(config, **kwargs):
            write_json(monitor / 'heartbeat', {'at': (recorded + timedelta(seconds=20)).isoformat()})
            return Supervisor(config, **kwargs)

        def validate(cwd, stage):
            if stage == 'candidate':
                usage = json.loads((monitor / 'state.json').read_text())['usage']
                expected = fixture.config.max_runtime_s - usage['max_runtime_s'] + usage['elapsed_s']
                self.assertGreaterEqual(usage['elapsed_s'], 22)
                self.assertGreaterEqual(engine.record['elapsed_s'] + .002, expected)
                self.assertLessEqual(engine._remaining(), fixture.config.max_runtime_s - expected + .05)
                remaining_at_validation.append(engine._remaining())
            return original_validate(cwd, stage)

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller), \
             patch.object(engine, '_validate', side_effect=validate):
            result = self.continue_cycle(engine, context)
        self.assert_promoted_once(fixture, engine, result, fixture.engine.path)
        self.assertEqual(len(remaining_at_validation), 1)

    def test_heartbeat_runtime_exhaustion_stops_after_real_resume_without_upgrading(self):
        fixture = self.fixture()
        monitor, _, recorded = self.running_child_with_heartbeat(fixture, 0)
        engine, context = self.resumer(fixture)
        configurations = []

        def controller(config, **kwargs):
            configurations.append(config)
            write_json(monitor / 'heartbeat', {'at': (recorded + timedelta(seconds=90)).isoformat()})
            return Supervisor(config, **kwargs)

        with patch('auto_moniter.evolution.Supervisor', side_effect=controller), \
             patch.object(engine, '_validate', side_effect=AssertionError('exhausted validation started')):
            result = self.continue_cycle(engine, context)
        self.assertEqual(len(configurations), 1)
        self.assertTrue(configurations[0].resume)
        self.assertEqual(result['phase'], 'stopped', result.get('summary'))
        self.assertGreaterEqual(result['elapsed_s'], fixture.config.max_runtime_s)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
        previous = result['elapsed_s']
        repeated, repeated_context = self.resumer(fixture)
        started = time.monotonic()
        with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('exhausted Supervisor replayed')):
            again = self.continue_cycle(repeated, repeated_context)
        self.assertEqual(again['phase'], 'stopped')
        self.assertGreaterEqual(again['elapsed_s'], previous)
        self.assertLessEqual(again['elapsed_s'] - previous, time.monotonic() - started + .05)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 1))
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)


if __name__ == '__main__':
    unittest.main()
