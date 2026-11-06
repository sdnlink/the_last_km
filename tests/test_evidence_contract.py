import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import acceptance_problems, file_snapshot
from tests.test_supervisor import ScriptedProvider, _verdict


class EvidenceContractTests(unittest.TestCase):
    def test_deleted_baseline_file_is_evidence_in_git_and_plain_projects(self):
        for git in (False, True):
            with self.subTest(git=git), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "obsolete.py"
                target.write_text("old implementation")
                if git:
                    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
                    subprocess.run(["git", "add", "obsolete.py"], cwd=root, check=True, capture_output=True)
                baseline = file_snapshot(root)
                target.unlink()
                self.assertEqual(acceptance_problems(root, ["obsolete.py"], baseline=baseline), [])

    def test_missing_unknown_outside_link_and_directory_replacements_are_not_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "old.py").write_text("old")
            (project / "broken.py").write_text("old")
            outside = root / "outside.py"
            outside.write_text("outside")
            (project / "parent").mkdir()
            (project / "parent/nested.py").write_text("old")
            baseline = file_snapshot(project)
            (project / "old.py").unlink()
            (project / "old.py").mkdir()
            (project / "broken.py").unlink()
            (project / "broken.py").symlink_to("missing.py")
            (project / "parent/nested.py").unlink()
            (project / "parent").rmdir()
            (project / "parent").symlink_to(root, target_is_directory=True)
            for evidence in ("never-existed.py", "../outside.py", "old.py", "broken.py", "parent/nested.py"):
                with self.subTest(evidence=evidence):
                    self.assertTrue(acceptance_problems(project, [evidence], baseline=baseline))

    def test_pure_deletion_can_be_delivered_after_both_agents_confirm_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "obsolete.py"
            target.write_text("remove this file")
            verdict = _verdict("done", evidence=["obsolete.py"])
            code = "from pathlib import Path; import json; Path('obsolete.py').unlink(); print(json.dumps(" + repr(verdict) + "))"
            worker = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            reviewer = ScriptedProvider("reviewer", [{"verdict": verdict}])
            result = Supervisor(RunConfig("Remove obsolete.py", root, worker, reviewer=reviewer,
                                          checks=["test ! -e obsolete.py"], max_rounds=1,
                                          retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertFalse(target.exists())
            self.assertEqual(len(reviewer.calls), 1)

    def test_failed_agent_plan_cannot_freeze_module_definitions(self):
        for role in ("worker", "reviewer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                broken = _verdict("done", evidence=["app.py"])
                broken["modules"][0]["acceptance"] = ["incorrect partial plan from a failed turn"]
                if role == "reviewer":
                    broken["modules"][0]["id"] = "phantom"
                valid = _verdict("done", evidence=["app.py"])
                worker_turns = ([{"verdict": broken, "exit_code": 1, "touch": "app.py"}, {"verdict": valid}]
                                if role == "worker" else [{"verdict": _verdict("continue"), "touch": "app.py"},
                                                          {"verdict": valid}])
                review_turns = ([{"verdict": valid}] if role == "worker" else
                                [{"verdict": broken, "exit_code": 1}, {"verdict": valid}])
                worker = ScriptedProvider("worker", worker_turns)
                reviewer = ScriptedProvider("reviewer", review_turns)
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_every=1,
                                              max_rounds=2, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
                self.assertEqual(result.status, "done")
                state = json.loads((root / "run/state.json").read_text())
                self.assertEqual(state["modules"][0]["acceptance"], valid["modules"][0]["acceptance"])
                self.assertEqual([item["id"] for item in state["modules"]], ["main"])

    def test_deleted_file_can_close_a_previously_reported_audit_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "obsolete.py").write_text("old implementation")
            pending = _verdict("continue", ["remove obsolete implementation"])
            complete = _verdict("done", evidence=["obsolete.py"])
            code = ("from pathlib import Path; import json; marker=Path('.auto-moniter/attempt'); "
                    "first=not marker.exists(); marker.touch(); "
                    "Path('obsolete.py').unlink() if not first else None; "
                    f"print(json.dumps({pending!r} if first else {complete!r}))")
            worker = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue", ["obsolete file still present"])},
                {"verdict": _verdict("done", evidence=["obsolete.py"], closed=["AUD-1"])},
            ])
            result = Supervisor(RunConfig("Remove obsolete.py", root, worker, reviewer=reviewer,
                                          max_rounds=2, review_every=1, retry_delay_s=0,
                                          run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            report = json.loads((root / "run/report.json").read_text())
            self.assertEqual(report["audit_issues"]["items"][0]["status"], "resolved")

    def test_malformed_completion_does_not_register_its_partial_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = {"status": "done", "modules": [{"id": "main", "title": "wrong", "acceptance": ["wrong"]}]}
            valid = _verdict("done", evidence=["app.py"])
            worker = ScriptedProvider("worker", [{"verdict": broken, "touch": "app.py"}, {"verdict": valid}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": valid}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, max_rounds=2,
                                          retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "done")


if __name__ == "__main__":
    unittest.main()
