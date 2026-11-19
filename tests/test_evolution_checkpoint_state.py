"""Evolution checkpoints retain a safe cursor and the cycle's budget history."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution
from auto_moniter.providers import CommandProvider, Provider
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig


class EvolutionCheckpointStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'runtime.py').write_text('original runtime\n')
        self.directory = self.root / 'private-evolution'
        self.cycle = self.directory / 'cycle-1'
        self.cycle.mkdir(parents=True, mode=0o700)
        self.config = RunConfig('repair the monitor', self.source, Provider('worker', 'worker'),
                                reviewer=Provider('reviewer', 'reviewer'), model='worker-model',
                                reviewer_model='review-model', max_runtime_s=120, max_agent_calls=8,
                                memory_bytes=4096, quality_policy={'max_tokens': 1000, 'max_cost_usd': 2})
        self.engine = Evolution(self.config, directory=self.directory, log=lambda _: None)
        self.engine.started = 100
        self.engine.path = self.cycle / 'evolution.json'
        self.engine.record = {'version': 1, 'source': str(self.source),
                              'base': {'ref': 'refs/heads/main', 'commit': 'a' * 40},
                              'workspace': str(self.cycle / 'workspace')}
        clock = patch('auto_moniter.evolution.time.monotonic', return_value=110)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def load(self):
        return json.loads(self.engine.path.read_text())

    def test_phase_transition_persists_versioned_cursor_identity_and_budgets(self):
        self.engine._save('selected')
        self.engine._save('implementing', monitor_run=str(self.cycle / 'monitor'))
        saved = self.load()
        self.assertEqual(saved['version'], 2)
        self.assertEqual(saved['resume_phase'], 'implementing')
        self.assertEqual(saved['base'], self.engine.record['base'])
        self.assertEqual(saved['monitor_run'], str(self.cycle / 'monitor'))
        identity = saved['config_identity']
        self.assertEqual(identity['worker']['name'], 'worker')
        self.assertEqual(identity['reviewer']['name'], 'reviewer')
        self.assertEqual(identity['model'], 'worker-model')
        self.assertEqual(identity['reviewer_model'], 'review-model')
        self.assertEqual(identity['limits']['max_runtime_s'], 120)
        self.assertEqual(identity['limits']['max_agent_calls'], 8)
        self.assertEqual(identity['limits']['memory_bytes'], 4096)
        self.assertEqual(identity['limits']['max_tokens'], 1000)
        self.assertEqual(identity['limits']['max_cost_usd'], 2)
        self.assertEqual(saved['usage']['max_runtime_s'], 120)
        self.assertEqual(saved['usage']['max_agent_calls'], 8)

    def test_terminal_and_exception_wrappers_retain_last_trusted_phase_and_reason(self):
        for phase in ('stopped', 'deferred', 'baseline_failed', 'rejected', 'candidate_failed',
                      'recovery_required', 'rollback_activation_failed', 'failed'):
            with self.subTest(phase=phase):
                self.engine._save('promoting')
                self.engine._save(phase, summary='interrupted at promotion')
                self.engine._save('failed', summary='outer exception')
                saved = self.load()
                self.assertEqual(saved['phase'], 'failed')
                self.assertEqual(saved['resume_phase'], 'promoting')
                self.assertEqual(saved['stop_reason'], 'outer exception')
        self.engine._save('ready')
        self.assertEqual(self.load()['stop_reason'], '')

    def test_known_usage_never_decreases_or_double_counts_on_repeated_save(self):
        self.engine._save('reviewed', agent_calls=5, elapsed_s=40)
        self.engine.record['agent_calls'] = 1  # Existing exception collection can replace this field.
        self.engine._save('failed', agent_calls=2, elapsed_s=3,
                          usage={'agent_calls': 1, 'elapsed_s': 2})
        saved = self.load()
        self.assertEqual(saved['agent_calls'], 5)
        self.assertEqual(saved['usage']['agent_calls'], 5)
        self.assertEqual(saved['elapsed_s'], 40)
        self.assertEqual(saved['usage']['elapsed_s'], 40)
        self.engine._save('failed', agent_calls=5)
        self.assertEqual(self.load()['usage']['agent_calls'], 5)
        self.assertEqual(self.load()['usage']['elapsed_s'], 40)

    def test_loaded_checkpoint_adds_new_active_time_without_resetting_limits(self):
        self.engine._save('reviewed', agent_calls=5, elapsed_s=40)
        saved = self.load()
        resumed = Evolution(self.config, directory=self.directory, log=lambda _: None)
        resumed.path = self.engine.path
        resumed.record = saved
        resumed.started = 110
        self.clock.return_value = 117
        resumed._save('validating', agent_calls=1)
        self.assertEqual(resumed.record['elapsed_s'], 47)
        self.assertEqual(resumed.record['agent_calls'], 5)
        self.clock.return_value = 118
        resumed._save('candidate')
        self.assertEqual(resumed.record['elapsed_s'], 48)
        self.config.max_runtime_s = 999
        self.config.max_agent_calls = 999
        resumed._save('stopped')
        self.assertEqual(resumed.record['config_identity'], saved['config_identity'])
        self.assertEqual(resumed.record['usage']['max_runtime_s'], 120)
        self.assertEqual(resumed.record['usage']['max_agent_calls'], 8)

    def test_config_identity_excludes_credentials_environment_and_executable_history(self):
        secret = 'checkpoint-private-credential-23456789'
        history = 'sh -c "execute-old-history-marker"'
        self.config.requirement = history + secret
        self.config.acceptance = history
        self.config.extra_args = ['--api-key', secret, history]
        self.config.provider = CommandProvider(history + secret, history)
        self.config.provider.profile = {'api_key': secret, 'unrelated': 'profile-private-marker'}
        with patch.dict(os.environ, {'CHECKPOINT_API_KEY': secret, 'CHECKPOINT_OTHER': 'environment-full-marker'}):
            self.engine._save('selected', summary='bearer ' + secret)
        raw = self.engine.path.read_text()
        for value in (secret, history, 'profile-private-marker', 'environment-full-marker', 'CHECKPOINT_OTHER'):
            self.assertNotIn(value, raw)
        identity = self.load()['config_identity']
        self.assertEqual(identity['worker']['name'], 'command')
        self.assertEqual(len(identity['worker']['command_hash']), 64)
        self.assertEqual(len(identity['requirement_sha256']), 64)

    def test_failed_write_preserves_previous_bytes_record_and_source(self):
        self.engine._save('baseline', agent_calls=3)
        original = self.engine.path.read_bytes()
        record = copy.deepcopy(self.engine.record)
        source = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        with patch('auto_moniter.evolution.write_json', side_effect=OSError('injected persistence failure')), \
             patch('auto_moniter.evolution.git', side_effect=AssertionError('checkpoint must not operate on Git')):
            with self.assertRaisesRegex(OSError, 'injected persistence failure'):
                self.engine._save('implementing', agent_calls=7)
        self.assertEqual(self.engine.path.read_bytes(), original)
        self.assertEqual(self.engine.record, record)
        self.assertEqual({p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}, source)
        self.assertEqual(list(self.cycle.iterdir()), [self.engine.path])

    def test_failure_after_atomic_replace_restores_last_valid_checkpoint(self):
        self.engine._save('ready', agent_calls=4)
        original = self.engine.path.read_bytes()
        record = copy.deepcopy(self.engine.record)

        def fail_after_publish(*args, **kwargs):
            write_json(*args, **kwargs)
            raise OSError('directory sync failed after replace')

        with patch('auto_moniter.evolution.write_json', side_effect=fail_after_publish):
            with self.assertRaisesRegex(OSError, 'directory sync failed'):
                self.engine._save('promoting', candidate='b' * 40)
        self.assertEqual(self.engine.path.read_bytes(), original)
        self.assertEqual(self.engine.record, record)
        self.assertEqual(list(self.cycle.iterdir()), [self.engine.path])

    def test_failed_initial_write_does_not_publish_a_partial_checkpoint(self):
        record = copy.deepcopy(self.engine.record)

        def fail_after_publish(*args, **kwargs):
            write_json(*args, **kwargs)
            raise OSError('first save interrupted')

        with patch('auto_moniter.evolution.write_json', side_effect=fail_after_publish):
            with self.assertRaisesRegex(OSError, 'first save interrupted'):
                self.engine._save('selected')
        self.assertFalse(self.engine.path.exists())
        self.assertEqual(self.engine.record, record)
        self.assertEqual(list(self.cycle.iterdir()), [])

    def test_known_provider_usage_is_retained_without_estimates_or_decreases(self):
        self.engine._save('reviewed', usage={'provider_reported': {
            'tokens': 500, 'cost_usd': 0.75, 'reported_turns': 2, 'unreported_turns': 1,
            'unknown_token_turns': 1, 'unknown_cost_turns': 1}})
        self.engine._save('failed', usage={'provider_reported': {'tokens': 100, 'cost_usd': 0.25}})
        reported = self.load()['usage']['provider_reported']
        self.assertEqual(reported['tokens'], 500)
        self.assertEqual(reported['cost_usd'], 0.75)
        self.assertEqual(reported['reported_turns'], 2)
        self.assertEqual(reported['unknown_token_turns'], 1)
        self.assertEqual(reported['unknown_cost_turns'], 1)

    def test_invalid_usage_cannot_replace_the_last_trustworthy_checkpoint(self):
        self.engine._save('baseline', agent_calls=3)
        original = self.engine.path.read_bytes()
        record = copy.deepcopy(self.engine.record)
        for fields in ({'agent_calls': -1}, {'agent_calls': True},
                       {'elapsed_s': float('nan')}, {'usage': {'elapsed_s': float('inf')}}):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, 'Invalid evolution usage'):
                    self.engine._save('failed', **fields)
                self.assertEqual(self.engine.path.read_bytes(), original)
                self.assertEqual(self.engine.record, record)

    def test_legacy_checkpoint_cannot_acquire_current_identity_or_budgets(self):
        legacy = {**self.engine.record, 'phase': 'ready', 'candidate': 'b' * 40,
                  'agent_calls': 4, 'elapsed_s': 40}
        write_json(self.engine.path, legacy, durable=True)
        self.engine.record = self.load()
        original = self.engine.path.read_bytes()
        record = copy.deepcopy(self.engine.record)
        self.config.max_runtime_s = 999
        self.config.max_agent_calls = 999
        source = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        with patch('auto_moniter.evolution.write_json', wraps=write_json) as persist:
            with self.assertRaisesRegex(ValueError, 'Cannot verify evolution checkpoint'):
                self.engine._save('activating')
            persist.assert_not_called()
        self.assertEqual(self.engine.path.read_bytes(), original)
        self.assertEqual(self.engine.record, record)
        self.assertEqual({p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}, source)
        self.assertEqual(list(self.cycle.iterdir()), [self.engine.path])

    def test_existing_checkpoint_requires_complete_historical_identity_and_budgets(self):
        self.engine._save('ready', agent_calls=4)
        complete = self.load()
        self.config.max_runtime_s = 999
        self.config.max_agent_calls = 999
        missing_paths = (
            ('config_identity',),
            ('config_identity', 'policy_sha256'),
            ('config_identity', 'worker', 'command_hash'),
            ('config_identity', 'limits'),
            ('config_identity', 'limits', 'max_runtime_s'),
            ('config_identity', 'limits', 'max_agent_calls'),
        )
        for missing in missing_paths:
            with self.subTest(missing=missing):
                damaged = copy.deepcopy(complete)
                parent = damaged
                for key in missing[:-1]:
                    parent = parent[key]
                del parent[missing[-1]]
                write_json(self.engine.path, damaged, durable=True)
                self.engine.record = self.load()
                original = self.engine.path.read_bytes()
                record = copy.deepcopy(self.engine.record)
                with patch('auto_moniter.evolution.write_json', wraps=write_json) as persist:
                    with self.assertRaisesRegex(ValueError, 'Cannot verify evolution checkpoint'):
                        self.engine._save('activating')
                    persist.assert_not_called()
                self.assertEqual(self.engine.path.read_bytes(), original)
                self.assertEqual(self.engine.record, record)

    def test_historical_record_without_a_file_is_not_a_new_cycle(self):
        self.engine.record.update(phase='ready', agent_calls=4, elapsed_s=40)
        record = copy.deepcopy(self.engine.record)
        with self.assertRaisesRegex(ValueError, 'Cannot verify evolution checkpoint'):
            self.engine._save('activating')
        self.assertEqual(self.engine.record, record)
        self.assertFalse(self.engine.path.exists())


if __name__ == '__main__':
    unittest.main()
