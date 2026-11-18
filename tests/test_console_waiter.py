"""Faults after startup must not strand a CLI or its console registration."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.processes import process_identity
from console.launch import abort_start
from console.tasks import TaskStore


class ConsoleWaiterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore(self.root/'data', self.root)
        task = self.store.create({'title':'waiter fixture', 'requirement':'fixture input',
            'provider':'codex', 'reviewer':'codex', 'review_isolation':'provider', 'workflow':'legacy'})
        self.ident = task['id']
        self.meta = self.store._get(self.ident)
        self.child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(abort_start, self.child, timeout=.2)
        self.store._children[self.ident] = self.child
        self.meta.update(status='starting', launcher=process_identity(self.child.pid))
        self.store._save(self.meta)

    def test_communicate_failure_reaps_the_cli_and_records_the_original_error(self):
        with patch.object(self.child, 'communicate', side_effect=OSError('fixture pipe write failed')):
            self.store._wait(self.ident, self.child, self.meta['requirement'])
        self.assertIsNotNone(self.child.poll())
        self.assertTrue(self.child.stdin.closed)
        self.assertNotIn(self.ident, self.store._children)
        meta = self.store._get(self.ident)
        self.assertEqual(meta['status'], 'failed')
        self.assertIn('fixture pipe write failed', meta['error'])
        self.assertNotIn('launcher', meta)

    def test_exit_metadata_failure_still_releases_a_reaped_registration(self):
        with patch.object(self.store, '_save', side_effect=OSError('fixture catalog unavailable')):
            with self.assertLogs('console.tasks', level='WARNING') as logs:
                self.store._wait(self.ident, self.child, self.meta['requirement'])
        self.assertEqual(self.child.poll(), 0)
        self.assertNotIn(self.ident, self.store._children)
        self.assertIn('fixture catalog unavailable', '\n'.join(logs.output))

    def test_failed_cleanup_keeps_the_live_process_owned(self):
        with patch.object(self.child, 'communicate', side_effect=OSError('fixture transport failed')), \
             patch('console.tasks.abort_start', side_effect=RuntimeError('fixture cleanup unconfirmed')):
            self.store._wait(self.ident, self.child, self.meta['requirement'])
        self.assertIsNone(self.child.poll())
        self.assertIs(self.store._children[self.ident], self.child)
        meta = self.store._get(self.ident)
        self.assertIn('fixture transport failed', meta['error'])
        self.assertIn('fixture cleanup unconfirmed', meta['error'])
        self.assertEqual(meta['launcher']['pid'], self.child.pid)

    def test_an_old_waiter_cannot_overwrite_a_replacement_launcher(self):
        replacement = object()
        self.store._children[self.ident] = replacement
        saved = (self.store.tasks_dir/(self.ident+'.json')).read_bytes()
        self.store._wait(self.ident, self.child, self.meta['requirement'])
        self.assertIs(self.store._children[self.ident], replacement)
        self.assertEqual((self.store.tasks_dir/(self.ident+'.json')).read_bytes(), saved)
