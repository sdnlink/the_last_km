import contextlib
import gzip
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.providers import PreparedCommand, resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from auto_moniter.workspace import file_snapshot, preserve_tree, restore_reviewer_writes, workspace_fingerprint
from tests.test_supervisor import ScriptedProvider, _verdict


class WorkspaceScopeTests(unittest.TestCase):
    def test_generated_files_are_excluded_but_tracked_sources_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("cache/\n*.generated\n")
            (root / "app.py").write_text("source")
            tracked = root / "tracked.generated"
            tracked.write_text("tracked source")
            subprocess.run(["git", "-C", str(root), "add", "-f", tracked.name], check=True)
            for directory in ["target", "nested/target", "cache"]:
                path = root / directory
                path.mkdir(parents=True)
                (path / "artifact").write_text("generated content")
            before = file_snapshot(root)
            self.assertEqual({Path(name).relative_to(root).as_posix() for name in before}, {"app.py", ".gitignore", "tracked.generated"})
            fingerprint = workspace_fingerprint(root)
            (root / "target/artifact").write_text("new build output")
            self.assertEqual(workspace_fingerprint(root), fingerprint)
            backup = preserve_tree(root, root / ".auto-moniter")
            self.assertEqual(len(backup.saved), 3)
            # A reviewer cannot hide an added file by changing ignore rules.
            (root / ".gitignore").write_text("hidden.py\n")
            (root / "hidden.py").write_text("reviewer write")
            tracked.write_text("reviewer edit")
            restored = restore_reviewer_writes(root, backup)
            self.assertIn("hidden.py", restored)
            self.assertFalse((root / "hidden.py").exists())
            self.assertEqual(tracked.read_text(), "tracked source")
            self.assertEqual((root / ".gitignore").read_text(), "cache/\n*.generated\n")

    def test_failed_restoration_keeps_the_original_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("worker content")
            backup = preserve_tree(root, root / ".auto-moniter")
            (root / "app.py").write_text("reviewer content")
            with patch("auto_moniter.workspace.shutil.copy2", side_effect=OSError("restore failed")):
                with self.assertRaisesRegex(RuntimeError, "Backup retained"):
                    restore_reviewer_writes(root, backup)
            self.assertTrue(backup.root.exists())
            self.assertEqual(backup.saved[str(root / "app.py")].read_text(), "worker content")
            backup.cleanup()


class RunLifecycleTests(unittest.TestCase):
    def test_legacy_baseline_migration_preserves_initial_file_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app.py"
            app.write_text("original")
            baseline = file_snapshot(root)
            app.write_text("implemented before interruption")
            run = root / "run"
            run.mkdir()
            (run / "requirement.md").write_text("task")
            (run / "state.json").write_text(json.dumps({"status": "running", "cwd": str(root), "baseline_files": baseline}))
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])}])
            report = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=run, resume=True), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            state = json.loads((run / "state.json").read_text())
            self.assertNotIn("baseline_files", state)
            with gzip.open(run / state["baseline_file"], "rt") as handle:
                self.assertEqual(json.load(handle), baseline)

    def test_corrupt_baseline_resume_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            (run / "requirement.md").write_text("task")
            (run / "state.json").write_text(json.dumps({"status": "running", "cwd": str(root), "baseline_file": "baseline.json.gz"}))
            (run / "baseline.json.gz").write_bytes(b"invalid gzip")
            (run / "report.json").write_text("existing report")
            worker = ScriptedProvider("worker", [])
            report = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=run, resume=True), log=lambda _: None).run()
            self.assertEqual(report.status, "invalid")
            self.assertEqual((run / "report.json").read_text(), "existing report")
            self.assertEqual(worker.calls, [])

    def test_unfinished_worker_continues_before_review_or_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", ["integration tests pending"]), "touch": "app.py"},
                {"verdict": _verdict("continue", ["edge case pending"])},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])}])
            with patch("auto_moniter.supervisor.preserve_tree", wraps=preserve_tree) as backup:
                report = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertEqual([r.index for r in report.records if r.role == "reviewer"], [3])
            self.assertEqual(backup.call_count, 1)
            self.assertIn("integration tests pending", worker.calls[1]["prompt"])
            self.assertIn("edge case pending", worker.calls[2]["prompt"])

    def test_restore_error_finishes_run_with_a_diagnostic_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])}])
            with patch("auto_moniter.supervisor.restore_reviewer_writes", side_effect=OSError("workspace restore failed")):
                report = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "failed")
            self.assertIn("workspace restore failed", report.summary)
            self.assertEqual(json.loads((root / "run/state.json").read_text())["status"], "failed")
            self.assertEqual(json.loads((root / "run/report.json").read_text())["status"], "failed")

    def test_baseline_is_compressed_once_and_state_stays_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(300):
                (root / f"source-{i}.txt").write_text("original")
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", ["tests pending"]), "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            run = root / "run"
            supervisor = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=run, retry_delay_s=0), log=lambda _: None)
            baseline_mtime = []
            def observe(message):
                if message.startswith("round ") and message.endswith("worker running"):
                    baseline_mtime.append((run / "baseline.json.gz").stat().st_mtime_ns)
            supervisor.log = observe
            self.assertEqual(supervisor.run().status, "done")
            state = json.loads((run / "state.json").read_text())
            self.assertNotIn("baseline_files", state)
            self.assertEqual(state["baseline_count"], 300)
            self.assertEqual(len(set(baseline_mtime)), 1)
            self.assertLess((run / "state.json").stat().st_size, 8000)
            with gzip.open(run / state["baseline_file"], "rt") as handle:
                self.assertEqual(len(json.load(handle)), 300)


class UsefulOutputTests(unittest.TestCase):
    def test_watch_legacy_run_omits_baseline_and_old_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {"status": "running", "next_round": 2, "baseline_files": {"irrelevant-build-artifact": "fingerprint"}, "rounds": [{"role": "worker", "summary": "old history"}], "last_worker": {"status": "continue", "summary": "integration checks pending"}}
            (root / "state.json").write_text(json.dumps(state))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["watch", str(root), "--once"]), 0)
            self.assertIn("integration checks pending", output.getvalue())
            self.assertNotIn("baseline_files", output.getvalue())
            self.assertNotIn("irrelevant-build-artifact", output.getvalue())
            self.assertNotIn("old history", output.getvalue())

    def test_large_fragmented_tool_output_cannot_request_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0, "aggregated_output": "x" * 20000 + " waiting for input " + "y" * 100}})
            split = len(event) - 50
            code = f"import sys,time; sys.stdout.write({event[:split]!r}); sys.stdout.flush(); time.sleep(0.4); print({event[split:]!r}, flush=True); time.sleep(0.4); print('finished', flush=True)"
            result = run_command(PreparedCommand([sys.executable, "-c", code]), Path(tmp), input_grace_s=0.05)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.interruption, "")
            self.assertIn("finished", result.stdout)

    def test_real_assistant_question_still_interrupts(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Would you like me to proceed?"}})
            code = f"import time; print({event!r}, flush=True); time.sleep(20)"
            result = run_command(PreparedCommand([sys.executable, "-c", code]), Path(tmp), input_grace_s=0.05)
            self.assertEqual(result.interruption, "input request")
            self.assertNotEqual(result.exit_code, 0)

    def test_logs_keep_conclusion_and_failed_checks_without_success_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            success = {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0, "command": "cat source.rs", "aggregated_output": "unrelated source listing\n" * 10000}}
            failure = {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 101, "command": "cargo test", "aggregated_output": "test failed: important assertion"}}
            conclusion = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_verdict("done", evidence=["app.py"]))}}
            events = [json.dumps(event) for event in [success, failure, conclusion]]
            code = f"from pathlib import Path; Path('app.py').write_text('deliverable'); print({chr(10).join(events)!r})"
            script = root / "agent.py"
            script.write_text(code)
            provider = resolve_provider("command", command=shlex.join([sys.executable, str(script)]))
            report = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=root / "run", failure_limit=1), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            log = (root / "run/001-worker.log").read_text()
            self.assertNotIn("unrelated source listing", log)
            self.assertNotIn("cat source.rs", log)
            self.assertIn("cargo test", log)
            self.assertIn("important assertion", log)
            self.assertIn('"status": "done"', log)
            self.assertIn("interruption=-", log)
            self.assertLess(len(log.encode()), 4000)


if __name__ == "__main__":
    unittest.main()
