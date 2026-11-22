"""Optional referee settings survive CLI, console creation and checkpoint recovery."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import build_parser
from auto_moniter.health import run_health
from auto_moniter.storage import write_json
from console.recovery import resume_arguments
from console.tasks import TaskStore, TaskError
from console.workflow import work_details
from tests import test_console_recovery as recovery


class RefereeConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'private')})
        env.start()
        self.addCleanup(env.stop)

    def test_console_creation_is_optional_and_independent_model_reaches_cli(self):
        store = TaskStore(self.root / 'data', self.project)
        for options in ({}, {'referee': True, 'referee_model': 'independent-model'}):
            created = store.create({'title': 'Referee fixture', 'requirement': 'Implement behavior',
                'provider': 'codex', 'reviewer': 'codex', 'workflow': 'legacy', **options})
            meta = store._get(created['id'])
            args = build_parser().parse_args(store._new_args(meta))
            self.assertEqual(args.referee, bool(options))
            self.assertEqual(args.referee_model, options.get('referee_model', ''))
            self.assertEqual(created['referee']['enabled'], bool(options))
        with self.assertRaises(TaskError):
            store.create({'title': 'bad', 'requirement': 'task', 'provider': 'codex',
                          'reviewer': 'codex', 'referee': 'true'})

    def test_saved_referee_configuration_reaches_normalized_resume_cli(self):
        fixture = recovery.ResumeArgumentsTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        state = {**fixture.state, 'referee': {
            'enabled': True, 'model': 'independent-model', 'decision': None, 'replan': False}}
        argv = resume_arguments({'argv': fixture.argv}, fixture.root / 'run', state)
        parsed = build_parser().parse_args(argv)
        self.assertTrue(parsed.referee)
        self.assertEqual(parsed.referee_model, 'independent-model')
        argv = resume_arguments({'argv': [*fixture.argv, '--no-referee']}, fixture.root / 'run', state)
        self.assertFalse(build_parser().parse_args(argv).referee)

    def test_health_and_console_show_referee_as_a_separate_optional_role(self):
        run = self.root / 'run'
        run.mkdir()
        write_json(run / 'state.json', {'status': 'running', 'referee': {'enabled': True, 'model': 'judge'}})
        (run / 'supervisor.lock').touch()
        from datetime import datetime, timezone
        write_json(run / 'heartbeat', {'at': datetime.now(timezone.utc).isoformat(),
                                      'note': 'round 920001: referee running'})
        health = run_health(run)
        self.assertEqual(health['phase'], 'monitor_exited')
        from auto_moniter.workspace import file_lock
        with file_lock(run / 'supervisor.lock'):
            health = run_health(run)
            self.assertEqual(health['phase'], 'refereeing')
            state = json.loads((run / 'state.json').read_text())
            work = work_details(state, health, {}, 'running', {}, {}, 'codex', 'codex')
            self.assertEqual(work['roles']['referee']['status'], 'running')
            self.assertEqual(work['roles']['worker']['status'], 'waiting')
            self.assertEqual(work['roles']['reviewer']['status'], 'waiting')
            state['referee']['enabled'] = False
            work = work_details(state, health, {}, 'running', {}, {}, 'codex', 'codex')
            self.assertNotIn('referee', work['roles'])
