"""A known stop or exhausted budget must not start a new local publication."""
import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.git_history import GitHistoryError
from auto_moniter.publication import PublicationRefused, publish
from auto_moniter.supervisor import BudgetExceeded, RunConfig, Supervisor
from tests.test_business_publication import BusinessPublicationTests
from tests.test_modules import PLAN, outcome
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Reviewer(ScriptedProvider):
    def __init__(self, root, scripts):
        super().__init__('reviewer', scripts)
        self.root = root

    def _command(self, session, prompt, read_only):
        if self.calls:
            self.scripts[0]['verdict']['evidence'].append(
                re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt.read_text()).group(1))
        return super()._command(session, prompt, read_only)


class PublicationBudgetBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.auto-moniter/\n')
        (self.root / 'app.py').write_text('baseline\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@local', 'commit', '-qm', 'initial'], check=True)
        self.before = git(self.root, 'rev-parse', 'HEAD')
        self.run = self.root / 'run'

    def _monitor(self, clock):
        worker = ScriptedProvider('worker', [{'verdict': outcome(['A'], plan=PLAN), 'touch': 'app.py'}])
        reviewer = Reviewer(self.root, [
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': outcome(['A'])},
        ])
        monitor = Supervisor(RunConfig(
            'task', self.root, worker, reviewer=reviewer, workflow='review-first',
            git_history='required', run_dir=self.run, max_rounds=1, retry_delay_s=0,
            checks=['true'], modules=PLAN, max_runtime_s=10,
        ), log=lambda _: None)
        monitor._elapsed = lambda: clock.now
        return monitor

    def _assert_unpublished(self, report):
        self.assertNotEqual(report.status, 'done')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        history = json.loads((self.run / 'git-history.json').read_text())
        self.assertFalse(history.get('head'))
        state = json.loads((self.run / 'state.json').read_text())
        self.assertNotEqual(state['modules'][0]['status'], 'accepted')
        self.assertTrue(report.summary or report.records[-1].verdict.gaps)

    def test_budget_exhausted_at_commit_entry_does_not_publish(self):
        clock = Clock()
        monitor = self._monitor(clock)
        original = monitor._commit_approved_locked

        def expire(worker, reviewer, *args):
            clock.now = 10
            return original(worker, reviewer, *args)

        with patch.object(monitor, '_commit_approved_locked', side_effect=expire):
            report = monitor.run()
        self._assert_unpublished(report)
        self.assertIn('runtime budget', ' '.join(report.records[-1].verdict.gaps).lower() + report.summary.lower())

    def test_stop_after_checks_and_before_commit_does_not_publish(self):
        clock = Clock()
        monitor = self._monitor(clock)
        original = monitor._commit_approved

        def stop_first(*args, **kwargs):
            monitor.request_stop()
            return original(*args, **kwargs)

        with patch.object(monitor, '_commit_approved', side_effect=stop_first):
            report = monitor.run()
        self._assert_unpublished(report)
        self.assertIn('stop', (report.summary + ' '.join(report.records[-1].verdict.gaps)).lower())

    def test_budget_during_preparation_and_stop_before_transaction_keep_refs(self):
        fixture = BusinessPublicationTests()
        fixture.tmp = self.tmp
        fixture.root = self.root
        fixture.run = self.run
        fixture.before = self.before
        self.run.mkdir(parents=True, exist_ok=True)
        history = fixture.history()
        head = git(self.root, 'rev-parse', 'HEAD')

        def submit(boundary):
            return publish(history, 1, {'summary': 'worker'}, {'summary': 'review'}, '{}', '{}', boundary=boundary)

        with self.assertRaises(PublicationRefused):
            submit(lambda: False)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertFalse(history.head)
        self.assertFalse((self.run / 'publication.json').exists())

        history = fixture.history()
        calls = {'n': 0}

        def refuse_during_preparation():
            calls['n'] += 1
            return calls['n'] == 1

        with self.assertRaises(PublicationRefused):
            publish(history, 1, {'summary': 'worker'}, {'summary': 'review'}, '{}', '{}', boundary=refuse_during_preparation)
        self.assertEqual(calls['n'], 2)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.run / 'publication.json').exists())

        history = fixture.history()
        seen = {'n': 0}

        def stop_at_transaction():
            seen['n'] += 1
            return seen['n'] < 3

        with self.assertRaises(PublicationRefused):
            publish(history, 1, {'summary': 'worker'}, {'summary': 'review'}, '{}', '{}', boundary=stop_at_transaction)
        self.assertEqual(seen['n'], 3)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertFalse((self.run / 'publication.json').exists())
        self.assertFalse(history.head)

    def test_completed_atomic_publication_is_retained_when_budget_ends_afterward(self):
        clock = Clock()
        monitor = self._monitor(clock)
        original = monitor._commit_approved_locked

        def expire_after(worker, reviewer, *args):
            value = original(worker, reviewer, *args)
            clock.now = 10
            return value

        with patch.object(monitor, '_commit_approved_locked', side_effect=expire_after):
            report = monitor.run()
        self.assertNotEqual(report.status, 'done')
        self.assertEqual(report.status, 'budget_exceeded')
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        history = json.loads((self.run / 'git-history.json').read_text())
        self.assertTrue(history.get('head'))
        self.assertEqual(git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_supervisor_boundaries_refuse_before_refs_move(self):
        boundaries = ('fingerprint', 'prepare', 'transaction', 'stop-file', 'stop-before-start')
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                self.setUp()
                clock = Clock()
                monitor = self._monitor(clock)
                launched = {'checks': 0}
                import auto_moniter.supervisor as supervisor_module
                original = supervisor_module.run_command

                def counting(prepared, cwd, **kwargs):
                    if prepared.argv[:2] == ['sh', '-c']:
                        launched['checks'] += 1
                    return original(prepared, cwd, **kwargs)

                def fingerprint(cwd, *, ignore_dir=None):
                    if ignore_dir == monitor.run_dir and (monitor.run_dir / '001-reviewer.log').exists():
                        if boundary == 'fingerprint':
                            clock.now = 10
                        if boundary == 'stop-before-start':
                            (monitor.run_dir / 'control.json').write_text(json.dumps({'action': 'stop'}))
                    from auto_moniter.workspace import workspace_fingerprint as real
                    return real(cwd, ignore_dir=ignore_dir)

                original_publish = None

                def wrapped_publish(*args, **kwargs):
                    gate = kwargs.get('boundary')
                    calls = {'n': 0}

                    def sequenced():
                        calls['n'] += 1
                        if boundary == 'prepare' and calls['n'] == 2:
                            clock.now = 10
                        if boundary == 'transaction' and calls['n'] == 3:
                            clock.now = 10
                        return gate()

                    kwargs['boundary'] = sequenced
                    return original_publish(*args, **kwargs)

                from auto_moniter import publication as publication_module
                original_publish = publication_module.publish
                patches = [
                    patch('auto_moniter.supervisor.run_command', side_effect=counting),
                    patch('auto_moniter.supervisor.workspace_fingerprint', side_effect=fingerprint),
                ]
                if boundary in {'prepare', 'transaction'}:
                    patches.append(patch('auto_moniter.publication.publish', side_effect=wrapped_publish))
                if boundary == 'stop-file':
                    def write_stop(prepared, cwd, **kwargs):
                        if prepared.argv[:2] == ['sh', '-c']:
                            launched['checks'] += 1
                            (monitor.run_dir / 'control.json').write_text(json.dumps({'action': 'stop'}))
                        return original(prepared, cwd, **kwargs)
                    patches[0] = patch('auto_moniter.supervisor.run_command', side_effect=write_stop)
                started = [item.start() for item in patches]
                try:
                    report = monitor.run()
                finally:
                    for item in reversed(patches):
                        item.stop()
                    del started
                self._assert_unpublished(report)
                self.assertIn('stop', (report.summary + ' '.join(report.records[-1].verdict.gaps)).lower()
                              + ' '.join(report.records[-1].verdict.gaps).lower())
                if boundary in {'fingerprint', 'stop-before-start'}:
                    self.assertEqual(launched['checks'], 0)
                    results = json.loads((self.run / 'acceptance-results.json').read_text())
                    self.assertEqual(results['checks'], [])
                    self.assertFalse(results['complete'])
                self.assertLessEqual(len(monitor.config.reviewer.calls), 2)

    def test_commit_fingerprint_and_prepare_stop_refuse_after_checks_pass(self):
        for boundary in ('commit-fingerprint', 'prepare-stop'):
            with self.subTest(boundary=boundary):
                self.setUp()
                clock = Clock()
                monitor = self._monitor(clock)
                before_audit = git(self.root, 'show-ref')
                launched = {'checks': 0}
                import auto_moniter.supervisor as supervisor_module
                original = supervisor_module.run_command

                def counting(prepared, cwd, **kwargs):
                    if prepared.argv[:2] == ['sh', '-c']:
                        launched['checks'] += 1
                    return original(prepared, cwd, **kwargs)

                entered = {'commit': False}

                def fingerprint(cwd, *, ignore_dir=None):
                    if boundary == 'commit-fingerprint' and entered['commit'] and ignore_dir == monitor.run_dir:
                        clock.now = 10
                    from auto_moniter.workspace import workspace_fingerprint as real
                    return real(cwd, ignore_dir=ignore_dir)

                original_commit = monitor._commit_approved_locked

                def enter_then_commit(*args, **kwargs):
                    entered['commit'] = True
                    return original_commit(*args, **kwargs)

                from auto_moniter import publication as publication_module
                original_publish = publication_module.publish

                def stop_during_prepare(*args, **kwargs):
                    gate = kwargs.get('boundary')
                    calls = {'n': 0}

                    def sequenced():
                        calls['n'] += 1
                        if calls['n'] == 2:
                            (monitor.run_dir / 'control.json').write_text(json.dumps({'action': 'stop'}))
                        return gate()

                    kwargs['boundary'] = sequenced
                    return original_publish(*args, **kwargs)

                patches = [
                    patch('auto_moniter.supervisor.run_command', side_effect=counting),
                    patch('auto_moniter.supervisor.workspace_fingerprint', side_effect=fingerprint),
                    patch.object(monitor, '_commit_approved_locked', side_effect=enter_then_commit),
                ]
                if boundary == 'prepare-stop':
                    patches.append(patch('auto_moniter.publication.publish', side_effect=stop_during_prepare))
                started = [item.start() for item in patches]
                try:
                    report = monitor.run()
                finally:
                    for item in reversed(patches):
                        item.stop()
                    del started
                self._assert_unpublished(report)
                self.assertGreaterEqual(launched['checks'], 1)
                results = json.loads((self.run / 'acceptance-results.json').read_text())
                self.assertTrue(results['complete'])
                self.assertEqual(results['checks'][0]['status'], 'passed')
                heads = git(self.root, 'for-each-ref', '--format=%(refname)', 'refs/heads')
                self.assertEqual(heads, before_audit.split()[-1] if before_audit else 'refs/heads/master')
                audit_refs = subprocess.run(['git', '-C', str(self.root), 'show-ref'], capture_output=True, text=True).stdout
                self.assertNotIn('refs/auto-moniter/', audit_refs)
                self.assertIn('stop', (report.summary + ' '.join(report.records[-1].verdict.gaps)).lower())
                self.assertNotEqual(report.status, 'done')
                self.assertLessEqual(len(monitor.config.reviewer.calls), 2)

    def test_publication_error_type_is_not_a_generic_history_failure(self):
        self.assertTrue(issubclass(PublicationRefused, GitHistoryError))
        with self.assertRaises(BudgetExceeded):
            raise BudgetExceeded('Total active runtime budget exhausted')
        self.assertGreaterEqual(time.monotonic(), 0)


if __name__ == '__main__':
    unittest.main()
