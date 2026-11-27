import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from auto_moniter.history import prune_files
from auto_moniter.run_storage import clean_run_cache, maintain_run_storage, open_log
from console.logs import LogReader
from console.tasks import TaskStore


class RunStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = Path(directory.name)

    def test_archive_frees_quota_and_preserves_both_log_views(self):
        log = self.run / '001-reviewer.log'
        content = b'test output\n' * 2100
        log.write_bytes(content)
        reader = LogReader()
        self.assertEqual(reader.read(log, 0, 10)['total_lines'], 2100)
        check = self.run / '001-check-01.log'
        check.write_bytes(b'evidence' * 1000)
        active = self.run / '009-worker-prompt.md'
        active.write_bytes(b'active' * 1000)
        monitor = SimpleNamespace(run_dir=self.run, _next_round=9, _last_space_check=0,
                                  config=SimpleNamespace(max_run_bytes=22000))
        maintain_run_storage(monitor)
        self.assertFalse(monitor._space_exceeded)
        self.assertFalse(log.exists())
        self.assertTrue(active.exists())
        self.assertEqual(check.read_bytes(), b'evidence' * 1000)
        with open_log(log) as (stream, _, archived):
            self.assertTrue(archived)
            self.assertEqual(stream.read(), content)
        for _ in range(2):
            page = reader.read(log, 0, 10)
            self.assertEqual((page['start_line'], page['total_lines']), (2091, 2100))
            self.assertEqual(page['content'], 'test output\n' * 10)
        store = TaskStore(self.run / 'console', self.run)
        store._get = lambda _: {'run_dir': str(self.run)}
        self.assertEqual(store.log_text('task', log.name), content.decode())
        self.assertIn(log.name, store.logs('task')['files'])
        self.assertNotIn(log.name + '.gz', store.logs('task')['files'])
        self.assertEqual(prune_files(self.run, 2, protected_rounds=[1]), 0)
        self.assertEqual(prune_files(self.run, 2), 2)

    def test_cache_cleanup_requires_old_owned_temp_and_saved_original(self):
        now = time.time()
        names = ['.state.json-abcdefgh', '.report.json-abcdefgh', '.state.json-abcdefgi',
                 '.log-archive-abcdefgh', '.unrelated.json-abcdefgh', 'baseline.json.gz.tmp']
        for name in names:
            path = self.run / name
            path.write_text('temporary')
            if name != '.state.json-abcdefgi':
                os.utime(path, (now - 7200, now - 7200))
        (self.run / 'state.json').write_text('{}')
        (self.run / '.state.json-symlink0').symlink_to(self.run / 'state.json')
        clean_run_cache(self.run, now=now)
        self.assertEqual(
            {path.name for path in self.run.iterdir()},
            set(names) - {names[0], names[3]} | {'state.json', '.state.json-symlink0'},
        )
