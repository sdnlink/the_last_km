"""User amendments have stable provenance without rewriting old requirements."""
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.directions import Directions
from auto_moniter.supervisor import RunConfig, Supervisor
from console.tasks import TaskStore
from tests.test_supervisor import ScriptedProvider, _verdict


class RequirementRevisionTests(unittest.TestCase):
    def test_legacy_amendments_form_stable_parent_linked_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            messages = [{'id': 'a', 'text': 'Use Rust', 'at': 'earlier'},
                        {'id': 'b', 'text': 'Use PostgreSQL', 'at': 'later'}]
            raw = json.dumps({'version': 1, 'messages': messages})
            (run/'directions.json').write_text(raw)
            directions = Directions(run)
            revision = directions.revision('Original request\r\n')
            self.assertEqual(revision, Directions(run).revision('Original request\n'))
            self.assertEqual(revision['version'], 3)
            self.assertEqual(revision['amendments'][0]['parent_sha256'], revision['original_sha256'])
            self.assertEqual(revision['amendments'][1]['parent_sha256'], revision['amendments'][0]['sha256'])
            self.assertEqual(revision['sha256'], revision['amendments'][-1]['sha256'])
            self.assertEqual((run/'directions.json').read_text(), raw)
            altered = [messages[0], {**messages[1], 'text': 'Use a different database'}]
            self.assertNotEqual(revision['sha256'], directions.revision('Original request', altered)['sha256'])
            self.assertNotEqual(revision['sha256'], directions.revision('Original request', messages[::-1])['sha256'])

    def test_task_detail_exposes_latest_version_even_before_supervisor_consumes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root/'data', root)
            task = store.create({'title': 'Versioned', 'requirement': 'Use SQLite'})
            self.assertEqual(task['requirement_revision']['version'], 1)
            amended = store.add_direction(task['id'], {'text': 'Use PostgreSQL instead'})
            self.assertEqual(amended['requirement_revision']['version'], 2)
            self.assertEqual(amended['requirement'], 'Use SQLite')
            restored = TaskStore(root/'data', root).detail(task['id'])
            self.assertEqual(restored['requirement_revision'], amended['requirement_revision'])

    def test_invocation_records_exact_version_despite_later_amendment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run = root/'run'
            worker = ScriptedProvider('worker', [
                {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
                {'verdict': _verdict('done', evidence=['app.py'])}])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': _verdict('done', evidence=['app.py'])},
                {'verdict': _verdict('done', evidence=['app.py'])}])
            original = reviewer.start_command
            def amend(*args, **kwargs):
                if not reviewer.calls:
                    Directions(run).append('Use PostgreSQL instead of SQLite')
                return original(*args, **kwargs)
            from unittest.mock import patch
            with patch.object(reviewer, 'start_command', side_effect=amend):
                result = Supervisor(RunConfig('Use SQLite', root, worker, reviewer=reviewer,
                    run_dir=run, retry_delay_s=0, max_rounds=3), log=lambda _: None).run()
            self.assertEqual(result.status, 'done', result.summary)
            self.assertEqual(len(worker.calls), 2)
            state = json.loads((run/'state.json').read_text())
            self.assertEqual(state['requirement_revision']['version'], 2)
            self.assertEqual(state['last_worker_invocation']['requirement_revision'], state['requirement_revision'])
            self.assertEqual(state['last_invocation']['requirement_revision'], state['requirement_revision'])
            self.assertIn('"version": 1', worker.calls[0]['prompt'])
            self.assertIn('"version": 2', worker.calls[1]['prompt'])
            self.assertIn('Later explicit user amendments supersede', worker.calls[1]['prompt'])


if __name__ == '__main__':
    unittest.main()
