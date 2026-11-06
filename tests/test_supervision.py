import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import build_parser
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from auto_moniter.providers import PreparedCommand
from auto_moniter.workspace import file_lock
from tests.test_supervisor import ScriptedProvider, _verdict


class SupervisionTests(unittest.TestCase):
    def test_monitor_rejects_ai_completion_until_command_passes(self):
        for no_review in (False, True):
            with self.subTest(no_review=no_review), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code = "from pathlib import Path; p=Path('.auto-moniter/check-count'); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); print('assertion failed' if n == 0 else 'passed'); raise SystemExit(1 if n == 0 else 0)"
                command = shlex.join([sys.executable, "-c", code])
                worker = ScriptedProvider("worker", [
                    {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                    {"verdict": _verdict("done", evidence=["app.py"])},
                ])
                reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(2)])
                run = root / "run"
                result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, no_review=no_review, checks=[command], run_dir=run, retry_delay_s=0), log=lambda _: None).run()
                self.assertEqual(result.status, "done")
                self.assertEqual(result.rounds, 2)
                self.assertIn("Acceptance check failed", worker.calls[1]["prompt"])
                self.assertIn("assertion failed", worker.calls[1]["prompt"])
                if not no_review:
                    self.assertIn("Acceptance check failed", reviewer.calls[1]["prompt"])
                self.assertIn("exit=1", (run / "001-check-01.log").read_text())
                self.assertIn("exit=0", (run / "002-check-01.log").read_text())
                self.assertEqual(json.loads((run / "state.json").read_text())["checks"], [command])

    def test_audit_feedback_survives_worker_failure_and_history_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gap = "AUDIT-17: validate missing authorization on the export endpoint"
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                {"verdict": _verdict("continue"), "exit_code": 1},
                {"verdict": _verdict("continue", ["implementation in progress"])},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue", [gap], ["Add an authorization check and rejection evidence"])},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
            ])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, keep_rounds=1,
                                          max_rounds=4, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 4)
            self.assertIn(gap, worker.calls[2]["prompt"])
            self.assertIn(gap, worker.calls[3]["prompt"])
            self.assertIn(gap, reviewer.calls[1]["prompt"])
            self.assertEqual(worker.calls[2]["session_id"], "")
            self.assertTrue(all(call["read_only"] for call in reviewer.calls))
            self.assertFalse(any(call["read_only"] for call in worker.calls))

    def test_audit_feedback_survives_resume_after_worker_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            gap = "AUDIT-23: missing request size limit and rejection test evidence"
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                {"verdict": _verdict("continue"), "exit_code": 1},
            ])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("continue", [gap])}])
            monitor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, keep_rounds=1,
                                           retry_delay_s=0, run_dir=run), log=lambda _: None)
            monitor.log = lambda message: monitor.request_stop() if "worker failure 1:" in message else None
            self.assertEqual(monitor.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            self.assertTrue(all(record["role"] == "worker" for record in state["rounds"]))
            self.assertIn(gap, state["last_review"]["gaps"])
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, resume=True,
                                          max_rounds=3, retry_delay_s=0, run_dir=run), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertIn(gap, worker.calls[0]["prompt"])
            self.assertIn(gap, reviewer.calls[0]["prompt"])

    def test_reviewer_cannot_deliver_when_worker_has_not_confirmed_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("blocked"), "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(2)])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, max_rounds=2,
                                          retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 2)
            self.assertIn("Worker has not explicitly confirmed completion", worker.calls[1]["prompt"])

    def test_acceptance_timeout_returns_work_to_worker_and_caps_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = shlex.join([sys.executable, "-c", "import time; print('x'*40000, flush=True); time.sleep(30)"])
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, checks=[command], check_timeout_s=0.2, max_rounds=1, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "max_rounds")
            self.assertIn("Acceptance check failed", " ".join(result.records[0].verdict.gaps))
            log = root / "run/001-check-01.log"
            self.assertIn("timed_out=True", log.read_text())
            self.assertLess(log.stat().st_size, 25000)

    def test_multiple_checks_run_and_module_milestone_does_not_trigger_final_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", scope="module", evidence=["a.py"]), "touch": "a.py"},
                {"verdict": _verdict("done", evidence=["b.py"]), "touch": "b.py"},
            ])
            commands = ["echo check-one", "echo check-two"]
            result = Supervisor(RunConfig("task", root, worker, no_review=True, checks=commands, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertFalse((root / "run/001-check-01.log").exists())
            self.assertIn("check-one", (root / "run/002-check-01.log").read_text())
            self.assertIn("check-two", (root / "run/002-check-02.log").read_text())

    def test_resume_preserves_checks_and_rejects_substitution(self):
        for replacement in ([], ["echo changed"]):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / "run"
                run.mkdir()
                (run / "requirement.md").write_text("task")
                (run / "state.json").write_text(json.dumps({"status": "stopped", "checks": ["echo original"], "baseline_files": {}}))
                worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
                result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=run, resume=True, checks=replacement), log=lambda _: None).run()
                self.assertEqual(result.status, "invalid" if replacement else "done")
                if replacement:
                    self.assertEqual(worker.calls, [])
                else:
                    self.assertIn("original", (run / "001-check-01.log").read_text())

    def test_workspace_lock_blocks_different_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [])
            with file_lock(root / ".auto-moniter/workspace.lock"):
                result = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "other-run"), log=lambda _: None).run()
            self.assertEqual(result.status, "locked")
            self.assertEqual(worker.calls, [])
            self.assertFalse((root / "other-run").exists())
            supervisor = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "other-run"), log=lambda _: None)
            supervisor.request_stop()
            self.assertEqual(supervisor.run().status, "stopped")

    def test_stagnation_starts_new_session_and_preserves_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue", ["same unresolved failure"]), "session_id": "11111111-1111-1111-1111-111111111111"} for _ in range(3)
            ] + [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            supervisor = Supervisor(RunConfig("task", root, worker, no_review=True, restart_after_stall=2, run_dir=root / "run", retry_delay_s=0), log=lambda _: None)
            with patch.object(supervisor, "_close_delivered_sessions", return_value=[]) as close:
                result = supervisor.run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 4)
            self.assertTrue(worker.calls[1]["session_id"])
            self.assertEqual(worker.calls[3]["session_id"], "")
            self.assertIn("try a different concrete approach", worker.calls[3]["prompt"])
            self.assertEqual(close.call_count, 2)

    def test_cli_exposes_supervision_controls(self):
        args = build_parser().parse_args(["run", "task", "--no-review", "--check", "true", "--check", "echo ok", "--check-timeout", "2m", "--restart-after-stall", "4"])
        self.assertEqual(args.check, ["true", "echo ok"])
        self.assertEqual(args.check_timeout, "2m")
        self.assertEqual(args.restart_after_stall, 4)

    def test_acceptance_output_is_not_mistaken_for_agent_input_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = "import time; print('Would you like me to proceed?', flush=True); time.sleep(0.3); print('passed')"
            result = run_command(PreparedCommand([sys.executable, "-c", code]), Path(tmp), detect_input_requests=False, input_grace_s=0.01)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.interruption, "")


if __name__ == "__main__":
    unittest.main()
