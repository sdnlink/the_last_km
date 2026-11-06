import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.guardian import Guardian
from auto_moniter.isolation import WorkerIsolation
from auto_moniter.paths import default_run_dir
from auto_moniter.paths import workspace_lock
from auto_moniter.providers import PreparedCommand, ProviderError, resolve_provider
from auto_moniter.redaction import redact
from auto_moniter.service import main as service_main
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from tests.test_supervisor import _verdict


def ignore_shutdown(config, sender):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (config.run_dir / "unresponsive.pid").write_text(str(os.getpid()))
    while True:
        time.sleep(1)


class HardeningTests(unittest.TestCase):
    def test_service_manifest_uses_argv_and_resumes_existing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "state"
            run.mkdir()
            (run / "state.json").write_text('{}')
            manifest = root / "task.json"
            manifest.write_text(json.dumps({"argv": ["run", "task", "--run-dir", str(run)]}))
            manifest.chmod(0o600)
            with patch.object(sys, "argv", ["service", str(manifest)]), patch("auto_moniter.service.os.execv") as execute:
                service_main()
                self.assertIn("--resume", execute.call_args.args[1])
                self.assertEqual(execute.call_args.args[1][:3], [sys.executable, "-m", "auto_moniter"])
            manifest.chmod(0o666)
            with patch.object(sys, "argv", ["service", str(manifest)]), self.assertRaises(ValueError):
                service_main()

    def test_storage_budget_stops_work_with_diagnostic_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shlex
            root = Path(tmp)
            code = "import time; time.sleep(30)"
            worker = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", max_run_bytes=1), log=lambda _: None).run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(json.loads((root / "run/alert.json").read_text())["status"], "budget_exceeded")

    def test_external_project_lock_still_blocks_after_project_lock_is_deleted(self):
        from auto_moniter.workspace import file_lock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = resolve_provider("command", command="echo never")
            with file_lock(workspace_lock(root)):
                result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "locked")
            self.assertFalse((root / "run/state.json").exists())

    def test_guardian_forces_unresponsive_monitor_to_exit_within_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = resolve_provider("command", command="echo never")
            guard = Guardian(RunConfig("task", root, worker, no_review=True, run_dir=root / "run"), shutdown_s=0.2, log=lambda _: None)
            def stop_when_ready():
                deadline = time.monotonic() + 3
                while not (root / "run/unresponsive.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                guard.request_stop()
            thread = threading.Thread(target=stop_when_ready)
            started = time.monotonic()
            thread.start()
            try:
                with patch("auto_moniter.guardian._monitor", ignore_shutdown):
                    result = guard.run()
                self.assertEqual(result.status, "stopped")
                self.assertLess(time.monotonic() - started, 4)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / "run/unresponsive.pid").read_text()), 0)
            finally:
                thread.join(3)

    def test_private_default_state_is_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(default_run_dir(root).is_relative_to(root))

    def test_real_worker_can_edit_project_but_cannot_change_state_or_protected_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project, run = base / "project", base / "private"
            project.mkdir(); run.mkdir()
            (project / "test.py").write_text("original")
            (run / "state.json").write_text(json.dumps({"protect_paths": ["test.py"]}))
            try:
                isolation = WorkerIsolation(project, run)
                isolation.verify("command")
            except ProviderError as exc:
                self.skipTest(str(exc))
            code = """from pathlib import Path
import sys
Path('work.py').write_text('ok')
for path in (Path('test.py'),Path(sys.argv[1])):
    try: path.write_text('forbidden')
    except OSError: pass
    else: raise SystemExit('write boundary failed')
"""
            command = isolation.wrap(PreparedCommand([sys.executable, "-c", code, str(run / "state.json")]), "command")
            result = run_command(command, project, timeout_s=3)
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual((project / "work.py").read_text(), "ok")
            self.assertEqual((project / "test.py").read_text(), "original")
            self.assertNotIn("forbidden", (run / "state.json").read_text())

    def test_inherited_memory_and_file_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = "x=bytearray(256*1024*1024)"
            result = run_command(PreparedCommand([sys.executable, "-c", code]), root, memory_bytes=64*1024*1024, timeout_s=3)
            self.assertNotEqual(result.exit_code, 0)
            result = run_command(PreparedCommand([sys.executable, "-c", "open('large','wb').write(b'x'*1000000)"]), root, file_bytes=8192, timeout_s=3)
            self.assertNotEqual(result.exit_code, 0)
            self.assertLessEqual((root / "large").stat().st_size, 8192)

    def test_redaction_covers_log_report_and_events_without_changing_executable_config(self):
        import shlex
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TEST_API_KEY": "sensitive-secret-for-test"}):
            root = Path(tmp)
            verdict = _verdict("done", evidence=["app.py"])
            verdict["summary"] = "completed with sensitive-secret-for-test"
            code = f"from pathlib import Path; import json; Path('app.py').write_text('done'); print(json.dumps({verdict!r}))"
            worker = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            run = root / "run"
            result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            for name in ("report.json", "state.json", "001-worker.log", "001-worker-text.md", "events.jsonl"):
                self.assertNotIn("sensitive-secret-for-test", (run / name).read_text())
            self.assertIn("[REDACTED]", (run / "report.json").read_text())
            self.assertNotIn("secret123", redact('password=secret123 Bearer token123'))


if __name__ == "__main__":
    unittest.main()
