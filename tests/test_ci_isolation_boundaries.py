"""Workflow fixtures may opt into provider mode; production strict fails closed."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console.tasks import TaskStore, TaskError


class CIIsolationBoundaryTests(unittest.TestCase):
    def test_default_strict_task_without_bwrap_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);project=root/'project';project.mkdir()
            store=TaskStore(root/'data',project)
            with patch('console.tasks.discover_providers',return_value={'codex':True}), \
                    patch('auto_moniter.isolation.shutil.which',return_value=None), \
                    patch.object(store,'_launch') as launch:
                with self.assertRaisesRegex(TaskError,'requires bwrap') as error:
                    store.create({'title':'strict','requirement':'Do work','provider':'codex',
                                  'reviewer':'codex','auto_start':True})
                self.assertEqual(error.exception.status,409)
                launch.assert_not_called()
            self.assertEqual(list(store.tasks_dir.glob('*.json')),[])
            self.assertEqual(list(store.runs_dir.iterdir()),[])
