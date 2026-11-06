import contextlib
import fcntl
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.paths import project_runs_dir
from auto_moniter.retention import run_storage
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def run_dir(self, name, status="done", days=30, **state):
        run = self.runs / name
        run.mkdir()
        write_json(run / "state.json", {"cwd": str(self.project), "state_version": 1, "status": status, **state})
        write_json(run / "report.json", {"status": status})
        (run / "supervisor.lock").write_text("12345")
        (run / "001-worker.log").write_bytes(b"x" * 1024)
        at = time.time() - days * 86400
        os.utime(run / "report.json", (at, at))
        return run

    def inspect(self, **options):
        return run_storage(self.project, runs_dir=self.runs, **options)

    def test_preview_has_no_writes_and_apply_honors_age_and_newest_count(self):
        old = self.run_dir("old", days=30)
        retained = self.run_dir("retained", days=20)
        recent = self.run_dir("recent", days=1)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.runs.rglob("*") if p.is_file()}
        preview = self.inspect(keep_runs=1)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        self.assertEqual([r["name"] for r in preview["runs"] if r["selected"]], ["old", "retained"])
        self.assertGreater(preview["candidate_bytes"], 2048)
        self.assertEqual(preview["removed_bytes"], 0)
        applied = self.inspect(keep_runs=1, apply=True)
        self.assertEqual(applied["errors"], [])
        self.assertEqual(applied["removed_bytes"], preview["candidate_bytes"])
        self.assertFalse(old.exists())
        self.assertFalse(retained.exists())
        self.assertTrue(recent.exists())

    def test_age_and_default_failure_preservation(self):
        self.run_dir("old_failure", "failed")
        self.run_dir("old_success")
        self.run_dir("new_success", days=1)
        result = self.inspect(keep_runs=0)
        rows = {r["name"]: r for r in result["runs"]}
        self.assertEqual(rows["old_failure"]["reason"], "failed_run_preserved")
        self.assertEqual(rows["new_success"]["reason"], "too_recent")
        self.assertTrue(rows["old_success"]["selected"])

    def test_recoverable_states_are_kept_even_when_include_failed(self):
        statuses = ["stopped", "blocked", "running", "action_required", "recovery_failed", "budget_exceeded"]
        for status in statuses + ["failed", "stalled", "max_rounds"]:
            self.run_dir(status, status)
        result = self.inspect(keep_runs=0, older_than=0, include_failed=True, apply=True)
        self.assertEqual(result["errors"], [])
        for status in statuses:
            self.assertTrue((self.runs / status).is_dir())
        self.assertFalse((self.runs / "failed").exists())
        self.assertFalse((self.runs / "stalled").exists())
        self.assertFalse((self.runs / "max_rounds").exists())

    def test_held_supervisor_or_guardian_lock_prevents_removal_despite_done_report(self):
        for name in ("supervisor.lock", "guardian.lock"):
            with self.subTest(name=name):
                run = self.run_dir(name)
                (run / name).touch()
                with (run / name).open("r") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = self.inspect(keep_runs=0, older_than=0, apply=True)
                row = next(r for r in result["runs"] if r["name"] == name)
                self.assertEqual(row["reason"], "active_lock")
                self.assertTrue(run.exists())

    def test_unknown_mismatched_process_or_other_project_records_are_preserved(self):
        missing = self.run_dir("missing")
        (missing / "supervisor.lock").unlink()
        (self.run_dir("bad") / "state.json").write_text("[")
        self.run_dir("future", state_version=2)
        self.run_dir("status_type", status=[])
        self.run_dir("other", cwd=str(self.root))
        (self.run_dir("process") / "process.json").write_text("{}")
        write_json(self.run_dir("inconsistent") / "report.json", {"status": "failed"})
        result = self.inspect(keep_runs=0, older_than=0, include_failed=True, apply=True)
        self.assertFalse(any(r["selected"] for r in result["runs"]))
        self.assertEqual(len(list(self.runs.iterdir())), 7)

    def test_links_are_not_followed_during_measurement_or_removal(self):
        external = self.root / "important"
        external.mkdir()
        (external / "source.py").write_bytes(b"x" * 100_000)
        (self.runs / "linked-run").symlink_to(external, target_is_directory=True)
        run = self.run_dir("old")
        (run / "linked-tree").symlink_to(external, target_is_directory=True)
        (run / "linked-file").symlink_to(external / "source.py")
        result = self.inspect(keep_runs=0, older_than=0, apply=True)
        self.assertLess(result["total_bytes"], 10_000)
        self.assertEqual((external / "source.py").stat().st_size, 100_000)
        self.assertTrue((self.runs / "linked-run").is_symlink())
        self.assertFalse(run.exists())

    def test_symlink_metadata_lock_and_root_are_refused(self):
        external = self.root / "state"
        external.write_text(json.dumps({"cwd": str(self.project), "status": "done"}))
        for name in ("state.json", "report.json", "supervisor.lock", "guardian.lock"):
            run = self.run_dir(name)
            (run / name).unlink(missing_ok=True)
            (run / name).symlink_to(external)
        result = self.inspect(keep_runs=0, older_than=0, apply=True)
        self.assertFalse(any(r["selected"] for r in result["runs"]))
        link = self.root / "link"
        link.symlink_to(self.runs, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            run_storage(self.project, runs_dir=link, apply=True)

    def test_metadata_is_rechecked_under_locks_before_deletion(self):
        run = self.run_dir("old")
        from auto_moniter import retention
        remove = retention._remove

        def resumed(*args):
            write_json(run / "state.json", {"cwd": str(self.project), "status": "running"})
            return remove(*args)

        with patch("auto_moniter.retention._remove", side_effect=resumed):
            result = self.inspect(keep_runs=0, older_than=0, apply=True)
        self.assertTrue(run.exists())
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("run_changed_since_preview", result["errors"][0]["error"])

    def test_mounted_subtrees_are_preserved_including_escaped_bind_mount_paths(self):
        run = self.run_dir("contains space")
        mounted = str(run / "mounted").replace(" ", r"\040")
        mountinfo = f"36 25 0:32 / {mounted} rw - tmpfs tmpfs rw\n"
        from auto_moniter import retention
        original = Path.read_text

        def read(path, *args, **kwargs):
            return mountinfo if str(path) == "/proc/self/mountinfo" else original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            result = self.inspect(keep_runs=0, older_than=0, apply=True)
        self.assertEqual(result["runs"][0]["reason"], "mounted_tree")
        self.assertTrue(run.exists())

    def test_replacement_directory_with_identical_metadata_is_preserved(self):
        run = self.run_dir("old")
        from auto_moniter import retention
        remove = retention._remove

        def replaced(*args):
            saved = self.root / "saved-run"
            run.rename(saved)
            import shutil
            shutil.copytree(saved, run)
            (run / "important-note").write_text("keep replacement")
            return remove(*args)

        with patch("auto_moniter.retention._remove", side_effect=replaced):
            result = self.inspect(keep_runs=0, older_than=0, apply=True)
        self.assertEqual((run / "important-note").read_text(), "keep replacement")
        self.assertEqual(result["errors"][0]["error"], "run_directory_changed")

    def test_run_becoming_locked_after_scan_is_preserved(self):
        run = self.run_dir("old")
        from auto_moniter import retention
        remove = retention._remove

        def locked(*args):
            with (run / "supervisor.lock").open("r") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return remove(*args)

        with patch("auto_moniter.retention._remove", side_effect=locked):
            result = self.inspect(keep_runs=0, older_than=0, apply=True)
        self.assertTrue(run.exists())
        self.assertEqual(result["errors"][0]["error"], "active_lock")

    def test_disappearing_temporary_files_do_not_zero_storage_measurement(self):
        run = self.run_dir("old")
        (run / "temporary").touch()
        original = os.stat

        def disappearing(path, *args, **kwargs):
            if path == "temporary":
                raise FileNotFoundError("pruned")
            return original(path, *args, **kwargs)

        with patch("auto_moniter.retention.os.stat", side_effect=disappearing):
            result = self.inspect()
        self.assertGreaterEqual(result["total_bytes"], 1024)

    def test_cli_reports_deletion_errors_with_nonzero_exit_code(self):
        run = self.run_dir("old")
        with patch("auto_moniter.retention.shutil.rmtree", side_effect=PermissionError("read-only tree")), contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["cleanup", "-C", str(self.project), "--runs-dir", str(self.runs),
                         "--keep-runs", "0", "--older-than", "0", "--apply"])
        self.assertEqual(code, 1)
        self.assertTrue(run.exists())
        self.assertEqual(json.loads(out.getvalue())["errors"][0]["error"], "read-only tree")

    def test_missing_root_is_empty_and_not_created(self):
        target = self.root / "not-created"
        result = run_storage(self.project, runs_dir=target, apply=True)
        self.assertEqual(result["total_bytes"], 0)
        self.assertFalse(target.exists())

    def test_candidate_cannot_contain_project(self):
        run = self.run_dir("contains-project")
        nested_project = run / "project"
        nested_project.mkdir()
        write_json(run / "state.json", {"cwd": str(nested_project), "status": "done"})
        result = run_storage(nested_project, runs_dir=self.runs, keep_runs=0, older_than=0, apply=True)
        self.assertEqual(result["runs"][0]["reason"], "contains_project")
        self.assertTrue(nested_project.exists())

    def test_cli_and_real_monitor_managed_paths(self):
        with patch.dict(os.environ, {"AUTO_MONITER_STATE_DIR": str(self.root / "private-state")}):
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": str(self.project / "app.py")}])
            monitor = Supervisor(RunConfig("task", self.project, worker, no_review=True, max_rounds=1), log=lambda _: None)
            report = monitor.run()
            self.assertEqual(report.status, "done", report.summary)
            self.assertEqual(monitor.run_dir.parent, project_runs_dir(self.project))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["runs", "-C", str(self.project)]), 0)
            self.assertEqual(len(json.loads(out.getvalue())["runs"]), 1)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["cleanup", "-C", str(self.project), "--keep-runs", "0", "--older-than", "0"]), 0)
            self.assertTrue(json.loads(out.getvalue())["runs"][0]["selected"])
            self.assertTrue(monitor.run_dir.exists())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["cleanup", "-C", str(self.project), "--keep-runs", "0", "--older-than", "0", "--apply"]), 0)
            self.assertFalse(monitor.run_dir.exists())
            self.assertTrue((self.project / "app.py").exists())


if __name__ == "__main__":
    unittest.main()
