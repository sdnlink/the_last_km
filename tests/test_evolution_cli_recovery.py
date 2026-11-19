"""Verify frozen cycle budgets through the actual evolve CLI entry point."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import time
import unittest
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.evolution import Evolution, git
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig
from tests import test_evolution_phase_execution as phase_fixtures


class EvolutionCLIRecoveryTests(unittest.TestCase):
    def test_rollback_recovery_preserves_source_returned_to_baseline(self):
        fixture = self.fixture
        with patch.object(fixture.engine, '_activate', return_value=False):
            path = self.phases.interrupt(fixture, 'rolling_back')
        saved = path.read_bytes()
        record = json.loads(saved)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), record['candidate'])
        git(fixture.source, 'reset', '--hard', fixture.base)  # This repository is isolated.
        refs = git(fixture.source, 'show-ref')
        objects = git(fixture.source, 'count-objects', '-v')
        with patch.object(Evolution, '_activate', side_effect=AssertionError('candidate was reactivated')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('unexpected dispatch')):
            for options in (('--plan-only',), (), ('--recover', str(path.parent))):
                with self.subTest(options=options):
                    code, cycles = self.invoke(*options)
                    self.assertEqual(code, 1, cycles)
                    self.assertEqual(cycles[0]['phase'], 'deferred')
                    self.assertIn('rollback source', cycles[0]['summary'])
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
        self.assertEqual(git(fixture.source, 'show-ref'), refs)
        self.assertEqual(git(fixture.source, 'count-objects', '-v'), objects)
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])

    def setUp(self):
        self.phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(self.phases.doCleanups)
        self.fixture = self.phases.fixture()
        fixture = self.fixture
        # The previous CLI persisted the remaining grant, including fractions
        # of a second and calls already spent in earlier cycles.
        fixture.config = RunConfig('', fixture.source, fixture.worker, reviewer=fixture.reviewer,
                                   max_runtime_s=89.75, max_agent_calls=5)
        fixture.engine = Evolution(fixture.config, evidence_roots=[fixture.evidence], log=lambda _: None)

    def invoke(self, *options, runtime='90s', calls='8'):
        fixture = self.fixture
        args = ['evolve', '--cwd', str(fixture.source), '--evidence-root', str(fixture.evidence),
                '--max-runtime', runtime, '--max-agent-calls', calls, *options]

        def provider(_name, *, role='worker'):
            return fixture.reviewer if role == 'reviewer' else fixture.worker

        with patch('auto_moniter.cli.resolve_provider', side_effect=provider), \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = main(args)
        self.assertTrue(stdout.getvalue(), stderr.getvalue())
        return code, json.loads(stdout.getvalue())['cycles']

    def test_selected_interruption_resumes_under_original_grant(self):
        fixture = self.fixture
        path = self.phases.interrupt(fixture, 'selected')
        saved = path.read_bytes()
        code, cycles = self.invoke('--plan-only')
        self.assertEqual(code, 0, cycles)
        self.assertEqual(cycles[0]['recovery_directory'], str(path.parent))
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (0, 0))

        code, cycles = self.invoke()
        self.assertEqual(code, 0, cycles)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]['phase'], 'promoted')
        self.assertEqual(cycles[0]['usage']['max_runtime_s'], 89.75)
        self.assertEqual(cycles[0]['usage']['max_agent_calls'], 5)
        self.assertEqual(cycles[0]['agent_calls'], 3)
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '2')

    def test_cli_rejects_insufficient_authorization_and_changed_model(self):
        fixture = self.fixture
        path = self.phases.interrupt(fixture, 'selected')
        saved = path.read_bytes()
        for options, budget in [((), {'runtime': '89s'}), ((), {'calls': '4'}),
                                (('--model', 'different'), {})]:
            with self.subTest(options=options, budget=budget):
                code, cycles = self.invoke(*options, **budget)
                self.assertEqual(code, 1, cycles)
                self.assertEqual(cycles[0]['phase'], 'deferred')
                self.assertIn('configuration or original budgets', cycles[0]['summary'])
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (0, 0))

    def test_repeated_cli_recovery_reuses_supervisor_and_complete_checks(self):
        fixture = self.fixture
        monitor, original_state, _ = self.phases.running_child_with_heartbeat(fixture, 20)
        path = fixture.engine.path
        original_save = Evolution._save

        def interrupt_after_validation(engine, phase, **fields):
            original_save(engine, phase, **fields)
            if phase == 'candidate' and fields.get('candidate_checks_complete'):
                raise phase_fixtures.PowerLoss('candidate checks saved')

        with patch.object(Evolution, '_save', interrupt_after_validation):
            with self.assertRaises(phase_fixtures.PowerLoss):
                self.invoke()
        reviewed = json.loads(path.read_text())
        self.assertEqual(reviewed['monitor_run'], str(monitor))
        self.assertEqual(reviewed['agent_calls'], original_state['usage']['agent_calls'] + 1)
        self.assertGreaterEqual(reviewed['elapsed_s'], 22)
        state_before = (monitor / 'state.json').read_bytes()
        logs_before = {log: log.read_bytes() for log in path.parent.glob('*.log')}
        self.assertTrue(logs_before)
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

        with patch.object(Evolution, '_validate', side_effect=AssertionError('checks replayed')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('accepted work replayed')):
            started = time.monotonic()
            code, cycles = self.invoke()
            resume_time = time.monotonic() - started
        self.assertEqual(code, 0, cycles)
        self.assertEqual(len(cycles), 1)
        result = cycles[0]
        self.assertEqual(result['phase'], 'promoted')
        self.assertEqual(result['agent_calls'], reviewed['agent_calls'])
        self.assertEqual(result['config_identity'], reviewed['config_identity'])
        self.assertGreaterEqual(result['elapsed_s'], reviewed['elapsed_s'])
        self.assertLessEqual(result['elapsed_s'] - reviewed['elapsed_s'], resume_time + .01)
        self.assertEqual((monitor / 'state.json').read_bytes(), state_before)
        self.assertEqual({log: log.read_bytes() for log in logs_before}, logs_before)
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '2')

    def test_cli_checks_child_limits_against_original_cycle_grant(self):
        fixture = self.fixture
        monitor, state, _ = self.phases.running_child_with_heartbeat(fixture, 0)
        path = fixture.engine.path
        for limits, message in [({'max_agent_calls': 6}, 'cannot verify evolution supervisor usage'),
                                ({'agent_calls': 5}, 'supervisor budget exhausted')]:
            with self.subTest(limits=limits):
                child = monitor / 'state.json'
                write_json(child, {**state, 'usage': {**state['usage'], **limits}})
                saved = (path.read_bytes(), child.read_bytes())
                with patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('child dispatched')):
                    code, cycles = self.invoke()
                self.assertEqual(code, 1, cycles)
                self.assertIn(message, cycles[0]['summary'].lower())
                self.assertEqual((path.read_bytes(), child.read_bytes()), saved)
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), fixture.base)

    def test_interrupted_publication_and_rollback_keep_original_commits(self):
        fixture = self.fixture
        path = self.phases.interrupt(fixture, 'ready')
        original_save = Evolution._save

        def interrupt_before_rollback_merge(engine, phase, **fields):
            original_save(engine, phase, **fields)
            if phase == 'rolling_back':
                raise phase_fixtures.PowerLoss('rollback commit saved before fast-forward')

        with patch.object(Evolution, '_validate', side_effect=AssertionError('verified checks replayed')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('implementation replayed')):
            with patch.object(Evolution, '_activate', return_value=False), \
                 patch.object(Evolution, '_save', interrupt_before_rollback_merge):
                with self.assertRaises(phase_fixtures.PowerLoss):
                    self.invoke()
            saved = json.loads(path.read_text())
            self.assertEqual(saved['phase'], 'rolling_back')
            self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), saved['candidate'])

            code, cycles = self.invoke()
            self.assertEqual(code, 1, cycles)
            self.assertEqual(len(cycles), 1)
            self.assertEqual(cycles[0]['phase'], 'rolled_back')
            self.assertEqual(cycles[0]['config_identity'], saved['config_identity'])
            self.assertEqual(cycles[0]['agent_calls'], saved['agent_calls'])
            final = path.read_bytes()
            with patch.object(Evolution, '_activate', side_effect=AssertionError('completed rollback replayed')):
                _, cycles = self.invoke('--recover', str(path.parent))
            self.assertEqual(cycles[0]['phase'], 'rolled_back')

        self.assertEqual(path.read_bytes(), final)
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD'), saved['rollback_commit'])
        self.assertEqual(git(fixture.source, 'rev-list', '--count', 'HEAD'), '3')
        self.assertEqual(git(fixture.source, 'rev-parse', 'HEAD^{tree}'),
                         git(fixture.source, 'rev-parse', fixture.base + '^{tree}'))
