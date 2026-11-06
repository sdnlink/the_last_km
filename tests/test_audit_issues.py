import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.audit import AuditCapacityError, AuditLedger, KEEP_CLOSED, MAX_BYTES, MAX_OPEN
from auto_moniter.health import run_health
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.verdict import parse_verdict
from tests.test_modules import PLAN, outcome
from tests.test_supervisor import ScriptedProvider, _verdict


TREE = "a" * 64


def resolution(ident="AUD-1", evidence=None):
    return {"id": ident, "summary": "Independently inspected the corrected behavior and test evidence",
            "evidence": ["app.py"] if evidence is None else evidence}


def reviewed(status="done", gaps=None, ids=()):
    return {**_verdict(status, gaps=gaps, evidence=["app.py"]),
            "issue_resolutions": [resolution(ident) for ident in ids]}


class AuditLedgerTests(unittest.TestCase):
    def test_omission_and_dedup_preserve_stable_issue_ids(self):
        ledger = AuditLedger()
        self.assertEqual(ledger.record(["Missing test", "Missing route"], 1), ["AUD-1", "AUD-2"])
        ledger.record([" missing   TEST "], 2)
        self.assertEqual(len(ledger.pending()), 2)
        self.assertEqual(ledger.items[0]["last_seen_round"], 2)
        ledger.record([], 3)
        self.assertEqual([item["id"] for item in ledger.pending()], ["AUD-1", "AUD-2"])

    def test_unknown_duplicate_and_invalid_evidence_closures_are_atomic(self):
        for proposals in ([resolution(), resolution("AUD-99")], [resolution(), resolution()],
                          [resolution(), resolution("AUD-2", ["nonexistent"])]):
            ledger = AuditLedger()
            ledger.record(["one", "two"], 1)
            before = ledger.export()
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                ledger.resolve(proposals, 2, TREE, lambda evidence: ["invalid"] if evidence == ["nonexistent"] else [])
            self.assertEqual(ledger.export(), before)

    def test_closure_reopens_after_source_change_and_restore_preserves_history(self):
        ledger = AuditLedger()
        ledger.record(["missing check"], 1, ["A"])
        ledger.resolve([resolution()], 2, TREE, lambda _: [])
        restored = AuditLedger.restore(ledger.export())
        self.assertFalse(restored.pending())
        self.assertEqual(restored.invalidate(TREE), [])
        self.assertEqual(restored.invalidate("b" * 64), ["AUD-1"])
        self.assertEqual(restored.pending()[0]["summary"], "missing check")
        self.assertIsNone(restored.items[0]["resolution"])
        self.assertEqual(AuditLedger.restore(restored.export()).export(), restored.export())

    def test_module_scope_keeps_global_findings_and_blocks_only_relevant_modules(self):
        ledger = AuditLedger()
        ledger.record(["A issue"], 1, ["A"])
        ledger.record(["B issue"], 1, ["B"])
        ledger.record(["integration issue"], 2)
        self.assertEqual({item["id"] for item in ledger.pending({"A"})}, {"AUD-1", "AUD-3"})
        self.assertEqual(len(ledger.pending()), 3)

    def test_retention_never_drops_open_issues_and_capacity_failure_is_atomic(self):
        ledger = AuditLedger()
        ledger.record([f"open-{index}" for index in range(MAX_OPEN)], 1)
        before = ledger.export()
        with self.assertRaises(AuditCapacityError):
            ledger.record(["overflow"], 2)
        self.assertEqual(ledger.export(), before)
        for number in range(1, MAX_OPEN + 1):
            ledger.resolve([resolution(f"AUD-{number}")], 2, TREE, lambda _: [])
        self.assertEqual(len(ledger.items), KEEP_CLOSED)
        self.assertEqual(ledger.pruned_closed, MAX_OPEN - KEEP_CLOSED)
        self.assertLess(len(json.dumps(ledger.export()).encode()), MAX_BYTES)
        self.assertEqual(AuditLedger.restore(ledger.export()).export(), ledger.export())
        ledger.record(["new"], 3)
        self.assertEqual(ledger.pending()[0]["id"], f"AUD-{MAX_OPEN + 1}")

    def test_corrupt_saved_ledgers_are_refused(self):
        ledger = AuditLedger()
        ledger.record(["issue"], 1)
        valid = ledger.export()
        variants = [None, {}, {**valid, "next_id": 1}, {**valid, "pruned_closed": -1}]
        for field, value in [("status", "unknown"), ("resolution", {}), ("last_seen_round", -1),
                             ("summary", ""), ("module_ids", [42]), ("tree", TREE)]:
            variant = copy.deepcopy(valid)
            variant["items"][0][field] = value
            variants.append(variant)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                AuditLedger.restore(variant)

    def test_closure_retention_leaves_room_to_reopen_without_exceeding_capacity(self):
        ledger = AuditLedger()
        ledger.record([f"original-{number}" for number in range(MAX_OPEN)], 1)
        ledger.resolve([resolution(f"AUD-{number}") for number in range(1, KEEP_CLOSED + 1)], 2, TREE, lambda _: [])
        ledger.record([f"new-{number}" for number in range(KEEP_CLOSED)], 3)
        self.assertEqual(len(ledger.items), MAX_OPEN)
        self.assertEqual(ledger.pruned_closed, KEEP_CLOSED)
        self.assertEqual(len(ledger.invalidate("b" * 64)), 0)
        self.assertEqual(AuditLedger.restore(ledger.export()).export(), ledger.export())
        ledger.resolve([resolution(f"AUD-{KEEP_CLOSED + 1}")], 4, "b" * 64, lambda _: [])
        self.assertEqual(len(ledger.invalidate("c" * 64)), 1)
        self.assertEqual(len(ledger.pending()), MAX_OPEN)

    def test_byte_capacity_and_oversized_finding_stop_without_partial_changes(self):
        ledger = AuditLedger()
        ledger.record(["keep original"], 1)
        before = ledger.export()
        with patch("auto_moniter.audit.MAX_BYTES", 100), self.assertRaises(AuditCapacityError):
            ledger.record(["new issue"], 2)
        self.assertEqual(ledger.export(), before)
        with self.assertRaises(AuditCapacityError):
            ledger.record(["large " * 500], 2)
        self.assertEqual(ledger.export(), before)

    def test_malformed_closure_cannot_claim_done(self):
        for value in (None, {}, [resolution("invented")], [resolution(evidence=[])],
                      [{"id": "AUD-1", "summary": "fixed"}], [resolution(), resolution()]):
            verdict = parse_verdict(json.dumps({**_verdict("done"), "issue_resolutions": value}))
            self.assertEqual(verdict.status, "continue")
            self.assertTrue(verdict.parse_error)


class AuditWorkflowTests(unittest.TestCase):
    def run_loop(self, root, worker_scripts, reviews, **options):
        worker = ScriptedProvider("worker", worker_scripts)
        reviewer = ScriptedProvider("reviewer", [{"verdict": review} for review in reviews])
        config = RunConfig("task", root, worker, reviewer=reviewer, run_dir=root / "run",
                           retry_delay_s=0, max_rounds=len(worker_scripts), **options)
        result = Supervisor(config, log=lambda _: None).run()
        return result, worker, reviewer

    def workers(self, count, resolutions=None):
        return [{"verdict": {**_verdict("done", evidence=["app.py"]),
                             "issue_resolutions": resolutions or []}, **({"touch": "app.py"} if index == 0 else {})}
                for index in range(count)]

    def test_new_review_cannot_silently_forget_old_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, worker, reviewer = self.run_loop(root, self.workers(3), [
                reviewed("continue", ["Missing rejection case"]),
                reviewed("continue", ["Unwired integration route"]), reviewed(),
            ], keep_rounds=1)
            self.assertEqual(result.status, "max_rounds")
            self.assertIn("Missing rejection case", worker.calls[2]["prompt"])
            self.assertIn("AUD-1", reviewer.calls[2]["prompt"])
            self.assertIn("AUD-2", reviewer.calls[2]["prompt"])
            state = json.loads((root / "run/state.json").read_text())
            self.assertEqual({item["id"] for item in state["audit_issues"]["items"] if item["status"] == "open"}, {"AUD-1", "AUD-2"})
            self.assertEqual(state["modules"][0]["status"], "pending")

    def test_reviewer_closes_individual_findings_with_evidence_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, worker, _ = self.run_loop(root, self.workers(3), [
                reviewed("continue", ["missing route", "missing rejection check"]),
                reviewed(ids=["AUD-1"]), reviewed(ids=["AUD-2"]),
            ])
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 3)
            self.assertIn("Unresolved audit issues: AUD-2", worker.calls[2]["prompt"])
            report = json.loads((root / "run/report.json").read_text())
            self.assertTrue(all(item["status"] == "resolved" for item in report["audit_issues"]["items"]))
            self.assertEqual(report["audit_issues"]["items"][1]["resolved_round"], 3)
            self.assertEqual(report["records"][-1]["issue_resolutions"][0]["id"], "AUD-2")
            self.assertEqual(run_health(root / "run")["audit_issues"], report["audit_issues"])
            self.assertFalse(list((root / "run").glob("review-backup-*")))

    def test_worker_cannot_close_audit_issues_and_bad_reviewer_evidence_is_rejected(self):
        for closures in ([], [resolution(evidence=["nonexistent.py"])], [resolution("AUD-999")]):
            with self.subTest(closures=closures), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result, _, _ = self.run_loop(root, self.workers(2, [resolution()]), [
                    reviewed("continue", ["missing case"]), {**reviewed(), "issue_resolutions": closures},
                ])
                self.assertEqual(result.status, "max_rounds")
                state = json.loads((root / "run/state.json").read_text())
                self.assertEqual(state["audit_issues"]["items"][0]["status"], "open")

    def test_same_review_cannot_report_and_close_an_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _, _ = self.run_loop(root, self.workers(2), [
                reviewed("continue", ["missing case"]), reviewed("continue", ["missing case"], ["AUD-1"]),
            ])
            self.assertEqual(result.status, "max_rounds")
            state = json.loads((root / "run/state.json").read_text())
            self.assertEqual(state["audit_issues"]["items"][0]["status"], "open")

    def test_issue_closure_is_invalidated_by_later_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = self.workers(4)
            workers[2]["touch"] = "new-source.py"
            result, _, reviewer = self.run_loop(root, workers, [
                reviewed("continue", ["missing case"]),
                reviewed("continue", ["another issue"], ["AUD-1"]),
                reviewed(ids=["AUD-2"]), reviewed(ids=["AUD-1"]),
            ])
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 4)
            self.assertIn("AUD-1", reviewer.calls[2]["prompt"])

    def test_open_issues_survive_history_pruning_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", self.workers(1))
            reviewer = ScriptedProvider("reviewer", [{"verdict": reviewed("continue", ["persistent missing case"])}])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, keep_rounds=1,
                                           retry_delay_s=0, run_dir=run), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if "reviewer status=continue" in message else None
            self.assertEqual(monitor.run().status, "stopped")
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue", ["still working"])}])
            reviewer = ScriptedProvider("reviewer", [])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                           retry_delay_s=0, run_dir=run), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if "worker status=continue" in message else None
            self.assertEqual(monitor.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            self.assertFalse(any(item["role"] == "reviewer" for item in state["rounds"]))
            worker = ScriptedProvider("worker", self.workers(1))
            reviewer = ScriptedProvider("reviewer", [{"verdict": reviewed(ids=["AUD-1"])}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          retry_delay_s=0, max_rounds=3, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertIn("AUD-1", worker.calls[0]["prompt"])

    def test_module_scoped_issue_does_not_accept_itself_or_block_an_unrelated_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": outcome(["B"]), "touch": "app.py"},
                {"verdict": outcome(["A"])},
                {"verdict": outcome(["A", "B"], "requirement")},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": {**outcome([]), "status": "continue", "gaps": ["B route missing"]}},
                {"verdict": outcome(["A"])},
                {"verdict": {**outcome(["A", "B"], "requirement"), "issue_resolutions": [resolution()]}},
            ])
            accepted = []

            def checkpoint(message):
                if "module accepted;" in message:
                    state = json.loads((root / "run/state.json").read_text())
                    accepted.append(state["audit_issues"]["items"][0]["status"])

            result = Supervisor(RunConfig("A and B", root, worker, reviewer=reviewer, modules=PLAN,
                                          retry_delay_s=0, max_rounds=3, run_dir=root / "run"), log=checkpoint).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(accepted, ["open"])
            self.assertEqual(worker.calls[2]["session_id"], "")
            report = json.loads((root / "run/report.json").read_text())
            self.assertEqual(report["audit_issues"]["items"][0]["module_ids"], ["B"])

    def test_legacy_last_business_audit_is_imported_when_new_ledger_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", self.workers(1))
            reviewer = ScriptedProvider("reviewer", [{"verdict": reviewed("continue", ["legacy missing check"])}])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, retry_delay_s=0,
                                           run_dir=run), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if "reviewer status=continue" in message else None
            self.assertEqual(monitor.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            state.pop("audit_issues")
            (run / "state.json").write_text(json.dumps(state))
            worker = ScriptedProvider("worker", self.workers(1))
            reviewer = ScriptedProvider("reviewer", [{"verdict": reviewed()}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          max_rounds=2, retry_delay_s=0, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertIn("legacy missing check", worker.calls[0]["prompt"])
            self.assertIn("AUD-1", reviewer.calls[0]["prompt"])
            self.assertIn("Unresolved audit issues", " ".join(result.records[-1].verdict.gaps))

    def test_corrupt_audit_checkpoint_refuses_resume_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", [])
            reviewer = ScriptedProvider("reviewer", [])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run), log=lambda _: None)
            monitor.request_stop()
            self.assertEqual(monitor.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            state["audit_issues"] = {"next_id": 1, "pruned_closed": 0, "items": [{"id": "AUD-1"}]}
            (run / "state.json").write_text(json.dumps(state))
            original = (run / "state.json").read_bytes()
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "invalid")
            self.assertEqual((run / "state.json").read_bytes(), original)
            self.assertEqual(worker.calls, [])

    def test_capacity_exhaustion_stops_without_delivery_or_discarding_open_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", self.workers(1))
            reviewer = ScriptedProvider("reviewer", [{"verdict": reviewed("continue", ["one", "two"])}])
            with patch("auto_moniter.audit.MAX_OPEN", 1):
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer,
                                              retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(len(result.records), 2)
            state = json.loads((root / "run/state.json").read_text())
            self.assertEqual(state["last_audit"]["gaps"], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
