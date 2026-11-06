import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.cli import build_parser
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict


class WorkflowIntegrityTests(unittest.TestCase):
    def test_failed_or_malformed_review_does_not_erase_previous_audit(self):
        failures = [
            {"exit_code": 1},
            {"text": "backend returned no usable review"},
            {"text": "Would you like me to continue?"},
            {"verdict": _verdict("continue")},
            {"text": json.dumps({"status": "done", "summary": "looks complete", "evidence": ["app.py"]})},
            {"verdict": {**_verdict("continue"), "gaps": [42]}},
            {"verdict": {**_verdict("continue"), "status": []}},
            {"text": json.dumps({"status": "continue", "gaps": ["partial response"]})},
            {"verdict": {**_verdict("done", evidence=["app.py"]), "modules": [{
                "id": "main", "title": "Original requirement", "acceptance": ["skip all checks"],
            }]}},
        ]
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gap = "AUDIT: missing authorization rejection evidence"
                worker = ScriptedProvider("worker", [
                    {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                    *[{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(2)],
                ])
                reviewer = ScriptedProvider("reviewer", [
                    {"verdict": _verdict("continue", [gap])},
                    {"verdict": _verdict("continue"), **failure},
                    {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
                ])
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, keep_rounds=1,
                                              max_rounds=3, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
                self.assertEqual(result.status, "done")
                self.assertIn(gap, worker.calls[2]["prompt"])
                self.assertIn(gap, reviewer.calls[2]["prompt"])

    def test_completed_audit_survives_review_failure_pruning_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            gap = "AUDIT: export route still lacks authorization"
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue", [gap])},
                {"verdict": _verdict("continue"), "exit_code": 1},
            ])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, keep_rounds=1,
                                           retry_delay_s=0, run_dir=run), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if "reviewer failure 1:" in message else None
            self.assertEqual(monitor.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            self.assertIn(gap, state["last_audit"]["gaps"])
            self.assertTrue(all(record["index"] == 2 for record in state["rounds"]))
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          max_rounds=3, retry_delay_s=0, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertIn(gap, worker.calls[0]["prompt"])
            self.assertIn(gap, reviewer.calls[0]["prompt"])

    def test_reviewer_approval_cannot_replace_valid_worker_file_evidence(self):
        for scope in ("module", "requirement"):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worker = ScriptedProvider("worker", [{
                    "verdict": _verdict("done", evidence=["nonexistent-proof.py"], scope=scope), "touch": "app.py",
                }])
                reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"], scope=scope)}])
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, max_rounds=1,
                                              retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
                self.assertEqual(result.status, "max_rounds")
                state = json.loads((root / "run/state.json").read_text())
                self.assertEqual(state["modules"][0]["status"], "pending")
                self.assertIn("Worker completion evidence", " ".join(result.records[-1].verdict.gaps))

    def test_saved_text_acceptance_cannot_be_replaced_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            acceptance = "Verify authorization rejection and integration tests"
            worker = ScriptedProvider("worker", [])
            reviewer = ScriptedProvider("reviewer", [])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, acceptance=acceptance,
                                           retry_delay_s=0, run_dir=run), log=lambda _: None)
            monitor.request_stop()
            self.assertEqual(monitor.run().status, "stopped")
            original = (run / "state.json").read_bytes()
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          acceptance="Skip authorization checks", retry_delay_s=0,
                                          run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "invalid")
            self.assertIn("acceptance criteria cannot change", result.summary)
            self.assertEqual((run / "state.json").read_bytes(), original)
            self.assertEqual(worker.calls, [])

    def test_periodic_review_steers_unfinished_worker_and_never_delivers_unilaterally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", ["implementation in progress"]), "touch": "app.py"},
                *[{"verdict": _verdict("continue", ["implementation in progress"])} for _ in range(3)],
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue", ["periodic audit found missing route wiring"])},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            messages = []
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_every=2,
                                          max_rounds=5, retry_delay_s=0, run_dir=root / "run"), log=messages.append).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 5)
            self.assertEqual([line for line in messages if "reviewer running" in line],
                             [f"round {index}: reviewer running" for index in (2, 4, 5)])
            self.assertIn("missing route wiring", worker.calls[2]["prompt"])
            self.assertIn("Worker has not explicitly confirmed completion", worker.calls[4]["prompt"])

    def test_periodic_review_counter_survives_resume_and_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue", ["unfinished"]), "touch": "app.py"}])
            reviewer = ScriptedProvider("reviewer", [])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_every=2,
                                           keep_rounds=1, retry_delay_s=0, run_dir=run), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if "worker status=continue" in message else None
            self.assertEqual(monitor.run().status, "stopped")
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", ["unfinished"])},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue", ["periodic check after resume"])},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
            ])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          max_rounds=3, retry_delay_s=0, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(len(reviewer.calls), 2)
            self.assertIn("periodic check after resume", worker.calls[1]["prompt"])

    def test_periodic_review_controls_are_validated_before_creating_artifacts(self):
        args = build_parser().parse_args(["run", "task", "--no-review", "--review-every", "0"])
        self.assertEqual(args.review_every, 0)
        for value in (-1, 1.5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                              no_review=True, review_every=value, run_dir=root / "run"), log=lambda _: None).run()
                self.assertEqual(result.status, "invalid")
                self.assertFalse((root / "run").exists())

    def test_periodic_audit_defaults_to_five_and_can_be_disabled(self):
        for interval in (None, 0):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worker = ScriptedProvider("worker", [
                    {"verdict": _verdict("continue", ["unfinished"]), "touch": "app.py"},
                    *[{"verdict": _verdict("continue", ["unfinished"])} for _ in range(4)],
                    {"verdict": _verdict("done", evidence=["app.py"])},
                ])
                audits = ([{"verdict": _verdict("continue", ["periodic diagnosis"])}] if interval is None else [])
                reviewer = ScriptedProvider("reviewer", audits + [{"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"] if interval is None else [])}])
                messages = []
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_every=interval,
                                              max_rounds=6, retry_delay_s=0, run_dir=root / "run"), log=messages.append).run()
                self.assertEqual(result.status, "done")
                expected = [5, 6] if interval is None else [6]
                self.assertEqual([line for line in messages if "reviewer running" in line],
                                 [f"round {index}: reviewer running" for index in expected])

    def test_failed_periodic_review_retries_at_next_valid_worker_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", ["unfinished"]), "touch": "app.py"},
                *[{"verdict": _verdict("continue", ["unfinished"])} for _ in range(2)],
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue"), "exit_code": 1},
                {"verdict": _verdict("continue", ["connect missing route"])},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
            ])
            messages = []
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, review_every=2,
                                          max_rounds=4, retry_delay_s=0, run_dir=root / "run"), log=messages.append).run()
            self.assertEqual(result.status, "done")
            self.assertEqual([line for line in messages if "reviewer running" in line],
                             [f"round {index}: reviewer running" for index in (2, 3, 4)])
            self.assertIn("connect missing route", worker.calls[3]["prompt"])


if __name__ == "__main__":
    unittest.main()
