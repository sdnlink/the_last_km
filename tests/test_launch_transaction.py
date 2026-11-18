"""A failed console startup must not leave its CLI waiting or running unowned."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console.launch import abort_start
from console.tasks import TaskStore, TaskError


class LaunchTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore(self.root/'data', self.root)
        self.task = self.store.create({'title':'Startup transaction', 'requirement':'Fixture input',
            'provider':'codex', 'reviewer':'codex', 'review_isolation':'provider', 'workflow':'legacy'})
        self.meta = self.store._get(self.task['id'])
        self.children = []
        popen = subprocess.Popen
        def spawn(argv, **options):
            child = popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], **options)
            self.children.append(child)
            self.addCleanup(abort_start, child, timeout=.2)
            return child
        for mocked in (patch.object(self.store, '_new_args', return_value=['run']),
                       patch.object(self.store, '_resume_args', return_value=['run']),
                       patch('console.tasks.subprocess.Popen', side_effect=spawn)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def assert_reaped(self):
        self.assertEqual(len(self.children), 1)
        child = self.children[0]
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)
        self.assertNotIn(self.task['id'], self.store._children)

    def test_metadata_write_failure_reaps_child_and_keeps_a_failed_registration(self):
        save = self.store._save
        attempts = []
        def fail_once(meta):
            attempts.append(meta['status'])
            if len(attempts) == 1:
                raise OSError('catalog write failed after Popen')
            save(meta)
        with patch.object(self.store, '_save', side_effect=fail_once):
            with self.assertRaisesRegex(TaskError, 'catalog write failed after Popen'):
                self.store._launch(self.meta)
        self.assert_reaped()
        saved = self.store._get(self.task['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertNotIn('launcher', saved)

    def test_persistent_metadata_failure_still_reaps_child_and_reports_both_errors(self):
        with patch.object(self.store, '_save', side_effect=OSError('disk remains full')):
            with self.assertRaisesRegex(TaskError, '失败记录未能保存') as error:
                self.store._launch(self.meta)
        self.assertEqual(error.exception.status, 500)
        self.assertIn('disk remains full', str(error.exception))
        self.assert_reaped()

    def test_thread_start_failure_rolls_back_the_spawn(self):
        with patch('console.tasks.threading.Thread.start', side_effect=RuntimeError('cannot start waiter')):
            with self.assertRaisesRegex(TaskError, 'cannot start waiter'):
                self.store._launch(self.meta)
        self.assert_reaped()

    def test_process_identity_failure_reaps_child_before_it_is_registered(self):
        with patch('console.tasks.process_identity', side_effect=OSError('identity unavailable')):
            with self.assertRaisesRegex(TaskError, 'identity unavailable'):
                self.store._launch(self.meta)
        self.assert_reaped()

    def test_failed_resume_restores_the_original_control_request(self):
        run = Path(self.meta['run_dir'])
        run.mkdir(parents=True)
        control = {'action':'stop', 'reason':'original request'}
        (run/'control.json').write_text(json.dumps(control))
        with patch('console.tasks.threading.Thread.start', side_effect=RuntimeError('waiter unavailable')):
            with self.assertRaises(TaskError):
                self.store._launch(self.meta, resume=True)
        self.assert_reaped()
        self.assertEqual(json.loads((run/'control.json').read_text()), control)

    def test_cleanup_failure_preserves_ownership_and_reports_the_failure(self):
        with patch('console.tasks.threading.Thread.start', side_effect=RuntimeError('waiter failed')), \
             patch('console.tasks.abort_start', side_effect=RuntimeError('termination unconfirmed')):
            with self.assertRaisesRegex(TaskError, '进程回收失败'):
                self.store._launch(self.meta)
        self.assertIs(self.store._children[self.task['id']], self.children[0])
        self.assertEqual(self.store._get(self.task['id'])['launcher']['pid'], self.children[0].pid)

    def test_identity_and_cleanup_failure_still_block_a_duplicate_launch(self):
        with patch('console.tasks.process_identity', side_effect=OSError('identity unavailable')), \
             patch('console.tasks.abort_start', side_effect=RuntimeError('termination unconfirmed')):
            with self.assertRaisesRegex(TaskError, '进程回收失败'):
                self.store._launch(self.meta)
            saved = self.store._get(self.task['id'])
            self.assertIs(self.store._children.get(self.task['id']), self.children[0])
            self.assertTrue(self.store._launcher_alive(saved))
            self.assertEqual(self.store.detail(self.task['id'])['status'], 'starting')
            with self.assertRaisesRegex(TaskError, '仍在'):
                self.store._check_start(saved)

    def test_abort_escalates_if_the_child_ignores_sigterm_and_closes_all_pipes(self):
        # Use the real constructor; the fixture's launch replacement is scoped
        # to the console path and would otherwise replace this invocation too.
        with patch('console.tasks.subprocess.Popen', new=self._real_popen):
            child = subprocess.Popen([sys.executable,'-u','-c',
                "import signal,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready'); sys.stdin.read()"],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.addCleanup(abort_start,child,timeout=.2)
        self.assertEqual(child.stdout.readline(),b'ready\n')
        abort_start(child,timeout=.05)
        self.assertEqual(child.returncode,-signal.SIGKILL)
        self.assertTrue(all(getattr(child,key).closed for key in ('stdin','stdout','stderr')))

    _real_popen = staticmethod(subprocess.Popen)
