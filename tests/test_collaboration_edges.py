import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.audit import AuditLedger, MAX_OPEN
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_audit_issues import resolution, reviewed
from tests.test_modules import PLAN, outcome
from tests.test_supervisor import ScriptedProvider, _verdict


class CollaborationEdgeTests(unittest.TestCase):
    def test_module_checks_and_milestones_use_only_jointly_accepted_ids(self):
        for worker_ids, reviewer_ids in [(["A"], ["A", "B"]), (["A", "B"], ["A"])]:
            with self.subTest(worker=worker_ids, reviewer=reviewer_ids), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan = [{**PLAN[0], "checks": ["true"]},
                        {**PLAN[1], "depends_on": ["A"], "checks": ["touch .auto-moniter/b-check-ran; exit 9"]}]
                worker = ScriptedProvider("worker", [{"verdict": outcome(worker_ids), "touch": "app.py"}])
                reviewer = ScriptedProvider("reviewer", [{"verdict": outcome(reviewer_ids)}])
                result = Supervisor(RunConfig("A and B", root, worker, reviewer=reviewer, modules=plan,
                                              max_rounds=1, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
                self.assertEqual(result.status, "max_rounds")
                state = json.loads((root / "run/state.json").read_text())
                self.assertEqual([item["status"] for item in state["modules"]], ["accepted", "pending"])
                self.assertFalse((root / ".auto-moniter/b-check-ran").exists())
                review_record = next(item for item in state["rounds"] if item["role"] == "reviewer")
                self.assertEqual(review_record["module_ids"], ["A"])
                events = [json.loads(line) for line in (root / "run/events.jsonl").read_text().splitlines()]
                accepted = [item for item in events if item["event"] == "module_accepted"]
                self.assertEqual(accepted[0]["modules"], "['A']")

    def test_full_audit_ledger_can_close_existing_issues_and_add_new_ones_in_one_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("changed")
            monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=root / "run"), log=lambda _: None)
            monitor.run_dir.mkdir()
            monitor._baseline_files = {}
            monitor.audit.record([f"existing-{index}" for index in range(MAX_OPEN)], 1)
            from auto_moniter.verdict import parse_verdict
            worker = parse_verdict(json.dumps(_verdict("done", evidence=["app.py"])))
            review = parse_verdict(json.dumps(reviewed("continue", ["newly discovered issue"], ["AUD-1"])))
            monitor._record_audit(review, worker, 2, "a" * 64)
            self.assertEqual(len(monitor.audit.pending()), MAX_OPEN)
            self.assertIn(f"AUD-{MAX_OPEN + 1}", {item["id"] for item in monitor.audit.pending()})
            self.assertNotIn("AUD-1", {item["id"] for item in monitor.audit.pending()})
            self.assertEqual(AuditLedger.restore(monitor.audit.export()).export(), monitor.audit.export())

    def test_closing_audit_issues_counts_as_progress_without_source_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                *[{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(4)],
            ])
            for script in worker.scripts:
                script["session_id"] = "11111111-1111-1111-1111-111111111111"
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": reviewed("continue", [f"issue-{number}" for number in range(4)])},
                *[{"verdict": reviewed("continue", ids=[f"AUD-{number}"])} for number in range(1, 4)],
                {"verdict": reviewed(ids=["AUD-4"])},
            ])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, max_rounds=5,
                                           retry_delay_s=0, restart_after_stall=3, run_dir=root / "run"), log=lambda _: None)
            with patch.object(monitor, "_close_delivered_sessions", return_value=[]) as close:
                result = monitor.run()
            self.assertEqual(result.status, "done")
            self.assertEqual(close.call_count, 1)
            self.assertEqual(monitor._recovery_count, 0)
            self.assertTrue(worker.calls[4]["session_id"])

    def test_numbered_followup_keeps_original_scope_and_description(self):
        ledger = AuditLedger()
        ledger.record(["original missing rejection case"], 1, ["A"])
        ledger.record(["AUD-1: rejection assertion still fails"], 2)
        ledger.record(["AUD-1：拒绝用例仍然失败"], 3)
        self.assertEqual(len(ledger.pending()), 1)
        self.assertEqual(ledger.pending()[0]["summary"], "original missing rejection case")
        self.assertEqual(ledger.pending()[0]["module_ids"], ["A"])
        self.assertEqual(ledger.pending()[0]["last_seen_round"], 3)

    def test_unknown_numbered_followup_is_rejected_without_assigning_another_id(self):
        ledger = AuditLedger()
        ledger.record(["original issue"], 1)
        before = ledger.export()
        with self.assertRaisesRegex(ValueError, "Unknown audit issue"):
            ledger.record(["AUD-999: still broken"], 2)
        self.assertEqual(ledger.export(), before)

    def test_numbered_followup_cannot_close_same_issue_in_the_same_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("changed")
            monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=root / "run"), log=lambda _: None)
            monitor.run_dir.mkdir()
            monitor._baseline_files = {}
            monitor.audit.record(["missing rejection case"], 1, ["A"])
            from auto_moniter.verdict import parse_verdict
            worker = parse_verdict(json.dumps(_verdict("done", evidence=["app.py"])))
            review = parse_verdict(json.dumps(reviewed("continue", ["AUD-1: assertion still fails"], ["AUD-1"])))
            with self.assertRaisesRegex(ValueError, "cannot be closed"):
                monitor._record_audit(review, worker, 2, "a" * 64)
            self.assertEqual(len(monitor.audit.pending()), 1)

    def test_repeating_an_already_resolved_id_is_not_new_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("changed")
            monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=root / "run"), log=lambda _: None)
            monitor.run_dir.mkdir()
            monitor._baseline_files = {}
            monitor.audit.record(["issue"], 1)
            monitor.audit.resolve([resolution()], 2, "a" * 64, lambda _: [])
            from auto_moniter.verdict import parse_verdict
            worker = parse_verdict(json.dumps(_verdict("done", evidence=["app.py"])))
            review = parse_verdict(json.dumps(reviewed("continue", ids=["AUD-1"])))
            monitor._record_audit(review, worker, 3, "a" * 64)
            self.assertFalse(monitor._progress_reset)

    def test_issue_progress_resets_repeated_gap_counter_as_well_as_no_progress_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=root / "run"), log=lambda _: None)
            monitor._no_progress = 2
            monitor._progress_reset = True
            from auto_moniter.verdict import parse_verdict
            verdict = parse_verdict(json.dumps(reviewed("continue", ["same remaining issue"])))
            with patch.object(monitor, "_close_delivered_sessions", return_value=[]) as close:
                stall, _, _ = monitor._track_stall(2, "same remaining issue", "a" * 64, verdict, "a" * 64)
            self.assertEqual(stall, 0)
            self.assertEqual(monitor._no_progress, 0)
            close.assert_not_called()

    def test_unconsumed_audit_progress_survives_stop_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": reviewed("continue", ["one", "two"])},
                {"verdict": reviewed("continue", ids=["AUD-1"])},
            ])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer,
                                           run_dir=run, retry_delay_s=0), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if message.startswith("round 2: reviewer status=") else None
            self.assertEqual(monitor.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            self.assertTrue(state["progress_pending"])
            state.update(no_progress_turns=2, stall=2, gap_key="same remaining issue")
            (run / "state.json").write_text(json.dumps(state))
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue", ["same remaining issue"])}])
            reviewer = ScriptedProvider("reviewer", [])
            resumed = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                           max_rounds=3, run_dir=run, retry_delay_s=0), log=lambda _: None)
            result = resumed.run()
            self.assertEqual(result.status, "max_rounds")
            self.assertEqual(resumed._no_progress, 0)
            self.assertEqual(resumed._recovery_count, 0)
            self.assertEqual(reviewer.calls, [])

    def test_failed_batch_preserves_new_findings_and_does_not_partially_close_old_issues(self):
        ledger = AuditLedger()
        ledger.record(["one", "two"], 1)
        with self.assertRaisesRegex(ValueError, "Unknown audit issue"):
            ledger.update(["new issue"], [resolution(), resolution("AUD-999")], 2, "a" * 64, [], lambda _: [])
        self.assertEqual([item["id"] for item in ledger.pending()], ["AUD-1", "AUD-2", "AUD-3"])
        self.assertEqual(AuditLedger.restore(ledger.export()).export(), ledger.export())


if __name__ == "__main__":
    unittest.main()
