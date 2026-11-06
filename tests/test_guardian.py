import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.guardian import Guardian
from auto_moniter.processes import recover_process
from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import file_lock
from tests.test_supervisor import _verdict


def finish_then_crash(config, sender):
    Supervisor(config).run()
    os._exit(9)


class GuardianTests(unittest.TestCase):
    def _command(self, root, body):
        script = root / "agent.py"
        script.write_text("import json, os, signal, time\nfrom pathlib import Path\n" + body)
        return resolve_provider("command", command=shlex.join([sys.executable, str(script)]))

    def test_unexpected_monitor_death_resumes_same_run_without_resetting_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = self._command(root, f'''
marker=Path('.auto-moniter/first')
if not marker.exists():
    marker.write_text('started')
    while not Path('run/guardian.json').exists():
        time.sleep(0.01)
    os.kill(json.loads(Path('run/guardian.json').read_text())['monitor_pid'], signal.SIGKILL)
    time.sleep(30)
else:
    Path('app.py').write_text('delivered')
    print(json.dumps({_verdict('done', evidence=['app.py'])!r}))
''')
            result = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", retry_delay_s=0), delay_s=0, log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            state = json.loads((root / "run/state.json").read_text())
            guard = json.loads((root / "run/guardian.json").read_text())
            self.assertEqual(state["usage"]["agent_calls"], 2)
            self.assertEqual(state["modules"][0]["status"], "accepted")
            self.assertEqual(guard["restarts"], 1)
            self.assertFalse((root / "run/process.json").exists())

    def test_restart_limit_stops_leftover_worker_and_preserves_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = self._command(root, '''
while True:
    try:
        pid=json.loads(Path('run/guardian.json').read_text())['monitor_pid']
        if pid == os.getppid() or Path('/proc/'+str(pid)).exists():
            break
    except (FileNotFoundError, ValueError):
        pass
    time.sleep(0.01)
os.kill(pid, signal.SIGKILL)
time.sleep(30)
''')
            result = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run"), max_restarts=0, delay_s=0, log=lambda _: None).run()
            self.assertEqual(result.status, "restart_limit")
            self.assertEqual(json.loads((root / "run/state.json").read_text())["usage"]["agent_calls"], 1)
            self.assertEqual(json.loads((root / "run/guardian.json").read_text())["status"], "restart_limit")
            self.assertFalse((root / "run/process.json").exists())

    def test_declared_limits_and_success_do_not_restart(self):
        for status in ("done", "continue"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worker = self._command(root, f"Path('app.py').write_text('delivered')\nprint(json.dumps({_verdict(status, evidence=['app.py'])!r}))\n")
                result = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", max_rounds=1, retry_delay_s=0), delay_s=0, log=lambda _: None).run()
                self.assertEqual(result.status, "done" if status == "done" else "max_rounds")
                self.assertEqual(json.loads((root / "run/guardian.json").read_text())["restarts"], 0)

    def test_guardian_lock_prevents_duplicate_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = resolve_provider("command", command="echo should-not-run")
            with file_lock(root / "run/guardian.lock"):
                result = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "locked")
            self.assertFalse((root / "run/state.json").exists())

    def test_saved_delivery_report_prevents_restart_if_pipe_reply_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = self._command(root, f"Path('app.py').write_text('delivered')\nprint(json.dumps({_verdict('done', evidence=['app.py'])!r}))\n")
            with patch("auto_moniter.guardian._monitor", finish_then_crash):
                result = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run"), delay_s=0, log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(json.loads((root / "run/guardian.json").read_text())["restarts"], 0)

    def test_budget_exhaustion_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = self._command(root, f"print(json.dumps({_verdict('continue', ['pending'])!r}))\n")
            result = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", max_agent_calls=1, retry_delay_s=0), delay_s=0, log=lambda _: None).run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(json.loads((root / "run/guardian.json").read_text())["restarts"], 0)

    def test_restart_counter_is_retained_and_fresh_start_does_not_overwrite_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = self._command(root, f"Path('app.py').write_text('delivered')\nprint(json.dumps({_verdict('done', evidence=['app.py'])!r}))\n")
            config = RunConfig("task", root, worker, no_review=True, run_dir=root / "run")
            supervisor = Supervisor(config, log=lambda _: None)
            supervisor.request_stop()
            self.assertEqual(supervisor.run().status, "stopped")
            saved = json.dumps({"restarts": 2, "status": "stopped"})
            (root / "run/guardian.json").write_text(saved)
            self.assertEqual(Guardian(config, log=lambda _: None).run().status, "invalid")
            self.assertEqual((root / "run/guardian.json").read_text(), saved)
            config.resume = True
            result = Guardian(config, log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(json.loads((root / "run/guardian.json").read_text())["restarts"], 2)

    def test_cli_signal_stops_guardian_and_monitor_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = self._command(root, "Path('.auto-moniter/agent.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
            process = subprocess.Popen([
                sys.executable, "-m", "auto_moniter", "run", "task", "--cwd", str(root),
                "--provider", "command", "--exec", worker.start_template, "--no-review",
                "--run-dir", str(root / "run"), "--guard", "--guard-delay", "0",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (root / ".auto-moniter/agent.pid").exists():
                    if process.poll() is not None:
                        self.fail(str(process.communicate()))
                    time.sleep(0.02)
                self.assertTrue((root / ".auto-moniter/agent.pid").exists())
                process.terminate()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 1, stdout + stderr)
                guard = json.loads((root / "run/guardian.json").read_text())
                self.assertEqual(guard["restarts"], 0)
                self.assertEqual(json.loads((root / "run/report.json").read_text())["status"], "stopped")
                self.assertFalse((root / "run/process.json").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)
                saved = root / "run/process.json"
                if saved.exists():
                    recover_process(json.loads(saved.read_text()), timeout_s=0.1)


if __name__ == "__main__":
    unittest.main()
