import contextlib
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from auto_moniter.cli import main
from auto_moniter.supervisor import RunConfig, Supervisor, _EXECUTION_DEFAULTS
from auto_moniter.providers import resolve_provider
from tests.test_supervisor import ScriptedProvider, _verdict


class ExecutionResumeTests(unittest.TestCase):
    def checkpoint(self, root, **settings):
        monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                       no_review=True, run_dir=root / "run", **settings), log=lambda _: None)
        monitor.request_stop()
        self.assertEqual(monitor.run().status, "stopped")
        return root / "run"

    def test_all_execution_settings_are_inherited_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = {"turn_timeout_s": 5400, "idle_timeout_s": 0, "retry_delay_s": 2,
                        "check_timeout_s": 3600, "max_output_bytes": 32 * 1024 * 1024}
            run = self.checkpoint(root, **settings)
            resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=run, resume=True), log=lambda _: None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status, "stopped")
            self.assertEqual(json.loads((run / "state.json").read_text())["execution_settings"], settings)

    def test_explicit_default_values_can_override_saved_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.checkpoint(root, idle_timeout_s=0, check_timeout_s=3600, max_output_bytes=1024)
            resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=run, resume=True, **_EXECUTION_DEFAULTS), log=lambda _: None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status, "stopped")
            self.assertEqual(json.loads((run / "state.json").read_text())["execution_settings"], _EXECUTION_DEFAULTS)

    def test_legacy_checkpoint_uses_existing_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.checkpoint(root)
            state = json.loads((run / "state.json").read_text())
            state.pop("execution_settings")
            (run / "state.json").write_text(json.dumps(state))
            resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=run, resume=True), log=lambda _: None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status, "stopped")
            self.assertEqual(json.loads((run / "state.json").read_text())["execution_settings"], _EXECUTION_DEFAULTS)

    def test_corrupt_settings_refuse_resume_without_overwriting_checkpoint(self):
        cases = [None, [], {"idle_timeout_s": -1}, {"check_timeout_s": 0}, {"retry_delay_s": float("nan")},
                 {"max_output_bytes": 1.5}, {"max_output_bytes": True}, {"unknown": 1}]
        for settings in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = self.checkpoint(root)
                state = json.loads((run / "state.json").read_text())
                state["execution_settings"] = settings
                (run / "state.json").write_text(json.dumps(state))
                before = (run / "state.json").read_bytes()
                resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                               no_review=True, run_dir=run, resume=True), log=lambda _: None)
                self.assertEqual(resumed.run().status, "invalid")
                self.assertEqual((run / "state.json").read_bytes(), before)

    def test_cli_resume_distinguishes_omission_from_explicit_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            base = ["run", "task", "-C", str(root), "--provider", "command", "--exec", "echo unused",
                    "--no-review", "--run-dir", str(run)]
            # A stop request lets us exercise the real CLI without launching an agent.
            run.mkdir()
            (run / "control.json").write_text(json.dumps({"action": "stop"}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(base + ["--idle-timeout", "0", "--check-timeout", "1h", "--max-output-bytes", "1024"]), 1)
                self.assertEqual(main(base + ["--resume"]), 1)
            settings = json.loads((run / "state.json").read_text())["execution_settings"]
            self.assertEqual(settings["idle_timeout_s"], 0)
            self.assertEqual(settings["check_timeout_s"], 3600)
            self.assertEqual(settings["max_output_bytes"], 1024)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(base + ["--resume", "--idle-timeout", "15m", "--max-output-bytes", "8388608"]), 1)
            settings = json.loads((run / "state.json").read_text())["execution_settings"]
            self.assertEqual(settings["idle_timeout_s"], 900)
            self.assertEqual(settings["max_output_bytes"], 8388608)
            self.assertEqual(settings["check_timeout_s"], 3600)

    def test_inherited_output_limit_is_actually_enforced_after_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdict = _verdict("done", evidence=["app.py"])
            verdict["summary"] = "x" * 2000
            code = f"from pathlib import Path; import json; Path('app.py').write_text('ok'); print(json.dumps({verdict!r}))"
            provider = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            first = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=root / "run",
                                         max_output_bytes=1024, retry_delay_s=0), log=lambda _: None)
            first.request_stop()
            self.assertEqual(first.run().status, "stopped")
            resumed = Supervisor(RunConfig("task", root, provider, no_review=True, resume=True,
                                           max_rounds=1, run_dir=root / "run"), log=lambda _: None)
            result = resumed.run()
            self.assertEqual(result.status, "max_rounds")
            self.assertEqual(result.records[0].interruption, "output limit")


if __name__ == "__main__":
    unittest.main()
