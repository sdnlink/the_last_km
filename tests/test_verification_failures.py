import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.guardian import Guardian
from auto_moniter.supervisor import RunConfig
from auto_moniter.workspace import acceptance_problems, file_snapshot
from tests.test_supervisor import ScriptedProvider


class VerificationFailureTests(unittest.TestCase):
    def test_git_failures_do_not_disable_diff_verification(self):
        for mode in ("exit", "timeout", "missing"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".git").mkdir()
                (root / "app.py").write_text("changed")
                failure = {"return_value": subprocess.CompletedProcess([], 128, b"", b"corrupt repository")}
                if mode == "timeout":
                    failure = {"side_effect": subprocess.TimeoutExpired("git", 10)}
                elif mode == "missing":
                    failure = {"side_effect": FileNotFoundError("git missing")}
                with patch("auto_moniter.workspace.subprocess.run", **failure):
                    problems = acceptance_problems(root, ["app.py"], baseline={})
                self.assertTrue(problems)
                self.assertIn("Cannot verify Git", " ".join(problems))

    def test_plain_project_still_accepts_evidence_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("changed")
            with patch("auto_moniter.workspace.subprocess.run", side_effect=FileNotFoundError("git missing")):
                self.assertEqual(acceptance_problems(root, ["app.py"], baseline={}), [])

    def test_git_failure_in_repository_subdirectory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("changed")
            with patch("auto_moniter.workspace.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
                self.assertTrue(acceptance_problems(project, ["app.py"], baseline={}))

    def test_unrelated_empty_git_directory_does_not_turn_plain_project_into_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("changed")
            with patch("auto_moniter.workspace.subprocess.run", side_effect=FileNotFoundError("git missing")):
                self.assertEqual(acceptance_problems(project, ["app.py"], baseline={}), [])

    def test_git_rename_old_path_is_valid_removed_baseline_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / "old name.py").write_text("original implementation")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "initial"], cwd=root, check=True, capture_output=True)
            baseline = file_snapshot(root)
            subprocess.run(["git", "mv", "old name.py", "new name.py"], cwd=root, check=True, capture_output=True)
            self.assertEqual(acceptance_problems(root, ["old name.py"], baseline=baseline), [])
            self.assertEqual(acceptance_problems(root, ["new name.py"], baseline=baseline), [])

    def test_guardian_requires_matching_state_before_using_an_orphan_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            guardian = Guardian(RunConfig("task", root, ScriptedProvider("worker", []), no_review=True, run_dir=run))
            report = {"status": "done", "rounds": 1, "summary": "delivered", "stop_reason": "done"}
            (run / "report.json").write_text(json.dumps(report))
            for content in (None, "{", json.dumps({"status": "running"}), json.dumps({"status": "failed"})):
                with self.subTest(content=content):
                    if content is not None:
                        (run / "state.json").write_text(content)
                    self.assertIsNone(guardian._new_report(0))
            (run / "state.json").write_text(json.dumps({"status": "done"}))
            self.assertEqual(guardian._new_report(0), report)
            self.assertIsNone(guardian._new_report((run / "report.json").stat().st_mtime_ns + 1))
            (run / "process.json").write_text("{}")
            self.assertIsNone(guardian._new_report(0))
            (run / "process.json").unlink()
            for field, value in (("rounds", -1), ("rounds", "1"), ("summary", []), ("stop_reason", None)):
                (run / "report.json").write_text(json.dumps({**report, field: value}))
                self.assertIsNone(guardian._new_report(0))
            for status in ("running", "unknown", None, []):
                (run / "state.json").write_text(json.dumps({"status": status}))
                (run / "report.json").write_text(json.dumps({**report, "status": status}))
                self.assertIsNone(guardian._new_report(0))


if __name__ == "__main__":
    unittest.main()
