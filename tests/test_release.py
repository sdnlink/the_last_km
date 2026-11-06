import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider


class ReleaseTests(unittest.TestCase):
    def test_missing_selected_executable_fails_before_creating_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = resolve_provider("codex")
            worker.binary = str(root / "does-not-exist")
            result = Supervisor(RunConfig("task", root, worker, no_review=True), log=lambda _: None).run()
            self.assertEqual(result.status, "invalid")
            self.assertIn("executable not found", result.summary)
            self.assertFalse((root / ".auto-moniter").exists())

    def test_bad_configuration_fails_before_creating_run(self):
        cases = [{"check_timeout_s": 0}, {"turn_timeout_s": float("nan")}, {"retry_delay_s": -1},
                 {"checks": [""]}, {"review_isolation": "typo"}, {"max_rounds": -1}]
        for values in cases:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), no_review=True, **values), log=lambda _: None).run()
                self.assertEqual(result.status, "invalid")
                self.assertFalse((root / ".auto-moniter").exists())

    def test_selftest_runs_without_paid_provider_and_cleans_project(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["selftest"]), 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value["passed"])
        self.assertEqual(value["paid_model_calls"], 0)

    def test_doctor_is_offline_and_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("subprocess.Popen", side_effect=AssertionError("doctor must not launch agents")), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["doctor", "-C", str(root)]), 0)
            self.assertTrue(json.loads(output.getvalue())["supported"])
            self.assertEqual(list(root.iterdir()), [])

    def test_future_state_version_refused_without_overwriting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            saved = json.dumps({"state_version": 99, "status": "running"})
            (run / "state.json").write_text(saved)
            (run / "requirement.md").write_text("task")
            result = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), no_review=True, run_dir=run, resume=True), log=lambda _: None).run()
            self.assertEqual(result.status, "invalid")
            self.assertIn("Unsupported state version", result.summary)
            self.assertEqual((run / "state.json").read_text(), saved)


if __name__ == "__main__":
    unittest.main()
