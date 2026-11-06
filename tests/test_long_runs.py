import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import _progress_text
from auto_moniter.health import run_health
from auto_moniter.history import prune_files
from auto_moniter.providers import PreparedCommand
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from tests.test_supervisor import ScriptedProvider, _verdict


class HistoryTests(unittest.TestCase):
    def test_reviewer_history_is_bounded_by_round_and_total_count_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}] + [{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(3)])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("continue", ["verify integration"])} for _ in range(3)] + [{"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, keep_rounds=1, restart_after_stall=0, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 4)
            report = json.loads((root / "run/report.json").read_text())
            self.assertEqual(len(report["records"]), 2)
            self.assertEqual(report["history"]["pruned_records"], 6)
            self.assertEqual(report["modules"][0]["status"], "accepted")
            self.assertFalse((root / "run/001-reviewer-prompt.md").exists())
            self.assertTrue((root / "run/004-reviewer-prompt.md").exists())

    def test_retention_bounds_records_files_and_preserves_total_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = [{"verdict": _verdict("continue", ["unfinished"]) } for _ in range(4)]
            scripts.append({"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"})
            worker = ScriptedProvider("worker", scripts)
            run = root / "run"
            run.mkdir()
            (run / "001-personal-note.log").write_text("keep")
            result = Supervisor(RunConfig("task", root, worker, no_review=True, keep_rounds=2, restart_after_stall=0, run_dir=run, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 5)
            state = json.loads((run / "state.json").read_text())
            self.assertEqual([record["index"] for record in state["rounds"]], [4, 5])
            self.assertEqual(state["history"]["pruned_worker_rounds"], 3)
            self.assertFalse((run / "001-worker.log").exists())
            self.assertTrue((run / "004-worker.log").exists())
            self.assertTrue((run / "001-personal-note.log").exists())
            self.assertEqual(run_health(run)["rounds"], 5)
            self.assertEqual(json.loads(_progress_text((run / "state.json").read_text()))["rounds"], 5)

    def test_resume_preserves_failure_streak_beyond_retained_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue"), "exit_code": 1} for _ in range(4)])
            first = Supervisor(RunConfig("task", root, worker, no_review=True, keep_rounds=1, run_dir=root / "run", retry_delay_s=0), log=lambda _: None)
            first.log = lambda message: first.request_stop() if "worker failure 4:" in message else None
            self.assertEqual(first.run().status, "stopped")
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue"), "exit_code": 1}])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", resume=True, failure_limit=5, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.rounds, 5)
            self.assertIn("failed 5 times", result.summary)
            state = json.loads((root / "run/state.json").read_text())
            self.assertEqual(state["history"]["keep_rounds"], 1)
            self.assertEqual(len(state["rounds"]), 1)

    def test_zero_retains_all_and_pruning_never_follows_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            target = root / "important"
            target.write_text("keep")
            (run / "001-worker.log").symlink_to(target)
            (run / "001-check-01.log").write_text("old check")
            self.assertEqual(prune_files(run, 2), 1)
            self.assertTrue((run / "001-worker.log").is_symlink())
            self.assertEqual(target.read_text(), "keep")
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue", ["unfinished"])} for _ in range(3)])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, keep_rounds=0, max_rounds=3, run_dir=root / "other", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.rounds, 3)
            self.assertEqual(len(json.loads((root / "other/state.json").read_text())["rounds"]), 3)


class PersistenceAndProcessTests(unittest.TestCase):
    def test_atomic_write_failure_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "state.json"
            target.write_text('{"original":true}')
            with patch("auto_moniter.storage.os.fsync", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    write_json(target, {"new": True}, durable=True)
            self.assertEqual(json.loads(target.read_text()), {"original": True})
            self.assertEqual([path.name for path in root.iterdir()], ["state.json"])

    def test_durable_write_syncs_file_and_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            with patch("auto_moniter.storage.os.fsync", wraps=os.fsync) as sync:
                write_json(target, {"persisted": True}, durable=True)
            self.assertEqual(sync.call_count, 2)
            self.assertEqual(json.loads(target.read_text()), {"persisted": True})

    def test_successful_cli_cannot_leave_background_process_with_closed_pipes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = """import os,signal,time
from pathlib import Path
pid=os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(0); os.close(1); os.close(2)
    time.sleep(30)
else:
    Path('background.pid').write_text(str(pid))
"""
            result = run_command(PreparedCommand([sys.executable, "-c", code]), root, timeout_s=3)
            self.assertEqual(result.interruption, "background processes")
            self.assertNotEqual(result.exit_code, 0)
            pid = int((root / "background.pid").read_text())
            status = Path(f"/proc/{pid}/stat")
            self.assertTrue(not status.exists() or status.read_text().rpartition(") ")[2].split()[0] == "Z")


if __name__ == "__main__":
    unittest.main()
