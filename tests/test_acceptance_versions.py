import copy
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from auto_moniter.modules import ModuleLedger
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import workspace_fingerprint
from tests.test_modules import PLAN, outcome
from tests.test_supervisor import ScriptedProvider, _verdict


class AcceptanceVersionTests(unittest.TestCase):
    def test_changes_after_review_conclusion_are_rejected_before_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])}])
            def observe(message):
                if "reviewer status=done" in message:
                    (root / "app.py").write_text("changed after conclusion")
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, checks=["touch .auto-moniter/check-ran"], run_dir=root / "run", max_rounds=1, retry_delay_s=0), log=observe).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertFalse((root / ".auto-moniter/check-ran").exists())
            self.assertIn("acceptance evidence is stale", " ".join(result.records[-1].verdict.gaps))

    def test_successful_check_modifying_source_requires_another_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(2)])
            command = shlex.join([sys.executable, "-c", "from pathlib import Path; Path('app.py').write_text('modified by check')"])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, checks=[command], run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 2)
            self.assertIn("Previous review and evidence are stale", worker.calls[1]["prompt"])
            report = json.loads((root / "run/report.json").read_text())
            self.assertEqual(report["accepted_tree"], workspace_fingerprint(root, ignore_dir=root / "run"))
            self.assertEqual(report["modules"][0]["tree"], report["accepted_tree"])

    def test_later_module_source_changes_require_previously_accepted_module_revalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": outcome(["A"], plan=PLAN), "touch": "app.py"},
                {"verdict": outcome(["B"], "requirement"), "touch": "b.py"},
                {"verdict": outcome(["A", "B"], "requirement")},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": outcome(["A"])}, {"verdict": outcome(["B"], "requirement")},
                {"verdict": outcome(["A", "B"], "requirement")},
            ])
            result = Supervisor(RunConfig("A and B", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 3)
            self.assertIn("unaccepted modules: A", worker.calls[2]["prompt"])
            state = json.loads((root / "run/state.json").read_text())
            self.assertTrue(all(item["status"] == "accepted" for item in state["modules"]))
            events = [json.loads(line) for line in (root / "run/events.jsonl").read_text().splitlines()]
            self.assertTrue(any(event["event"] == "modules_invalidated" for event in events))

    def test_dependency_cycles_unknown_dependencies_and_definition_changes_are_rejected(self):
        for plan in ([{**PLAN[0], "depends_on": ["missing"]}],
                     [{**PLAN[0], "depends_on": ["B"]}, {**PLAN[1], "depends_on": ["A"]}]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                ModuleLedger(plan)
        ledger = ModuleLedger([PLAN[0], {**PLAN[1], "depends_on": ["A"]}])
        from auto_moniter.verdict import parse_verdict
        value = parse_verdict(json.dumps(outcome(["B"])))
        self.assertIn("dependencies: A", " ".join(ledger.coverage(value)))
        with self.assertRaisesRegex(ValueError, "cannot change"):
            ledger.merge([{**PLAN[1], "depends_on": []}])
        reversed_plan = [{**PLAN[1], "depends_on": ["A"], "checks": ["check-b"]}, {**PLAN[0], "checks": ["check-a"]}]
        ledger = ModuleLedger(reversed_plan)
        self.assertEqual(ledger.checks_for({"B"}), ["check-a", "check-b"])
        restored = ModuleLedger.restore(ledger.export())
        self.assertEqual(restored.checks_for({"B"}), ["check-a", "check-b"])

    def test_user_module_command_executes_before_module_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = [{**PLAN[0], "checks": ["test -f app.py"]}, {**PLAN[1], "depends_on": ["A"]}]
            worker = ScriptedProvider("worker", [
                {"verdict": outcome(["A"]), "touch": "app.py"},
                {"verdict": outcome(["B"], "requirement")},
            ])
            result = Supervisor(RunConfig("task", root, worker, modules=plan, no_review=True, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertIn("exit=0", (root / "run/001-check-01.log").read_text())
            self.assertIn("exit=0", (root / "run/002-check-01.log").read_text())

    def test_failed_module_command_blocks_module_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": outcome(["A"]), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, modules=[{**PLAN[0], "checks": ["exit 9"]}], no_review=True, run_dir=root / "run", max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertEqual(json.loads((root / "run/state.json").read_text())["modules"][0]["status"], "pending")

    def test_agent_cannot_supply_executable_module_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": outcome(["A"], plan=[{**PLAN[0], "checks": ["touch injected"]}]), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertFalse((root / "injected").exists())
            self.assertIn("must be supplied by the user", " ".join(result.records[0].verdict.gaps))


if __name__ == "__main__":
    unittest.main()
