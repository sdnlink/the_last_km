import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.events import record
from auto_moniter.failures import classify
from auto_moniter.isolation import ReviewIsolation
from auto_moniter.providers import PreparedCommand, ProviderError
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict


class ControlsTests(unittest.TestCase):
    def test_explicit_maintenance_stop_reason_is_saved_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/'run';run.mkdir()
            (run/'control.json').write_text(json.dumps({'action':'stop','reason':'部署维护，保留候选后恢复'}))
            result=Supervisor(RunConfig('task',root,ScriptedProvider('worker',[]),no_review=True,
                git_history='off',run_dir=run),log=lambda _:None).run()
            self.assertEqual(result.status,'stopped')
            self.assertIn('部署维护',json.loads((run/'report.json').read_text())['summary'])

    def _wait(self, predicate, process):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            time.sleep(0.02)
        self.fail("monitor did not reach expected phase")

    def _start(self, root, body, guard=False):
        script = root / "agent.py"
        script.write_text("import json,time\nfrom pathlib import Path\n" + body)
        return subprocess.Popen([
            sys.executable, "-m", "auto_moniter", "run", "task", "--cwd", str(root), "--run-dir", str(root / "run"),
            "--provider", "command", "--exec", shlex.join([sys.executable, str(script)]), "--no-review", "--retry-delay", "0",
            *(["--guard"] if guard else []),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_pause_boundary_and_continue_without_restarting_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = f'''p=Path('.auto-moniter/first')
if not p.exists():
    p.write_text('yes')
    Path('run/control.json').write_text(json.dumps({{'action':'pause'}}))
    print(json.dumps({_verdict('continue', ['more work'])!r}))
else:
    Path('app.py').write_text('done')
    print(json.dumps({_verdict('done', evidence=['app.py'])!r}))
'''
            process = self._start(root, body)
            try:
                self._wait(lambda: (root / "run/heartbeat").exists() and "paused" in (root / "run/heartbeat").read_text(), process)
                self.assertFalse((root / "app.py").exists())
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["continue", str(root / "run")]), 0)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)

    def test_stop_command_interrupts_live_work_and_guard_does_not_restart(self):
        for guard in (False, True):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                process = self._start(root, "Path('.auto-moniter/started').write_text('yes')\ntime.sleep(30)\n", guard)
                try:
                    self._wait(lambda: (root / ".auto-moniter/started").exists(), process)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["stop", str(root / "run")]), 0)
                    stdout, stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 1, stdout + stderr)
                    self.assertEqual(json.loads((root / "run/report.json").read_text())["status"], "stopped")
                    if guard:
                        self.assertEqual(json.loads((root / "run/guardian.json").read_text())["restarts"], 0)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=8)


class EventsErrorsIsolationTests(unittest.TestCase):
    def test_event_history_rotates_and_has_bounded_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(800):
                record(root, "failure", message="x"*2000, index=index)
            files = list(root.glob("events*.jsonl"))
            self.assertEqual(len(files), 2)
            self.assertLess(sum(path.stat().st_size for path in files), 530000)
            entries = [json.loads(line) for path in files for line in path.read_text().splitlines()]
            self.assertTrue(all(len(entry["message"]) <= 1000 for entry in entries))

    def test_error_categories(self):
        for text, expected in [("HTTP 401 unauthorized", ("authentication", "stop")), ("429 rate limit", ("rate_limit", "backoff")),
                               ("session not found", ("session", "fresh_session")), ("connection reset", ("network", "retry")),
                               ("test assertion failed", ("execution", "retry"))]:
            self.assertEqual(classify(text), expected)

    def test_authentication_failure_does_not_spin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue"), "text": "HTTP 401 unauthorized", "exit_code": 1}])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "action_required")
            self.assertEqual(len(worker.calls), 1)

    def test_strict_isolation_failure_blocks_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [])
            reviewer = ScriptedProvider("reviewer", [])
            with patch("auto_moniter.isolation.shutil.which", return_value="/usr/bin/bwrap"), patch("auto_moniter.isolation.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "namespace disabled")):
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_isolation="strict", run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "invalid")
            self.assertIn("probe failed", result.summary)
            self.assertEqual(worker.calls, [])
            self.assertFalse(list(root.glob(".review-probe-*")))

    def test_strict_wrapper_enforces_mounts_and_rejects_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            with patch("auto_moniter.isolation.shutil.which", return_value="/usr/bin/bwrap"):
                isolation = ReviewIsolation(root, run)
            command = isolation.wrap(PreparedCommand(["codex", "exec"], stdin_text="prompt"), "codex")
            self.assertIn("--unshare-pid", command.argv)
            self.assertIn("--ro-bind", command.argv)
            self.assertEqual(command.argv[-2:], ["codex", "exec"])
            self.assertEqual(command.stdin_text, "prompt")
            with self.assertRaises(ProviderError):
                isolation.wrap(PreparedCommand(["paseo", "run"]), "paseo")

    def test_real_strict_mounts_deny_reviewer_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            try:
                ReviewIsolation(root, run).verify("codex")
            except ProviderError as exc:
                self.skipTest(str(exc))
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            reviewer = ScriptedProvider("codex", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "forbidden.py"}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_isolation="strict", run_dir=run, max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
            self.assertNotEqual(result.status, "done")
            self.assertFalse((root / "forbidden.py").exists())
            self.assertIn("Read-only file system", (run / "001-reviewer.log").read_text())


if __name__ == "__main__":
    unittest.main()
