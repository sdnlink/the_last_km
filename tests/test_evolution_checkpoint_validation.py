"""Checkpoint reads validate saved facts without performing recovery or writes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution
from auto_moniter.evolution_checkpoint import CheckpointError, load_checkpoint
from auto_moniter.providers import Provider
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig


class EvolutionCheckpointValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'runtime.py').write_text('original source\n')
        self.directory = self.root / 'private' / 'evolution'
        self.cycle = self.directory / 'cycle-1'
        self.cycle.mkdir(parents=True, mode=0o700)
        self.path = self.cycle / 'evolution.json'
        config = RunConfig('repair', self.source, Provider('worker', 'worker'),
                           reviewer=Provider('reviewer', 'reviewer'), model='model-a',
                           reviewer_model='model-b', max_runtime_s=120, max_agent_calls=8,
                           quality_policy={'max_tokens': 1000, 'max_cost_usd': 2})
        self.engine = Evolution(config, directory=self.directory, log=lambda _: None)
        self.engine.path = self.path
        self.engine.started = 100
        self.engine.record = {'version': 1, 'source': str(self.source),
                              'base': {'ref': 'refs/heads/main', 'commit': 'a' * 40},
                              'workspace': str(self.cycle / 'workspace')}
        clock = patch('auto_moniter.evolution.time.monotonic', return_value=105)
        clock.start()
        self.addCleanup(clock.stop)
        self.engine._save('selected')
        self.new_record = json.loads(self.path.read_text())

    def snapshot(self):
        result = {}
        for path in [self.root, *sorted(self.root.rglob('*'))]:
            info = path.lstat()
            content = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            result[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns, content)
        return result

    def reject(self, *, path=None, source=None, purpose='resume', pattern=None, **kwargs):
        before = self.snapshot()
        with self.assertRaisesRegex(CheckpointError, pattern or '.'):
            load_checkpoint(path or self.path, source or self.source, purpose=purpose, **kwargs)
        self.assertEqual(self.snapshot(), before)

    def legacy_publication(self, phase='ready'):
        workspace = self.cycle / 'workspace'
        monitor = self.cycle / 'monitor'
        validation = self.cycle / 'validation'
        for directory in (workspace, monitor, validation):
            directory.mkdir(exist_ok=True)
        candidate = 'b' * 40
        history = {'business_head': {'ref': 'refs/heads/evolution/test', 'commit': candidate},
                   'last_business_commit': {'commit': candidate}}
        usage = {'agent_calls': 3, 'elapsed_s': 20, 'max_runtime_s': 111, 'max_agent_calls': 8}
        write_json(monitor / 'state.json', {'cwd': str(workspace), 'state_version': 1,
                   'status': 'done', 'git_history': history, 'usage': usage})
        write_json(monitor / 'report.json', {'status': 'done', 'rounds': 1, 'summary': 'accepted',
                   'stop_reason': 'done', 'git_history': history, 'usage': usage})
        commands = ['python -m compileall -q auto_moniter console tests',
                    'python -m unittest discover -q', 'python -m auto_moniter selftest',
                    'git diff --check ' + 'a' * 40 + '..HEAD']
        rows = []
        for index, command in enumerate(commands, 1):
            name = f'candidate-{index:02d}.log'
            content = ('$ ' + command + '\npassed\n').encode()
            (self.cycle / name).write_bytes(content)
            rows.append({'command': command, 'exit_code': 0, 'timed_out': False,
                         'interruption': '', 'source_changed': False, 'log': name,
                         'sha256': hashlib.sha256(content).hexdigest()})
        record = {'version': 1, 'source': str(self.source), 'base': self.new_record['base'],
                  'workspace': str(workspace), 'monitor_run': str(monitor),
                  'validation_workspace': str(validation), 'phase': phase,
                  'candidate': candidate, 'candidate_checks': rows, 'monitor_status': 'done',
                  'elapsed_s': 30, 'agent_calls': 3}
        write_json(self.path, record)
        return record

    def test_real_writer_checkpoint_returns_concrete_identity_paths_and_usage(self):
        monitor = self.cycle / 'monitor'
        self.engine._save('implementing', monitor_run=str(monitor))
        self.engine._save('reviewed', agent_calls=3, elapsed_s=20)
        before = self.snapshot()
        context = load_checkpoint(self.path, self.source, allowed_versions=(2,))
        self.assertEqual(context['path'], self.path)
        self.assertEqual(context['directory'], self.cycle)
        self.assertEqual(context['source'], self.source)
        self.assertEqual(context['workspace'], self.cycle / 'workspace')
        self.assertEqual(context['monitor_run'], monitor)
        self.assertEqual(context['phase'], 'reviewed')
        self.assertEqual(context['resume_phase'], 'reviewed')
        self.assertEqual(context['config_identity']['worker']['name'], 'worker')
        self.assertEqual(context['config_identity']['reviewer_model'], 'model-b')
        self.assertEqual(context['limits']['max_runtime_s'], 120)
        self.assertEqual(context['limits']['max_agent_calls'], 8)
        self.assertEqual(context['usage']['agent_calls'], 3)
        self.assertEqual(context['usage']['elapsed_s'], 20)
        self.assertTrue(context['budget_verified'])
        self.assertFalse(context['legacy'])
        self.assertEqual(context['record'], json.loads(self.path.read_text()))
        self.assertEqual(self.snapshot(), before)

    def test_stopped_checkpoint_keeps_last_trusted_phase_and_stop_reason(self):
        self.engine._save('baseline')
        self.engine._save('stopped', summary='runtime budget exhausted')
        context = load_checkpoint(self.path, self.source)
        self.assertEqual(context['phase'], 'stopped')
        self.assertEqual(context['resume_phase'], 'baseline')
        self.assertEqual(context['record']['stop_reason'], 'runtime budget exhausted')

    def test_complete_legacy_publication_is_promotion_only_with_unknown_original_budgets(self):
        record = self.legacy_publication()
        before = self.snapshot()
        context = load_checkpoint(self.path, self.source, purpose='promotion', allowed_versions=(1,))
        self.assertTrue(context['legacy'])
        self.assertTrue(context['promotion_only'])
        self.assertFalse(context['budget_verified'])
        self.assertIsNone(context['config_identity'])
        self.assertIsNone(context['limits'])
        self.assertEqual(context['version'], 1)
        self.assertEqual(context['resume_phase'], 'ready')
        self.assertEqual(context['usage'], {'agent_calls': 3, 'elapsed_s': 30})
        self.assertEqual(context['record'], record)
        self.assertEqual(self.snapshot(), before)
        inspected = load_checkpoint(self.path, self.source, purpose='inspect')
        self.assertEqual(inspected['record'], record)
        self.reject(pattern='Legacy.*promotion')

    def test_legacy_exception_does_not_invent_an_unrecorded_resume_phase(self):
        self.legacy_publication('recovery_required')
        context = load_checkpoint(self.path, self.source, purpose='promotion')
        self.assertIsNone(context['resume_phase'])
        self.assertIsNone(context['limits'])

    def test_legacy_acceptance_requires_complete_matching_review_and_logs(self):
        good = self.legacy_publication()
        for mutation in ('missing_checks', 'failed_check', 'changed_log', 'wrong_candidate', 'missing_report'):
            with self.subTest(mutation=mutation):
                record = self.legacy_publication()
                if mutation == 'missing_checks':
                    record['candidate_checks'] = record['candidate_checks'][:1]
                elif mutation == 'failed_check':
                    record['candidate_checks'][0]['exit_code'] = 1
                elif mutation == 'changed_log':
                    (self.cycle / 'candidate-01.log').write_text('substituted')
                elif mutation == 'wrong_candidate':
                    record['candidate'] = 'c' * 40
                elif mutation == 'missing_report':
                    (self.cycle / 'monitor' / 'report.json').unlink()
                write_json(self.path, record)
                self.reject(purpose='promotion')
        write_json(self.path, good)

    def test_unknown_versions_and_invalid_purpose_are_rejected(self):
        for version in (0, 3, True, '2', None):
            with self.subTest(version=version):
                write_json(self.path, {**self.new_record, 'version': version})
                self.reject(pattern='version')
        write_json(self.path, self.new_record)
        self.reject(allowed_versions=(1,), pattern='version')
        self.reject(allowed_versions=(2, 3), pattern='version')
        self.reject(purpose='execute', pattern='purpose')

    def test_truncated_duplicate_nonobject_and_nonfinite_json_are_rejected(self):
        for data in ('{"version":', '[]', '{"version":2,"version":1}',
                     '{"version":NaN}', '{"version":Infinity}', '{"version":1e400}', '\xff'):
            with self.subTest(data=data):
                self.path.write_bytes(data.encode('latin1'))
                self.reject(pattern='JSON|object')

    def test_negative_nonfinite_boolean_and_string_usage_are_rejected(self):
        cases = [('agent_calls', -1), ('elapsed_s', -0.5), ('agent_calls', True),
                 ('elapsed_s', '5'), ('elapsed_s', float('nan')), ('elapsed_s', float('inf'))]
        for field, value in cases:
            for nested in (True, False):
                with self.subTest(field=field, value=value, nested=nested):
                    record = copy.deepcopy(self.new_record)
                    (record['usage'] if nested else record)[field] = value
                    write_json(self.path, record)
                    self.reject()
        record = copy.deepcopy(self.new_record)
        record['usage']['provider_reported'] = {'tokens': -1, 'cost_usd': 0}
        write_json(self.path, record)
        self.reject(pattern='provider_reported')

    def test_missing_identity_budgets_or_known_usage_are_not_backfilled(self):
        for missing in (('config_identity',), ('config_identity', 'worker'),
                        ('config_identity', 'policy_sha256'), ('config_identity', 'limits', 'max_runtime_s'),
                        ('config_identity', 'limits', 'max_agent_calls'), ('usage', 'elapsed_s')):
            with self.subTest(missing=missing):
                record = copy.deepcopy(self.new_record)
                parent = record
                for key in missing[:-1]:
                    parent = parent[key]
                del parent[missing[-1]]
                write_json(self.path, record)
                self.reject()
        record = copy.deepcopy(self.new_record)
        record['usage']['max_runtime_s'] = 999
        write_json(self.path, record)
        self.reject(pattern='disagree')

    def test_wrong_project_cycle_and_workspace_escapes_are_rejected(self):
        other = self.root / 'other-source'
        other.mkdir()
        self.reject(source=other, pattern='another source')
        for key in ('workspace', 'monitor_run', 'validation_workspace'):
            with self.subTest(key=key):
                write_json(self.path, {**self.new_record, key: str(self.root / 'outside')})
                self.reject(pattern='boundary')
        write_json(self.path, {**self.new_record, 'workspace': str(self.cycle / '..' / 'workspace')})
        self.reject(pattern='canonical')
        write_json(self.path, self.new_record)
        moved = self.directory / 'cycle-2'
        moved.mkdir()
        (moved / 'evolution.json').write_bytes(self.path.read_bytes())
        self.reject(path=moved / 'evolution.json', pattern='boundary')
        inside = self.source / 'evolution.json'
        inside.write_bytes(self.path.read_bytes())
        self.reject(path=inside, pattern='outside')

    def test_unknown_phases_and_inconsistent_resume_cursors_are_rejected(self):
        for fields in ({'phase': 'planned'}, {'resume_phase': 'failed'}, {'resume_phase': 'baseline'},
                       {'phase': []}, {'resume_phase': []}, {'stop_reason': 1},
                       {'phase': 'implementing', 'resume_phase': 'implementing'}):
            with self.subTest(fields=fields):
                write_json(self.path, {**self.new_record, **fields})
                self.reject(pattern='phase|cursor|reason|monitor')

    def test_symlink_checkpoint_ancestor_workspace_and_evidence_are_rejected(self):
        target = self.cycle / 'saved.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.reject(pattern='Symbolic')
        self.path.unlink()
        target.rename(self.path)
        alias = self.root / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.reject(path=alias / self.cycle.name / self.path.name, pattern='Symbolic')
        workspace = self.cycle / 'workspace'
        workspace.symlink_to(self.source, target_is_directory=True)
        self.reject(pattern='Symbolic')
        workspace.unlink()
        self.legacy_publication()
        log = self.cycle / 'candidate-01.log'
        saved_log = self.cycle / 'saved.log'
        log.rename(saved_log)
        log.symlink_to(saved_log)
        self.reject(purpose='promotion', pattern='Symbolic')

    def test_successful_inspection_and_failed_reads_never_create_or_rewrite_state(self):
        state = self.root / 'never-created-state'
        before = self.snapshot()
        with patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(state)}), \
             patch('pathlib.Path.mkdir', side_effect=AssertionError('reader must not create directories')), \
             patch('subprocess.run', side_effect=AssertionError('reader must not execute commands')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('reader must not call a model')):
            first = load_checkpoint(self.path, self.source, purpose='inspect')
            second = load_checkpoint(self.path, self.source, purpose='inspect')
            self.assertEqual(first, second)
            with self.assertRaises(CheckpointError):
                load_checkpoint(state / 'cycle' / 'evolution.json', self.source, purpose='inspect')
        self.assertFalse(state.exists())
        self.assertEqual(self.snapshot(), before)
        with patch('auto_moniter.evolution_checkpoint.os.open', side_effect=PermissionError('injected read failure')):
            self.reject(pattern='Cannot read')

    def test_arbitrary_historical_commands_are_not_fixed_gate_evidence_or_executed(self):
        record = self.legacy_publication()
        marker = self.root / 'must-not-be-created'
        command = 'touch ' + str(marker)
        log = self.cycle / 'candidate-01.log'
        log.write_text('$ ' + command + '\npassed\n')
        record['candidate_checks'][0].update(command=command, sha256=hashlib.sha256(log.read_bytes()).hexdigest())
        write_json(self.path, record)
        with patch('subprocess.run', side_effect=AssertionError('historical commands are data')):
            self.reject(purpose='promotion', pattern='fixed checks')
        self.assertFalse(marker.exists())

    def test_invalid_configuration_budget_types_are_rejected(self):
        for name, value in (('max_runtime_s', -1), ('max_runtime_s', float('inf')),
                            ('max_agent_calls', True), ('memory_bytes', '4096'),
                            ('max_cost_usd', float('nan')), ('max_output_bytes', -1)):
            with self.subTest(name=name, value=value):
                record = copy.deepcopy(self.new_record)
                record['config_identity']['limits'][name] = value
                write_json(self.path, record)
                self.reject()

    def test_legacy_final_report_requires_rounds_summary_and_stop_reason(self):
        self.legacy_publication()
        report_path = self.cycle / 'monitor' / 'report.json'
        complete = json.loads(report_path.read_text())
        invalid_values = {'rounds': (-1, True, 1.5, '1', None),
                          'summary': (None, 1, {}), 'stop_reason': (None, 1, [])}
        for field, values in invalid_values.items():
            for present, value in [(False, None), *((True, value) for value in values)]:
                with self.subTest(field=field, present=present, value=value):
                    damaged = copy.deepcopy(complete)
                    if present:
                        damaged[field] = value
                    else:
                        del damaged[field]
                    write_json(report_path, damaged)
                    self.reject(purpose='promotion', pattern='report')
        write_json(report_path, complete)
        before = self.snapshot()
        context = load_checkpoint(self.path, self.source, purpose='promotion')
        self.assertEqual(context['phase'], 'ready')
        self.assertEqual(context['record']['candidate'], 'b' * 40)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
