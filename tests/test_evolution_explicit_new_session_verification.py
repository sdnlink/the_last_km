"""Exercise explicit evolution history, lock, and shared budget boundaries."""

from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution, git
from auto_moniter.locking import workspace_lease
from auto_moniter.storage import write_json
from auto_moniter.workspace import file_lock
from tests import test_evolution_explicit_new_session as fixtures


class ExplicitNewSessionVerificationTests(unittest.TestCase):
    def setUp(self):
        self.session = fixtures.ExplicitNewSessionTests()
        self.addCleanup(self.session.doCleanups)
        self.session.setUp()
        self.source = self.session.source
        self.source_lock = self.source / '.auto-moniter/workspace.lock'
        self.cycle_lock = self.session.directory / 'evolution.lock'

    def saved_evolution(self):
        # Execution can open lock files. Compare the cycle directories and all
        # their evidence, as well as the source refs and registered worktrees.
        history = {}
        for directory in self.session.directory.iterdir():
            if directory == self.cycle_lock:
                continue
            for path in (directory, *directory.rglob('*')):
                if path.is_symlink():
                    content = ('symlink', path.readlink())
                elif path.is_file():
                    content = ('file', path.read_bytes())
                else:
                    content = ('directory', None)
                history[path] = content
        return history, git(self.source, 'show-ref'), git(self.source, 'worktree', 'list', '--porcelain')

    def assert_no_calls(self):
        self.assertFalse(self.session.fixture.worker.calls or self.session.fixture.reviewer.calls)

    def assert_deferred(self, reason, *, plan_reason=None):
        before = self.saved_evolution()
        for options in (('--plan-only',), ()):
            with self.subTest(options=options):
                code, result = self.session.invoke('--new-session', *options)
                self.assertEqual((code, result['agent_calls']), (1, 0), result)
                cycle, = result['cycles']
                self.assertEqual(cycle['phase'], 'deferred', result)
                self.assertIn((plan_reason or reason) if options else reason, cycle['summary'])
                self.assertEqual(self.saved_evolution(), before)
                self.assert_no_calls()

    def test_ordinary_rejection_and_seen_proactive_review_leave_no_new_candidate(self):
        rejected = self.session.legacy('rejected')
        before = self.session.snapshot()

        code, result = self.session.invoke('--new-session', '--plan-only')

        self.assertEqual((code, result['agent_calls']), (0, 0), result)
        plan, = result['cycles']
        self.assertEqual((plan['phase'], plan['issue']['kind']), ('planned', 'proactive_review'))
        self.assertEqual([row['directory'] for row in plan['retained_legacy_history']],
                         [str(rejected.parent)])
        self.assertEqual(self.session.snapshot(), before)

        proactive = self.session.legacy('rejected')
        record = json.loads(proactive.read_text())
        record['issue'] = plan['issue']
        write_json(proactive, record)
        before = self.saved_evolution()
        for options in (('--plan-only',), ()):
            with self.subTest(options=options):
                code, result = self.session.invoke('--new-session', *options)
                self.assertEqual((code, result['agent_calls']), (0, 0), result)
                idle, = result['cycles']
                self.assertEqual((idle['phase'], idle['action'], idle['base'], idle['issue']),
                                 ('idle', 'new_session', self.session.base, None))
                self.assertEqual({(row['directory'], row['phase']) for row in idle['retained_legacy_history']},
                                 {(str(path.parent), 'rejected') for path in (rejected, proactive)})
                self.assertEqual(self.saved_evolution(), before)
                self.assert_no_calls()

    def test_terminal_status_or_budget_contradiction_blocks_admission(self):
        path = self.session.legacy('rejected', 'budget_exceeded')
        report_path = path.parent / 'monitor/report.json'
        report = json.loads(report_path.read_text())
        write_json(report_path, {**report, 'status': 'failed'})
        with self.subTest(contradiction='terminal status'):
            self.assert_deferred('Supervisor checkpoint and terminal report disagree')

        state_path = path.parent / 'monitor/state.json'
        state = json.loads(state_path.read_text())
        for evidence_path, evidence in ((report_path, report), (state_path, state)):
            write_json(evidence_path, {**evidence, 'usage': {**evidence['usage'], 'max_agent_calls': 8}})
        with self.subTest(contradiction='budget never exhausted'):
            self.assert_deferred('budget exhaustion is unverified')

    def test_malformed_history_or_child_json_blocks_admission(self):
        path = self.session.legacy('rejected')
        for evidence in (path, path.parent / 'monitor/state.json'):
            with self.subTest(evidence=evidence.name):
                original = evidence.read_bytes()
                evidence.write_bytes(b'{"truncated":')
                self.assert_deferred('Invalid checkpoint JSON: ' + evidence.name)
                evidence.write_bytes(original)

    def test_missing_child_state_blocks_admission(self):
        path = self.session.legacy('rejected', 'budget_exceeded')
        (path.parent / 'monitor/state.json').unlink()
        self.assert_deferred('Cannot read checkpoint evidence: state.json')

    def test_foreign_source_or_monitor_path_blocks_admission(self):
        path = self.session.legacy('rejected')
        original = json.loads(path.read_text())
        foreign = self.session.fixture.root / 'foreign'
        foreign.mkdir()
        marker = foreign / 'sentinel'
        marker.write_bytes(b'foreign evidence')
        for field, reason in (('source', 'belongs to another source project'),
                              ('monitor_run', 'workspace boundary mismatch: monitor_run')):
            with self.subTest(field=field):
                write_json(path, {**original, field: str(foreign)})
                self.assert_deferred(reason)
                self.assertEqual(set(foreign.iterdir()), {marker})
                self.assertEqual(marker.read_bytes(), b'foreign evidence')

    def test_symlinked_checkpoint_or_supervisor_report_blocks_admission(self):
        path = self.session.legacy('rejected')
        for evidence in (path, path.parent / 'monitor/report.json'):
            with self.subTest(evidence=evidence.name):
                target = self.session.fixture.root / evidence.name
                original = evidence.read_bytes()
                evidence.rename(target)
                evidence.symlink_to(target)
                self.assert_deferred('Symbolic links are forbidden: checkpoint' if evidence == path
                                     else 'Cannot read checkpoint evidence: report.json')
                self.assertEqual(target.read_bytes(), original)
                evidence.unlink()
                target.rename(evidence)

    def test_pending_publication_blocks_admission(self):
        path = self.session.legacy('rejected')
        write_json(path.parent / 'monitor/publication.json',
                   {'phase': 'publishing', 'base': self.session.base})
        self.assert_deferred('pending process or Git transaction')

    def test_real_lock_contention_blocks_plan_and_execution(self):
        path = self.session.legacy('rejected')
        with file_lock(self.cycle_lock):
            self.assert_deferred('evolution.lock', plan_reason='Evolution cycle lock')
        for lock, reason in ((self.source_lock, 'workspace'),
                             (path.parent / 'monitor/supervisor.lock', 'Evolution monitor is active')):
            with self.subTest(lock=lock), file_lock(lock):
                self.assert_deferred(reason)

        with workspace_lease(self.source):
            self.assert_deferred('owns an overlapping workspace')
        code, result = self.session.invoke('--new-session', '--plan-only')
        self.assertEqual((code, result['cycles'][0]['phase']), (0, 'planned'), result)
        self.assert_no_calls()

    def test_source_head_change_at_lock_blocks_stale_admission(self):
        self.session.legacy('rejected')
        code, result = self.session.invoke('--new-session', '--plan-only')
        self.assertEqual((code, result['cycles'][0]['phase']), (0, 'planned'), result)
        after_change = None

        @contextmanager
        def change_source_at_lock(path):
            nonlocal after_change
            with file_lock(path):
                if path == self.source_lock:
                    git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
                        'commit', '--allow-empty', '-qm', 'advance source while taking the lock')
                    after_change = self.saved_evolution()
                yield

        with patch('auto_moniter.evolution.file_lock', side_effect=change_source_at_lock):
            code, result = self.session.invoke('--new-session')

        self.assertEqual((code, result['agent_calls']), (1, 0), result)
        cycle, = result['cycles']
        self.assertEqual((cycle['phase'], cycle['base']), ('deferred', self.session.base))
        self.assertIn('Source is busy or changed', cycle['summary'])
        self.assertIsNotNone(after_change)
        self.assertEqual(self.saved_evolution(), after_change)
        self.assertNotEqual(git(self.source, 'rev-parse', 'HEAD'), self.session.base['commit'])
        self.assert_no_calls()
        with workspace_lease(self.source), file_lock(self.cycle_lock), file_lock(self.source_lock):
            self.assertEqual(self.saved_evolution(), after_change)

    def test_stop_arriving_at_source_lock_blocks_dispatch(self):
        path = self.session.legacy('rejected')
        control = path.parent / 'monitor/control.json'
        after_stop = None

        @contextmanager
        def request_stop(path):
            nonlocal after_stop
            with file_lock(path):
                if path == self.source_lock:
                    write_json(control, {'action': 'stop'})
                    after_stop = self.saved_evolution()
                yield

        with patch('auto_moniter.evolution.file_lock', side_effect=request_stop):
            code, result = self.session.invoke('--new-session')

        self.assertEqual((code, result['agent_calls']), (1, 0), result)
        cycle, = result['cycles']
        self.assertEqual(cycle['phase'], 'stopped')
        self.assertIn('Explicit stop', cycle['summary'])
        self.assertIsNotNone(after_stop)
        self.assertEqual(self.saved_evolution(), after_stop)
        self.assert_no_calls()

    def test_second_cycle_rechecks_remaining_shared_time_at_source_lock(self):
        self.session.legacy('rejected', 'budget_exceeded')
        now = 1000
        clock = SimpleNamespace(monotonic=lambda: now)
        run = Evolution.run
        first_delivery = None
        second_grant = None

        def advance_between_cycles(engine, **options):
            nonlocal now, first_delivery, second_grant
            if first_delivery is not None:
                second_grant = (engine.config.max_runtime_s, engine.config.max_agent_calls)
                return run(engine, **options)
            result = run(engine, **options)
            self.assertEqual(result['phase'], 'promoted', result)
            first_delivery = self.saved_evolution()
            now = 1060
            return result

        @contextmanager
        def expire_at_lock(path):
            nonlocal now
            with file_lock(path):
                if path == self.source_lock and second_grant is not None:
                    now = 1091
                yield

        with patch('auto_moniter.cli.time', clock), patch('auto_moniter.evolution.time', clock), \
             patch.object(Evolution, 'run', advance_between_cycles), \
             patch('auto_moniter.evolution.file_lock', side_effect=expire_at_lock):
            code, result = self.session.invoke('--new-session', '--cycles', '2', calls='7')

        self.assertEqual((code, result['agent_calls'], result['elapsed_s']), (1, 3, 91), result)
        first, second = result['cycles']
        self.assertEqual((first['phase'], second['phase']), ('promoted', 'stopped'))
        self.assertEqual(second['base']['commit'], first['promoted_commit'])
        self.assertEqual(second_grant, (30, 4))
        self.assertIn('shared budget exhausted', second['summary'])
        fixture = self.session.fixture
        self.assertEqual((len(fixture.worker.calls), len(fixture.reviewer.calls)), (1, 2))
        self.assertEqual(self.saved_evolution(), first_delivery)
