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

from auto_moniter.processes import process_identity, recover_process
from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict


class RecoveryTests(unittest.TestCase):
    def test_sigkill_monitor_recovery_stops_old_native_worker_before_launching_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            script = root / "agent.py"
            script.write_text(
                "import json, os, time\nfrom pathlib import Path\n"
                "marker=Path('.auto-moniter/first.pid')\n"
                "if not marker.exists():\n"
                "    marker.write_text(str(os.getpid()))\n"
                "    time.sleep(30)\n"
                "else:\n"
                "    try:\n"
                "        stat=Path('/proc/'+marker.read_text()+'/stat').read_text().rpartition(') ')[2].split()\n"
                "        assert stat[0] == 'Z', 'old worker is still running'\n"
                "    except FileNotFoundError:\n"
                "        pass\n"
                "    Path('app.py').write_text('delivered')\n"
                f"    print(json.dumps({_verdict('done', evidence=['app.py'])!r}))\n"
            )
            args = [sys.executable, "-m", "auto_moniter", "run", "--cwd", str(root), "--run-dir", str(run),
                    "--provider", "command", "--exec", shlex.join([sys.executable, str(script)]), "--no-review", "--retry-delay", "0"]
            monitor = subprocess.Popen([*args, "task"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            saved = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (root / ".auto-moniter/first.pid").exists() and (run / "process.json").exists():
                        saved = json.loads((run / "process.json").read_text())
                        break
                    if monitor.poll() is not None:
                        self.fail(str(monitor.communicate()))
                    time.sleep(0.02)
                self.assertIsNotNone(saved)
                monitor.kill()
                monitor.communicate(timeout=3)
                result = subprocess.run([*args, "--resume"], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("resume process recovery: terminated", result.stdout)
                self.assertFalse((run / "process.json").exists())
                self.assertEqual(json.loads((run / "report.json").read_text())["status"], "done")
            finally:
                if monitor.poll() is None:
                    monitor.kill()
                monitor.communicate(timeout=3)
                if saved:
                    try:
                        recover_process(saved, timeout_s=0.1)
                    except (OSError, RuntimeError):
                        pass

    def test_pid_reuse_does_not_signal_an_unrelated_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            saved = process_identity(child.pid)
            saved["started"] -= 1
            with patch("auto_moniter.processes.os.killpg") as kill:
                self.assertEqual(recover_process(saved), "pid_reused")
                kill.assert_not_called()
            self.assertIsNone(child.poll())
        finally:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    def test_recovery_stops_previous_paseo_worker_and_archives_previous_reviewer(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / "run"
                run.mkdir()
                fake = root / "fake-paseo"
                fake.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
args=sys.argv[1:]
with Path('run/recovery-events').open('a') as output:
    output.write(json.dumps(args)+'\\n')
if args[0] == 'inspect':
    if {unavailable!r}:
        sys.exit(1)
    print(json.dumps({{'status':'running'}}))
elif args[:2] in (['agent','stop'],['agent','archive']):
    pass
else:
    sys.exit(2)
''')
                fake.chmod(0o755)
                worker = resolve_provider("paseo", paseo_backend="claude")
                reviewer = resolve_provider("paseo", paseo_backend="claude", role="reviewer")
                worker.binary = reviewer.binary = str(fake)
                (run / "requirement.md").write_text("task")
                (run / "state.json").write_text(json.dumps({"status": "running", "session_id": "worker-id", "active_reviewer_session_id": "reviewer-id", "baseline_files": {}}))
                supervisor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run, resume=True), log=lambda _: None)
                supervisor.request_stop()
                result = supervisor.run()
                events = [json.loads(line) for line in (run / "recovery-events").read_text().splitlines()]
                if unavailable:
                    self.assertEqual(result.status, "recovery_failed")
                    self.assertIn("No new agent was started", result.summary)
                else:
                    self.assertEqual(result.status, "stopped")
                    self.assertEqual(events[:5], [
                        ["inspect", "--json", "worker-id"], ["agent", "stop", "--json", "worker-id"],
                        ["inspect", "--json", "reviewer-id"], ["agent", "stop", "--json", "reviewer-id"],
                        ["agent", "archive", "--json", "reviewer-id"],
                    ])
                    self.assertEqual(json.loads((run / "state.json").read_text())["active_reviewer_session_id"], "")
                self.assertFalse(any(event[0] in {"run", "send"} for event in events))
                if unavailable:
                    fake.write_text(fake.read_text().replace("if True:", "if False:"))
                    retry = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run, resume=True), log=lambda _: None)
                    retry.request_stop()
                    self.assertEqual(retry.run().status, "stopped")

    def test_resume_rejects_provider_change_before_running_or_stopping_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = resolve_provider("command", command="echo original")
            supervisor = Supervisor(RunConfig("task", root, original, no_review=True, run_dir=root / "run"), log=lambda _: None)
            supervisor.request_stop()
            self.assertEqual(supervisor.run().status, "stopped")
            changed = resolve_provider("command", command="echo changed")
            before = (root / "run/state.json").read_text()
            with patch("auto_moniter.supervisor.run_command") as run:
                result = Supervisor(RunConfig("task", root, changed, no_review=True, run_dir=root / "run", resume=True), log=lambda _: None).run()
                run.assert_not_called()
            self.assertEqual(result.status, "invalid")
            self.assertIn("original configuration", result.summary)
            self.assertEqual((root / "run/state.json").read_text(), before)

    def test_reworded_gaps_without_workspace_progress_trigger_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", [f"attempt {number}: another explanation"]), "session_id": "11111111-1111-1111-1111-111111111111"}
                for number in range(3)
            ] + [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, restart_after_stall=2, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertTrue(worker.calls[1]["session_id"])
            self.assertEqual(worker.calls[3]["session_id"], "")
            self.assertIn("different concrete approach", worker.calls[3]["prompt"])
            self.assertEqual(json.loads((root / "run/state.json").read_text())["recovery_count"], 1)

    def test_explicit_round_limit_stops_idle_paseo_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake-paseo"
            fake.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
args=sys.argv[1:]
if args[0] == 'run':
    print(json.dumps({{'agentId':'11111111-1111-1111-1111-111111111111'}}))
elif args[0] == 'inspect':
    print(json.dumps({{'status':'idle'}}))
elif args[0] == 'logs':
    print(json.dumps({_verdict('continue', ['work remains'])!r}))
elif args[:2] == ['agent', 'stop']:
    Path('run/stopped-agent').write_text(args[-1])
else:
    sys.exit(2)
''')
            fake.chmod(0o755)
            worker = resolve_provider("paseo", paseo_backend="claude")
            worker.binary = str(fake)
            result = Supervisor(RunConfig("task", root, worker, no_review=True, max_rounds=1, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertEqual((root / "run/stopped-agent").read_text(), "11111111-1111-1111-1111-111111111111")


if __name__ == "__main__":
    unittest.main()
