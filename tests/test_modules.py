import copy
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.modules import ModuleLedger, definitions
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.verdict import parse_verdict
from tests.test_supervisor import ScriptedProvider, _verdict


PLAN = [
    {"id": "A", "title": "Module A", "acceptance": ["A works", "A failure path passes"]},
    {"id": "B", "title": "Module B", "acceptance": ["B works with A"]},
]


def outcome(ids, scope="module", plan=None):
    value = _verdict("done", scope=scope, evidence=["app.py"])
    value.update(module_ids=ids, modules=copy.deepcopy(plan or []))
    return value


class ModuleTests(unittest.TestCase):
    def test_definitions_are_stable_and_additions_are_atomic(self):
        ledger = ModuleLedger(PLAN)
        changed = copy.deepcopy(PLAN[0])
        changed["acceptance"] = ["weaker check"]
        with self.assertRaisesRegex(ValueError, "cannot change"):
            ledger.merge([{"id": "C", "title": "C", "acceptance": ["C works"]}, changed])
        self.assertEqual(set(ledger.items), {"A", "B"})
        ledger.merge([])
        self.assertEqual(set(ledger.items), {"A", "B"})

    def test_invalid_definitions_do_not_enter_ledger(self):
        for value in (None, {}, [{"id": "A"}], [PLAN[0], PLAN[0]], [{**PLAN[0], "acceptance": []}], [{**PLAN[0], "id": "bad id"}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                definitions(value)

    def test_joint_coverage_and_evidence_survive_restore(self):
        ledger = ModuleLedger(PLAN)
        worker = parse_verdict(json.dumps(outcome(["A"])))
        reviewer = parse_verdict(json.dumps(outcome(["A"])))
        self.assertEqual(ledger.coverage(worker, reviewer), [])
        ledger.accept(worker, reviewer, 1)
        restored = ModuleLedger.restore(ledger.export())
        self.assertEqual(restored.items["A"]["status"], "accepted")
        self.assertEqual(restored.items["A"]["evidence"], ["app.py"])
        self.assertEqual(restored.items["B"]["status"], "pending")
        self.assertTrue(restored.coverage(worker, reviewer))
        final_worker = parse_verdict(json.dumps(outcome(["A", "B"], "requirement")))
        final_review = parse_verdict(json.dumps(outcome(["A"], "requirement")))
        self.assertIn("B", " ".join(restored.coverage(final_worker, final_review)))
        final_review = parse_verdict(json.dumps(outcome(["A", "B"], "requirement")))
        restored.accept(final_worker, final_review, 2)
        self.assertEqual(restored.items["A"]["round"], 1)
        self.assertEqual(restored.items["B"]["round"], 2)

    def test_unknown_and_missing_ids_cannot_complete(self):
        for ids in ([], ["unknown"], ["A", "A"]):
            ledger = ModuleLedger(PLAN)
            value = parse_verdict(json.dumps(outcome(ids)))
            self.assertTrue(ledger.coverage(value))

    def test_monitor_does_not_finish_when_both_agents_omit_module_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": outcome(["A"], "requirement", PLAN), "touch": "app.py"},
                {"verdict": outcome(["A", "B"], "requirement")},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": outcome(["A"], "requirement")},
                {"verdict": outcome(["A", "B"], "requirement")},
            ])
            result = Supervisor(RunConfig("A and B", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 2)
            self.assertIn("unaccepted modules: B", worker.calls[1]["prompt"])
            report = json.loads((root / "run/report.json").read_text())
            self.assertEqual([item["status"] for item in report["modules"]], ["accepted", "accepted"])

    def test_module_ledger_and_evidence_survive_module_switch_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": outcome(["A"], plan=PLAN), "touch": "app.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": outcome(["A"])}])
            first = Supervisor(RunConfig("A and B", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None)
            first.log = lambda message: first.request_stop() if "module accepted;" in message else None
            self.assertEqual(first.run().status, "stopped")
            state = json.loads((root / "run/state.json").read_text())
            self.assertEqual([item["status"] for item in state["modules"]], ["accepted", "pending"])
            worker = ScriptedProvider("worker", [{"verdict": outcome(["B"], "requirement")}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": outcome(["B"], "requirement")}])
            result = Supervisor(RunConfig("A and B", root, worker, reviewer=reviewer, run_dir=root / "run", resume=True, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertIn("B works with A", worker.calls[0]["prompt"])

    def test_claiming_done_without_plan_returns_to_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": outcome([], "requirement"), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, max_rounds=1, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertIn("Declare the full original requirement", " ".join(result.records[0].verdict.gaps))

    def test_predefined_plan_rejects_weakened_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            changed = copy.deepcopy(PLAN)
            changed[0]["acceptance"] = ["skip failure paths"]
            worker = ScriptedProvider("worker", [{"verdict": outcome(["A", "B"], "requirement", changed), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, modules=PLAN, no_review=True, max_rounds=1, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertIn("definition cannot change", " ".join(result.records[0].verdict.gaps))
            saved = json.loads((root / "run/state.json").read_text())
            self.assertEqual(saved["modules"][0]["acceptance"], PLAN[0]["acceptance"])

    def test_resume_cannot_replace_saved_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), modules=PLAN, no_review=True, run_dir=root / "run"), log=lambda _: None)
            first.request_stop()
            self.assertEqual(first.run().status, "stopped")
            changed = copy.deepcopy(PLAN)
            changed[0]["acceptance"] = ["weaker"]
            worker = ScriptedProvider("worker", [])
            result = Supervisor(RunConfig("task", root, worker, modules=changed, no_review=True, run_dir=root / "run", resume=True), log=lambda _: None).run()
            self.assertEqual(result.status, "invalid")
            self.assertEqual(worker.calls, [])

    def test_module_definitions_have_total_size_limit(self):
        large = [{"id": f"m{index}", "title": "title", "acceptance": ["x" * 2000] * 30} for index in range(3)]
        with self.assertRaisesRegex(ValueError, "128 KiB"):
            ModuleLedger(large)

    def test_resume_accepts_original_plan_when_new_modules_were_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), modules=PLAN[:1], no_review=True, run_dir=root / "run"), log=lambda _: None)
            first.modules.merge(PLAN[1:])
            first.request_stop()
            self.assertEqual(first.run().status, "stopped")
            worker = ScriptedProvider("worker", [{"verdict": outcome(["A", "B"], "requirement"), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, modules=PLAN[:1], no_review=True, run_dir=root / "run", resume=True), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(len(json.loads((root / "run/state.json").read_text())["modules"]), 2)


if __name__ == "__main__":
    unittest.main()
