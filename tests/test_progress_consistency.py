import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.health import run_health
from auto_moniter.workspace import file_lock


class ProgressConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.state("running")
        (self.run / "supervisor.lock").touch()

    def state(self, status):
        (self.run / "state.json").write_text(json.dumps({"status": status, "rounds": [], "next_round": 3}))

    def report(self, status="done"):
        (self.run / "report.json").write_text(json.dumps({"status": status, "rounds": 2, "summary": "old report", "stop_reason": status}))

    def test_stale_report_does_not_override_running_state(self):
        self.report()
        with file_lock(self.run / "supervisor.lock"):
            health = run_health(self.run)
            self.assertEqual(health["status"], "running")
            self.assertFalse(health["delivery_confirmed"])
            self.assertEqual(health["report_warning"], "report_state_mismatch")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["watch", str(self.run), "--once"]), 0)
            self.assertEqual(json.loads(out.getvalue())["status"], "running")

    def test_watch_waits_until_monitor_releases_final_run_lock(self):
        self.state("done")
        self.report()
        with contextlib.ExitStack() as stack:
            stack.enter_context(file_lock(self.run / "supervisor.lock"))
            def release(_):
                stack.close()
            with patch("auto_moniter.cli.time.sleep", side_effect=release) as sleep, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["watch", str(self.run)]), 0)
            sleep.assert_called_once()

    def test_watch_waits_for_guardian_between_monitor_restarts(self):
        self.report("stopped")
        self.state("stopped")
        with contextlib.ExitStack() as stack:
            stack.enter_context(file_lock(self.run / "guardian.lock"))
            self.assertEqual(run_health(self.run)["health"], "recovering")
            def finish(_):
                self.state("done")
                self.report()
                stack.close()
            with patch("auto_moniter.cli.time.sleep", side_effect=finish) as sleep, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["watch", str(self.run)]), 0)
            sleep.assert_called_once()

    def test_dead_monitor_without_final_report_does_not_leave_watch_waiting_forever(self):
        with patch("auto_moniter.cli.time.sleep", side_effect=AssertionError("must exit")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["watch", str(self.run)]), 1)
        self.assertEqual(run_health(self.run)["health"], "monitor_exited")

    def test_missing_invalid_or_mismatched_state_cannot_confirm_delivery(self):
        self.report()
        for content in (None, "{", json.dumps({"status": "failed"})):
            with self.subTest(content=content):
                (self.run / "state.json").unlink(missing_ok=True)
                if content is not None:
                    (self.run / "state.json").write_text(content)
                health = run_health(self.run)
                self.assertFalse(health["delivery_confirmed"])
                self.assertNotEqual(health["health"], "finished")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["watch", str(self.run)]), 1)

    def test_corrupt_report_and_residual_process_record_do_not_confirm_delivery(self):
        self.state("done")
        (self.run / "report.json").write_text("{")
        self.assertEqual(run_health(self.run)["report_warning"], "report_invalid")
        self.report()
        (self.run / "process.json").write_text("{}")
        self.assertEqual(run_health(self.run)["report_warning"], "outstanding_process_record")
        self.assertFalse(run_health(self.run)["delivery_confirmed"])

    def test_read_only_inspection_preserves_files_and_lock_content(self):
        self.state("done")
        self.report()
        before = {path.name: path.read_bytes() for path in self.run.iterdir()}
        self.assertTrue(run_health(self.run)["delivery_confirmed"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["watch", str(self.run), "--once"]), 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.run.iterdir()}, before)

    def test_partial_report_is_not_confirmed_even_with_matching_done_state(self):
        self.state("done")
        (self.run / "report.json").write_text(json.dumps({"status": "done"}))
        self.assertFalse(run_health(self.run)["delivery_confirmed"])
        self.assertEqual(run_health(self.run)["report_warning"], "report_invalid")


if __name__ == "__main__":
    unittest.main()
