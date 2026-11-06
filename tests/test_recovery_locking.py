import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.guardian import Guardian
from auto_moniter.paths import workspace_lock
from auto_moniter.supervisor import RunConfig, RunReport, Supervisor
from auto_moniter.workspace import file_lock
from tests.test_supervisor import ScriptedProvider


class RecoveryLockingTests(unittest.TestCase):
    def test_guardian_does_not_start_monitor_while_external_workspace_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guardian = Guardian(RunConfig("task", root, ScriptedProvider("worker", []),
                                          no_review=True, run_dir=root / "run"))
            with file_lock(workspace_lock(root)), patch.object(guardian, "_run_locked", return_value=RunReport("done", 0, "unexpected", "done")) as start:
                result = guardian.run()
            self.assertEqual(result.status, "locked")
            start.assert_not_called()

    def test_interrupted_cleanup_cannot_run_while_another_monitor_holds_external_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            provider = ScriptedProvider("worker", [])
            first = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=run), log=lambda _: None)
            first.request_stop()
            self.assertEqual(first.run().status, "stopped")
            state = (run / "state.json").read_bytes()
            cleanup = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=run, resume=True), log=lambda _: None)
            with file_lock(workspace_lock(root)), patch.object(cleanup, "_recover_interrupted_run") as recover:
                result = cleanup.stop_interrupted("guardian recovery")
            self.assertEqual(result.status, "recovery_failed")
            recover.assert_not_called()
            self.assertEqual((run / "state.json").read_bytes(), state)


if __name__ == "__main__":
    unittest.main()
