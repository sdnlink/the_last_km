"""Explicit evolution admission through the CLI with disposable history."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.evolution import git, issues
from auto_moniter.paths import project_runs_dir
from auto_moniter.storage import write_json
from auto_moniter.workspace import file_lock
from tests import test_evolution as fixtures


class ExplicitNewSessionTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.EvolutionTests('test_plan_only_reads_faults_without_writing_or_calling_models')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.source = fixture.source
        self.base = {'commit': fixture.base, 'ref': git(self.source, 'symbolic-ref', 'HEAD')}
        self.directory = project_runs_dir(self.source).parent / 'evolution'

    def legacy(self, phase, status='failed'):
        ident = f'{len(list(self.directory.glob("*/evolution.json"))):03d}-{phase}'
        directory = self.directory / ident
        directory.mkdir(parents=True)
        work = directory / 'workspace'
        git(self.source, 'worktree', 'add', '-b', 'evolution/' + ident, str(work), self.base['commit'])
        monitor = directory / 'monitor'
        monitor.mkdir()
        record = {'version': 1, 'source': str(self.source), 'base': self.base,
                  'workspace': str(work), 'phase': phase, 'elapsed_s': 11,
                  'issue': issues([self.fixture.evidence])[0]}
        if phase == 'baseline_failed':
            command = shlex.quote(sys.executable) + ' -m compileall -q auto_moniter console tests'
            log = directory / 'baseline-01.log'
            log.write_text('$ ' + command + '\nfixture failure\n')
            record['baseline_checks'] = [{'command': command, 'log': log.name, 'exit_code': 1,
                'source_changed': False, 'timed_out': False, 'interruption': '',
                'sha256': hashlib.sha256(log.read_bytes()).hexdigest()}]
        else:
            usage = {'agent_calls': 2, 'max_agent_calls': 2 if status == 'budget_exceeded' else 8,
                     'elapsed_s': 10, 'max_runtime_s': 90}
            evidence = {'status': status, 'stop_reason': status, 'summary': 'Historical review ended',
                        'usage': usage}
            write_json(monitor / 'state.json', {**evidence, 'cwd': str(work)})
            write_json(monitor / 'report.json', {**evidence, 'rounds': 1})
            (monitor / 'supervisor.lock').touch()
            record.update(monitor_run=str(monitor), monitor_status=status, agent_calls=2)
        path = directory / 'evolution.json'
        write_json(path, record)
        return path

    def invoke(self, *options, calls='8', runtime='90s'):
        args = ['evolve', '-C', str(self.source), '--evidence-root', str(self.fixture.evidence),
                '--max-runtime', runtime, '--max-agent-calls', calls, *options]

        def provider(_name, *, role='worker'):
            return self.fixture.reviewer if role == 'reviewer' else self.fixture.worker

        with patch('auto_moniter.cli.resolve_provider', side_effect=provider), \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = main(args)
        self.assertTrue(stdout.getvalue(), stderr.getvalue())
        return code, json.loads(stdout.getvalue())

    def snapshot(self):
        return {str(path.relative_to(self.fixture.root)): path.read_bytes() if path.is_file() else None
                for path in self.fixture.root.rglob('*')}

    def assert_legacy_refused(self, phase, status):
        self.legacy(phase, status)
        before = self.snapshot()
        code, result = self.invoke('--plan-only')
        self.assertEqual((code, result['agent_calls']), (1, 0))
        self.assertEqual(result['cycles'][0]['phase'], 'deferred')
        self.assertIn('Legacy checkpoint supports evidenced promotion only', result['cycles'][0]['summary'])
        self.assertEqual(self.snapshot(), before)

    def test_original_plan_refuses_legacy_baseline_failure(self):
        self.assert_legacy_refused('baseline_failed', '')

    def test_original_plan_refuses_legacy_budget_rejection(self):
        self.assert_legacy_refused('rejected', 'budget_exceeded')

    def test_explicit_plan_reads_both_terminal_shapes_and_deduplicates(self):
        paths = [self.legacy('baseline_failed'), self.legacy('rejected', 'budget_exceeded')]
        before = self.snapshot()
        with patch('pathlib.Path.mkdir', side_effect=AssertionError('plan wrote a directory')):
            code, result = self.invoke('--new-session', '--plan-only')
        self.assertEqual((code, result['agent_calls']), (0, 0))
        plan = result['cycles'][0]
        self.assertEqual((plan['phase'], plan['action'], plan['base']), ('planned', 'new_session', self.base))
        self.assertEqual(plan['issue']['kind'], 'proactive_review')
        self.assertEqual({row['directory'] for row in plan['retained_legacy_history']},
                         {str(path.parent) for path in paths})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.fixture.worker.calls or self.fixture.reviewer.calls)

    def test_explicit_execution_dispatches_a_new_reviewed_cycle_with_its_own_grant(self):
        path = self.legacy('rejected', 'budget_exceeded')
        # The failed historical issue belongs to an older source baseline.
        git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
            'commit', '--allow-empty', '-qm', 'new source baseline')
        base = {'commit': git(self.source, 'rev-parse', 'HEAD'), 'ref': self.base['ref']}
        history = {p: p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
        legacy_ref = git(self.source, 'rev-parse', 'refs/heads/evolution/' + path.parent.name)

        code, result = self.invoke('--new-session', '--cycles', '1', calls='5')

        self.assertEqual(code, 0, result)
        cycle, = result['cycles']
        self.assertEqual((cycle['phase'], cycle['base']), ('promoted', base))
        self.assertEqual((cycle['usage']['max_agent_calls'], result['agent_calls']), (5, 3))
        new_path, = set(self.directory.glob('*/evolution.json')) - {path}
        saved = json.loads(new_path.read_text())
        self.assertEqual(saved['session_id'], cycle['session_id'])
        self.assertEqual(saved['config_identity']['limits']['max_agent_calls'], 5)
        self.assertEqual((len(self.fixture.worker.calls), len(self.fixture.reviewer.calls)), (1, 2))
        self.assertTrue(all(call['read_only'] for call in self.fixture.reviewer.calls))
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), cycle['promoted_commit'])
        self.assertEqual(git(self.source, 'rev-parse', 'refs/heads/evolution/' + path.parent.name), legacy_ref)
        self.assertEqual({p: p.read_bytes() for p in history}, history)
        code, result = self.invoke('--new-session', '--plan-only')
        self.assertEqual((code, result['cycles'][0]['phase']), (0, 'planned'), result)

    def test_control_arriving_at_workspace_lock_blocks_new_dispatch(self):
        path = self.legacy('rejected')
        saved = path.read_bytes()
        refs = git(self.source, 'show-ref')
        code, result = self.invoke('--new-session', '--plan-only')
        self.assertEqual((code, result['cycles'][0]['phase']), (0, 'planned'), result)

        @contextmanager
        def request_pause(lock):
            with file_lock(lock):
                if lock == self.source / '.auto-moniter/workspace.lock':
                    write_json(path.parent / 'monitor/control.json', {'action': 'pause'})
                yield

        with patch('auto_moniter.evolution.file_lock', side_effect=request_pause):
            code, result = self.invoke('--new-session')
        self.assertEqual((code, result['agent_calls']), (1, 0))
        self.assertEqual(result['cycles'][0]['phase'], 'deferred')
        self.assertIn('pause', result['cycles'][0]['summary'])
        self.assertEqual(list(self.directory.glob('*/evolution.json')), [path])
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(git(self.source, 'show-ref'), refs)
        self.assertFalse(self.fixture.worker.calls or self.fixture.reviewer.calls)

    def test_missing_child_evidence_blocks_new_dispatch(self):
        path = self.legacy('rejected', 'budget_exceeded')
        (path.parent / 'monitor/state.json').unlink()
        before = self.snapshot()
        code, result = self.invoke('--new-session', '--plan-only')
        self.assertEqual((code, result['agent_calls']), (1, 0))
        self.assertEqual(result['cycles'][0]['phase'], 'deferred')
        self.assertIn('state.json', result['cycles'][0]['summary'])
        self.assertEqual(self.snapshot(), before)

    def assert_history_deferred(self, path, reason):
        history = {p: p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
        refs = git(self.source, 'show-ref')
        cycles = set(self.directory.glob('*/evolution.json'))
        worker_calls = len(self.fixture.worker.calls)
        reviewer_calls = len(self.fixture.reviewer.calls)
        for options in (('--plan-only',), ()):
            with self.subTest(options=options):
                code, result = self.invoke('--new-session', *options)
                self.assertEqual((code, result['agent_calls']), (1, 0))
                cycle, = result['cycles']
                self.assertEqual(cycle['phase'], 'deferred')
                self.assertIn(reason, cycle['summary'])
                self.assertEqual(set(self.directory.glob('*/evolution.json')), cycles)
                self.assertEqual(git(self.source, 'show-ref'), refs)
                self.assertEqual({p: p.read_bytes() for p in history}, history)
        self.assertEqual(len(self.fixture.worker.calls), worker_calls)
        self.assertEqual(len(self.fixture.reviewer.calls), reviewer_calls)

    def test_baseline_failure_requires_fixed_command_and_matching_log_header(self):
        path = self.legacy('baseline_failed')
        record = json.loads(path.read_text())
        row, = record['baseline_checks']
        log = path.parent / row['log']
        original_command = row['command']
        with self.subTest(mismatch='command'):
            row['command'] = 'false'
            write_json(path, record)
            self.assert_history_deferred(path, 'Invalid terminal baseline check evidence')
        with self.subTest(mismatch='log header'):
            row['command'] = original_command
            log.write_text('$ false\nfixture failure\n')
            row['sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
            write_json(path, record)
            self.assert_history_deferred(path, 'Invalid terminal baseline check evidence')
        other = self.fixture.root / 'other-python'
        other.touch()
        for command in (shlex.quote(sys.executable) + ' -m unittest discover -q',
                        shlex.quote(str(other)) + ' -m compileall -q auto_moniter console tests'):
            with self.subTest(command=command):
                row['command'] = command
                log.write_text('$ ' + command + '\nfixture failure\n')
                row['sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
                write_json(path, record)
                self.assert_history_deferred(path, 'Invalid terminal baseline check evidence')

    def test_newer_supervisor_recovery_failure_blocks_plan_and_execution(self):
        path = self.legacy('rejected')
        monitor = path.parent / 'monitor'
        diagnostic = monitor / 'resume-error.json'
        write_json(diagnostic, {'status': 'recovery_failed', 'summary': 'Fixture recovery failure'})
        newest = max((monitor / name).stat().st_mtime_ns for name in ('state.json', 'report.json'))
        os.utime(diagnostic, ns=(newest + 1, newest + 1))
        self.assert_history_deferred(path, 'Supervisor recovery failed')
        lock = monitor / 'supervisor.lock'
        lock.unlink()
        self.assert_history_deferred(path, 'Supervisor lock state is unverified')
        self.assertFalse(lock.exists())

    def test_explicit_plan_accepts_equivalent_python_entry_point(self):
        path = self.legacy('baseline_failed')
        alias = self.fixture.root / 'alias with spaces' / 'python3'
        alias.parent.mkdir()
        alias.symlink_to(sys.executable)
        self.assertTrue(alias.samefile(sys.executable))
        before = self.snapshot()

        code, expected = self.invoke('--new-session', '--plan-only')
        self.assertEqual((code, expected['agent_calls']), (0, 0))
        self.assertEqual(expected['cycles'][0]['phase'], 'planned')
        self.assertEqual(expected['cycles'][0]['retained_legacy_history'][0]['directory'], str(path.parent))
        with patch('auto_moniter.evolution.sys.executable', str(alias)):
            code, actual = self.invoke('--new-session', '--plan-only')

        self.assertEqual(code, 0, actual)
        self.assertEqual((actual['cycles'], actual['agent_calls']), (expected['cycles'], 0))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.fixture.worker.calls or self.fixture.reviewer.calls)

    def test_consumed_approval_with_tracked_output_allows_a_second_cycle(self):
        build = self.source / 'build'
        build.mkdir()
        (build / 'keep.txt').write_text('tracked output\n')
        git(self.source, 'add', 'build/keep.txt')
        git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
            'commit', '-qm', 'track build output')

        self.fixture.worker.scripts.extend(deepcopy(self.fixture.worker.scripts))
        planning, approval = deepcopy(self.fixture.reviewer.scripts)
        planning['text'] = (planning['text'].replace('test_evolution_fix.py', 'test_evolution_second_fix.py')
                            .replace('repair_target', 'second_target'))
        self.fixture.reviewer.scripts.extend([planning, approval])
        worker_command = self.fixture.worker._command
        second_fix = fixtures.FIX + '\ndef second_target():\n    return 43\n'
        second_regression = fixtures.REGRESSION.replace('repair_target', 'second_target').replace('(),42', '(),43')

        def write_second_fix(*args):
            command = worker_command(*args)
            if len(self.fixture.worker.calls) == 2:
                script = Path(command.argv[-1])
                script.write_text(script.read_text().replace('test_evolution_fix', 'test_evolution_second_fix')
                                  .replace(repr(fixtures.FIX), repr(second_fix))
                                  .replace(repr(fixtures.REGRESSION), repr(second_regression)))
            return command

        with patch.object(self.fixture.worker, '_command', side_effect=write_second_fix):
            code, result = self.invoke('--new-session', '--cycles', '2', calls='7')
        self.assertEqual(code, 0, result)
        first, second = result['cycles']
        self.assertEqual((first['phase'], second['phase'], result['agent_calls']), ('promoted', 'promoted', 6))
        self.assertEqual(second['base']['commit'], first['promoted_commit'])
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertEqual(second['usage']['max_agent_calls'], 4)
        self.assertEqual(git(self.source, 'show', 'HEAD:build/keep.txt'), 'tracked output')
        first_path, = (path for path in self.directory.glob('*/evolution.json')
                       if json.loads(path.read_text()).get('promoted_commit') == first['promoted_commit'])
        marker = first_path.parent / 'monitor/approval-ready.json'
        saved = json.loads(marker.read_text())
        self.assertNotEqual(saved['candidate_tree'], git(self.source, 'rev-parse', first['promoted_commit'] + '^{tree}'))
        saved['round'] += 1
        saved['business_head']['commit'] = first['promoted_commit']
        write_json(marker, saved)
        self.assert_history_deferred(first_path, 'unconsumed publication approval')
