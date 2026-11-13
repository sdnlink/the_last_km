"""Exercise real console restarts in isolated catalogs without model access."""
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auto_moniter.processes import process_identity
from console.restart import lock_owner


ROOT = Path(__file__).resolve().parent.parent


class ConsoleRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='console restart ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        self.data = self.root / 'console data'
        self.data.mkdir()
        self.environment = {**os.environ, 'AUTO_MONITER_STATE_DIR': str(self.root / 'state'),
                            'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT)}
        self.children = []
        self.identities = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for identity in self.identities:
            pid = identity['pid']
            if process_identity(pid) == identity:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            if child.stderr:
                child.stderr.close()
        deadline = time.monotonic() + 5
        while (self.data / 'console-process.json').exists() and time.monotonic() < deadline:
            time.sleep(.05)

    def restart(self, *extra, cwd=True):
        arguments = [str(ROOT / 'restart.sh'), '--data-dir', str(self.data)]
        if cwd:
            arguments.extend(['--cwd', str(self.project)])
        result = subprocess.run([*arguments, *extra], cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        process_file = self.data / 'console-process.json'
        if process_file.exists():
            saved = json.loads(process_file.read_text())
            if isinstance(saved, dict) and saved.get('identity', {}).get('pid'):
                self.identities.append(saved['identity'])
        return result

    def config(self, port):
        connection = HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request('GET', '/api/config')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    def holder(self, module=False, child=False):
        # The legacy fixture has the exact pre-metadata CLI and catalog flock.
        source = ("import fcntl, os, subprocess, sys, time\nfrom pathlib import Path\n"
                  f"root=Path({str(self.data)!r})\n"
                  "lock=(root/'console.lock').open('a')\n"
                  "fcntl.flock(lock,fcntl.LOCK_EX)\n")
        if child:
            source += (f"worker=subprocess.Popen([{sys.executable!r},'-c','import time; time.sleep(60)'],start_new_session=True)\n"
                       "(root/'worker.pid').write_text(str(worker.pid))\n")
        source += "(root/'holder.ready').write_text(str(os.getpid()))\ntime.sleep(60)\n"
        if module:
            package = self.root / 'legacy' / 'console'
            package.mkdir(parents=True)
            (package / '__init__.py').touch()
            (package / '__main__.py').write_text(source)
            env = {**self.environment, 'PYTHONPATH': str(package.parent)}
            command = [sys.executable, '-m', 'console', '--cwd', str(self.project),
                       '--data-dir', str(self.data), '--port', '0']
        else:
            env = self.environment
            command = [sys.executable, '-c', source]
        process = subprocess.Popen(command, cwd=self.project, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.children.append(process)
        deadline = time.monotonic() + 5
        while not (self.data / 'holder.ready').exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail('lock fixture failed to start')
            time.sleep(.02)
        return process

    def test_stopped_console_starts_in_background_from_another_directory(self):
        queue = self.data / 'queue.json'
        queue.write_text('{"ids": [], "blocked": {}}\n')
        result = self.restart('--port', '0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('控制台已就绪', result.stdout)
        saved = json.loads((self.data / 'console-process.json').read_text())
        self.assertEqual(self.config(saved['port'])['cwd'], str(self.project))
        self.assertEqual(queue.read_text(), '{"ids": [], "blocked": {}}\n')
        self.assertTrue((self.data / 'console-server.log').exists())

    def test_restart_preserves_port_project_and_extra_scan_directories(self):
        extra = self.root / 'extra runs'
        extra.mkdir()
        first = self.restart('--port', '0', '--runs-dir', str(extra))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = json.loads((self.data / 'console-process.json').read_text())
        second = self.restart(cwd=False)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        after = json.loads((self.data / 'console-process.json').read_text())
        self.assertNotEqual(before['identity']['pid'], after['identity']['pid'])
        self.assertEqual(before['port'], after['port'])
        self.assertEqual(after['cwd'], str(self.project))
        self.assertEqual(after['runs_dirs'], [str(extra)])
        self.assertEqual(self.config(after['port'])['data_dir'], str(self.data))

    def test_legacy_console_is_identified_by_lock_and_detached_task_survives(self):
        previous = self.holder(module=True, child=True)
        worker_pid = int((self.data / 'worker.pid').read_text())
        worker_identity = process_identity(worker_pid)
        self.identities.append(worker_identity)
        self.assertEqual(lock_owner(self.data / 'console.lock'), previous.pid)
        result = self.restart('--port', '0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        previous.wait(timeout=5)
        self.assertEqual(process_identity(worker_pid), worker_identity)

    def test_unrelated_lock_holder_is_never_terminated(self):
        previous = self.holder()
        result = self.restart('--port', '0')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('其他程序持有', result.stderr)
        self.assertIsNone(previous.poll())
        self.assertEqual(lock_owner(self.data / 'console.lock'), previous.pid)

    def test_port_conflict_is_reported_without_stopping_the_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = self.restart('--port', str(port))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('启动失败', result.stderr)
            self.assertIn('console-server.log', result.stderr)
            self.assertEqual(listener.getsockname()[1], port)

    def test_concurrent_restart_is_rejected_without_starting_anything(self):
        with (self.data / 'console-restart.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.restart('--port', '0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('正在重启', result.stderr)
        self.assertFalse((self.data / 'console-process.json').exists())


if __name__ == '__main__':
    unittest.main()
