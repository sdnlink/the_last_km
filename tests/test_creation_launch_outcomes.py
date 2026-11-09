"""Keep genuine launch failures visible while rejecting invalid input before save."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from console.tasks import TaskError, TaskStore


class CreationLaunchOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.environment = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = TaskStore(self.root / 'console', self.project)
        self.payload = {'title': '启动失败仍可查看', 'requirement': '保留真实失败原因',
                        'cwd': str(self.project), 'provider': 'codex', 'reviewer': 'codex',
                        'review_isolation': 'provider', 'auto_start': True}

    def test_os_launch_failure_keeps_failed_registration_and_saved_configuration(self):
        with patch('console.tasks.discover_providers', return_value={'codex': True}), \
                patch('console.tasks.subprocess.Popen', side_effect=OSError('launch fixture unavailable')):
            with self.assertRaisesRegex(TaskError, '启动失败') as raised:
                self.store.create(self.payload)
        self.assertEqual(raised.exception.status, 500)
        rows = self.store.task_summaries()['tasks']
        self.assertEqual(len(rows), 1)
        task = rows[0]
        self.assertEqual(task['status'], 'failed')
        meta = self.store._get(task['id'])
        self.assertIn('launch fixture unavailable', meta['error'])
        self.assertEqual(meta['requirement'], self.payload['requirement'])
        self.assertTrue((Path(meta['run_dir']) / 'launch.json').is_file())
        self.assertFalse(self.store._children)

    def test_real_cli_preflight_failure_is_visible_and_never_calls_a_model(self):
        binaries = self.root / 'bin'
        binaries.mkdir()
        marker = self.project / 'model-was-called'
        binary = binaries / 'codex'
        binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                          f'Path({str(marker)!r}).write_text("unexpected model call")\n')
        binary.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']}):
            task = self.store.create(self.payload)
            ident = task['id']
            deadline = time.monotonic() + 8
            try:
                while ident in self.store._children and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertNotIn(ident, self.store._children, 'CLI did not exit after its preflight failure')
                view = self.store.detail(ident)
                self.assertEqual(view['status'], 'failed')
                self.assertNotEqual(self.store._get(ident)['exit_code'], 0)
                self.assertTrue(view['failure']['reason'])
                self.assertIn('git', view['failure']['raw_summary'].lower())
                self.assertFalse(marker.exists())
                self.assertEqual(view['requirement'], self.payload['requirement'])
                self.assertTrue((Path(view['run_dir']) / 'console.log').is_file())
            finally:
                child = self.store._children.get(ident)
                if child is not None:
                    child.terminate()
                    child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
