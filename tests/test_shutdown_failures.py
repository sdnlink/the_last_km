import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.providers import resolve_provider, TurnResult
from auto_moniter.supervisor import RecoveryError, RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict


class ShutdownFailureTests(unittest.TestCase):
    def test_paseo_stop_failure_prevents_next_worker_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake-paseo"
            fake.write_text(f'''#!{sys.executable}
import json,sys
from pathlib import Path
args=sys.argv[1:]
with Path('.auto-moniter/calls').open('a') as out:
    out.write(json.dumps(args)+'\\n')
if args[0] in ('run', 'send'):
    print(json.dumps({{'agentId':'11111111-1111-1111-1111-111111111111'}}))
elif args[0] == 'inspect':
    print(json.dumps({{'status':'awaiting_permission'}}))
elif args[:2] == ['agent','stop']:
    sys.exit(1)
elif args[0] == 'logs':
    print('Waiting for approval')
''')
            fake.chmod(0o755)
            provider = resolve_provider("paseo", paseo_backend="claude")
            provider.binary = str(fake)
            result = Supervisor(RunConfig("task", root, provider, no_review=True, max_rounds=2,
                                          retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "recovery_failed")
            calls = [json.loads(line) for line in (root / ".auto-moniter/calls").read_text().splitlines()]
            self.assertEqual(len([call for call in calls if call[0] in {"run", "send"}]), 1)
            state = json.loads((root / "run/state.json").read_text())
            self.assertEqual(state["session_id"], "11111111-1111-1111-1111-111111111111")

    def test_launch_failure_with_known_paseo_id_does_not_ignore_stop_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = resolve_provider("paseo", paseo_backend="claude")
            monitor = Supervisor(RunConfig("task", root, provider, no_review=True,
                                           run_dir=root / "run"), log=lambda _: None)
            monitor.run_dir.mkdir()
            monitor._write_state([], "running", 0, "", "")
            launch = TurnResult("", exit_code=1, stdout=json.dumps({"agentId": "11111111-1111-1111-1111-111111111111"}))
            with patch("auto_moniter.supervisor.run_command", return_value=launch), patch("auto_moniter.supervisor._paseo_stop", return_value=False):
                with self.assertRaises(RecoveryError):
                    monitor._invoke(provider, "task", "", role="worker", index=1, model="", extra_args=[], read_only=False)

    def test_live_native_process_identity_is_not_forgotten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=root / "run"), log=lambda _: None)
            monitor.run_dir.mkdir()
            child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
            try:
                monitor._remember_child(child)
                saved = (monitor.run_dir / "process.json").read_bytes()
                with self.assertRaises(RecoveryError):
                    monitor._forget_child()
                self.assertEqual((monitor.run_dir / "process.json").read_bytes(), saved)
            finally:
                child.kill()
                child.wait(timeout=3)
            monitor._forget_child()
            self.assertFalse((monitor.run_dir / "process.json").exists())

    def test_failed_native_cleanup_preserves_identity_and_can_be_recovered_without_new_work(self):
        from auto_moniter.supervisor import run_command
        for role in ("worker", "check"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / "run"
                provider = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
                children = []

                def fail_cleanup(prepared, cwd, **kwargs):
                    if role == "check" and prepared.argv[:2] != ["sh", "-lc"]:
                        return run_command(prepared, cwd, **kwargs)
                    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
                    children.append(child)
                    kwargs["on_child"](child)
                    child.kill()
                    child.wait(timeout=3)
                    raise RuntimeError("background process cleanup could not be confirmed")

                monitor = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=run,
                                               checks=["true"] if role == "check" else [], retry_delay_s=0), log=lambda _: None)
                try:
                    with patch("auto_moniter.supervisor.run_command", side_effect=fail_cleanup):
                        result = monitor.run()
                    self.assertEqual(result.status, "recovery_failed")
                    self.assertEqual(json.loads((run / "process.json").read_text())["pid"], children[0].pid)
                    cleanup = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=run,
                                                   resume=True), log=lambda _: None)
                    self.assertEqual(cleanup.stop_interrupted("test cleanup").status, "stopped")
                    self.assertFalse((run / "process.json").exists())
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=3)

    def test_unsuccessful_external_shutdown_is_resumable_instead_of_claiming_stopped(self):
        for requested in ("stopped", "max_rounds"):
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                provider = ScriptedProvider("worker", [{"verdict": _verdict("continue")}])
                monitor = Supervisor(RunConfig("task", root, provider, no_review=True,
                                               max_rounds=1, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None)
                if requested == "stopped":
                    monitor.request_stop()
                with patch.object(monitor, "_stop_external_agent", return_value=["daemon stop failed"]):
                    result = monitor.run()
                self.assertEqual(result.status, "recovery_failed")
                self.assertIn("daemon stop failed", result.summary)
                self.assertEqual(json.loads((root / "run/state.json").read_text())["status"], "recovery_failed")


if __name__ == "__main__":
    unittest.main()
