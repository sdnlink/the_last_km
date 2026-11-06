import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from auto_moniter.storage import write_json
from console.metrics import Metrics, process_tree
from console.server import ConsoleServer
from console.tasks import TaskError, TaskStore


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='auto-moniter-console-')
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        self.env.start()
        self.store = TaskStore(self.root / 'data', self.project)

    def tearDown(self):
        for child in list(self.store._children.values()):
            if child.poll() is None:
                # Ask the guardian to stop, preserving its usual child cleanup.
                child.terminate()
                child.wait(timeout=12)
        self.env.stop()
        self.temp.cleanup()

    def create(self, **extra):
        return self.store.create({'title': '测试开发任务', 'requirement': '实现接口并补充测试',
                                  'cwd': str(self.project), 'provider': 'codex', 'reviewer': 'codex', **extra})

    def wait_for(self, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail('task did not reach the expected state')

    def test_persistence_validation_and_cancel(self):
        task = self.create()
        restored = TaskStore(self.root / 'data', self.project)
        self.assertEqual(restored.detail(task['id'])['title'], '测试开发任务')
        self.assertEqual(restored.detail(task['id'])['status'], 'queued')
        cancelled = restored.action(task['id'], 'cancel')
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.assertRaises(TaskError):
            restored.action(task['id'], 'start')
        for invalid in ({'requirement': ''}, {'provider': 'command'}, {'max_agent_calls': -1},
                        {'cwd': str(self.root / 'missing')}, {'checks': ['']}, {'auto_start': 'yes'},
                        {'max_rounds': True}, {'model': 'x\x00y'}):
            with self.subTest(invalid=invalid), self.assertRaises(TaskError):
                self.create(**invalid)

    def test_missing_provider_does_not_leave_a_half_created_task(self):
        with patch('console.tasks.discover_providers', return_value={}):
            with self.assertRaises(TaskError):
                self.create(auto_start=True)
        self.assertEqual(self.store.overview()['tasks'], [])

    def test_discover_existing_run_and_reject_unconfirmed_done(self):
        run = self.project / '.auto-moniter/custom/run'
        run.mkdir(parents=True)
        write_json(run / 'state.json', {'cwd': str(self.project), 'status': 'done', 'rounds': []})
        (run / 'supervisor.lock').touch()
        (run / 'requirement.md').write_text('已有运行任务')
        task = self.store.overview()['tasks'][0]
        self.assertFalse(task['managed'])
        self.assertEqual(task['title'], '已有运行任务')
        self.assertEqual(task['status'], 'interrupted')
        self.assertNotIn('resume', task['actions'])
        write_json(run / 'report.json', {'status': 'done', 'rounds': 0, 'summary': '完成', 'stop_reason': 'done'})
        self.assertEqual(self.store.detail(task['id'])['status'], 'done')

    def test_logs_are_bounded_redacted_and_reject_paths_and_symlinks(self):
        task = self.create()
        run = Path(task['run_dir'])
        run.mkdir()
        (run / '001-worker.log').write_text('x' * 100000 + '\npassword=secret-value')
        (run / 'monitor.log').symlink_to('/etc/passwd')
        logs = self.store.logs(task['id'])
        self.assertNotIn('monitor.log', logs['files'])
        self.assertLessEqual(len(logs['content'].encode()), 65536)
        self.assertNotIn('secret-value', logs['content'])
        with self.assertRaises(TaskError):
            self.store.logs(task['id'], '../../etc/passwd')

    def test_host_and_csrf_checks_and_http_workflow(self):
        with ConsoleServer(('127.0.0.1', 0), self.store) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            port = server.server_address[1]

            def request(method, path, body=None, headers=None):
                connection = HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
                response = connection.getresponse()
                status, payload = response.status, response.read()
                connection.close()
                return status, payload
            try:
                status, page = request('GET', '/')
                self.assertEqual(status, 200)
                self.assertIn('开发控制台'.encode(), page)
                self.assertEqual(request('GET', '/api/config', headers={'Host': 'evil.example'})[0], 403)
                config = json.loads(request('GET', '/api/config')[1])
                headers = {'Content-Type': 'application/json', 'X-Console-Token': config['csrf_token']}
                data = {'title': 'HTTP task <script>', 'requirement': 'build it', 'cwd': str(self.project)}
                self.assertEqual(request('POST', '/api/tasks', data, {'Content-Type': 'application/json'})[0], 403)
                self.assertEqual(request('POST', '/api/tasks', data, {**headers, 'X-Console-Token': 'é'})[0], 403)
                self.assertEqual(request('POST', '/api/tasks', data, {**headers, 'Origin': 'https://evil.example'})[0], 403)
                status, payload = request('POST', '/api/tasks', data, headers)
                self.assertEqual(status, 201)
                ident = json.loads(payload)['id']
                status, payload = request('POST', f'/api/tasks/{ident}/cancel', {}, headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)['status'], 'cancelled')
                self.assertEqual(request('GET', '/api/tasks/missing/logs')[0], 404)
                self.assertEqual(request('GET', '/api/overview')[0], 200)
            finally:
                server.shutdown()
                worker.join(timeout=5)

    def test_real_cli_launch_pause_continue_stop_and_resume(self):
        binaries = self.root / 'bin'
        binaries.mkdir()
        fake = binaries / 'codex'
        # Native provider fixture: consume stdin and emit a valid unfinished verdict.
        # It never invokes a model. Slow turns let us exercise live process controls.
        fake.write_text(f'''#!{sys.executable}
import json, sys, time
from pathlib import Path
sys.stdin.read()
Path('agent-started').write_text('yes')
print('local fixture started', flush=True)
time.sleep(0.5)
print(json.dumps({{'status':'continue','scope':'requirement','summary':'仍在实现','done':[],
                  'gaps':['需要继续工作'],'next_actions':['继续实现'],'blockers':[],'evidence':[]}}), flush=True)
''')
        fake.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']}):
            task = self.create(auto_start=True)
            ident = task['id']
            run = Path(task['run_dir'])
            self.wait_for(lambda: self.store.detail(ident)['status'] == 'running')
            self.wait_for(lambda: (self.project / 'agent-started').exists())
            # A running turn must be visible before its final per-round log exists.
            self.wait_for(lambda: any('local fixture started' in e['text']
                                     for e in self.store.detail(ident)['activity']['events']))
            self.assertFalse((run / '001-worker.log').exists())
            restored = TaskStore(self.root / 'data', self.project)
            self.assertEqual(restored.detail(ident)['status'], 'running')
            with self.assertRaises(TaskError):
                self.create(auto_start=True)
            self.store.action(ident, 'pause')
            self.wait_for(lambda: self.store.detail(ident)['status'] == 'paused')
            paused = self.store.detail(ident)
            self.assertGreater(paused['resources']['process_count'], 0)
            self.assertTrue(paused['git_history']['enabled'])
            self.assertTrue(paused['git_checkpoints'][0]['commit'])
            self.assertEqual(paused['git_checkpoints'][0]['role'], 'worker')
            self.store.action(ident, 'continue')
            self.wait_for(lambda: self.store.detail(ident)['status'] == 'running')
            self.store.action(ident, 'stop')
            self.wait_for(lambda: self.store.detail(ident)['status'] == 'stopped')
            self.wait_for(lambda: not self.store._launcher_alive(self.store._get(ident)))
            self.assertIn('resume', self.store.detail(ident)['actions'])
            self.store.action(ident, 'resume')
            self.wait_for(lambda: self.store.detail(ident)['status'] == 'running')
            self.store.action(ident, 'stop')
            self.wait_for(lambda: self.store.detail(ident)['status'] == 'stopped')
            self.wait_for(lambda: ident not in self.store._children)
            self.assertEqual(json.loads((run / 'report.json').read_text())['status'], 'stopped')
            self.assertFalse((run / 'process.json').exists())
            self.assertIn('console.log', self.store.logs(ident)['files'])


class MetricsTests(unittest.TestCase):
    def test_real_sampling_and_descendant_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = Metrics(Path(tmp)).sample()
            self.assertGreater(sample['memory_total'], 0)
            self.assertGreater(sample['disk_total'], 0)
            self.assertIn(os.getpid(), sample['processes'])
            tree = process_tree(sample['processes'], [os.getpid()])
            self.assertGreater(tree['rss_bytes'], 0)
            self.assertGreaterEqual(tree['process_count'], 1)
        tree = process_tree({10:{'ppid':1,'rss_bytes':100,'cpu_percent':10},
                             11:{'ppid':10,'rss_bytes':200,'cpu_percent':20},
                             12:{'ppid':11,'rss_bytes':300,'cpu_percent':30},
                             13:{'ppid':1,'rss_bytes':400,'cpu_percent':40}}, [10, 11])
        self.assertEqual(tree, {'process_count':3,'rss_bytes':600,'cpu_percent':60})


if __name__ == '__main__':
    unittest.main()
