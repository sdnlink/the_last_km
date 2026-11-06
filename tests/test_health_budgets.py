import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_moniter.cli import main
from auto_moniter.health import run_health
from auto_moniter.providers import PreparedCommand, resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from auto_moniter.workspace import file_lock
from tests.test_supervisor import ScriptedProvider, _verdict


class BudgetTests(unittest.TestCase):
    def test_agent_call_budget_is_shared_by_worker_reviewer_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run, max_agent_calls=1), log=lambda _: None).run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(reviewer.calls, [])
            self.assertEqual(json.loads((run / "state.json").read_text())["usage"]["agent_calls"], 1)
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run, resume=True), log=lambda _: None).run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertEqual(len(worker.calls), 1)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])}])
            result = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run, resume=True, max_agent_calls=3), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(json.loads((run / "report.json").read_text())["usage"]["agent_calls"], 3)

    def test_runtime_budget_interrupts_live_agent_and_reaps_child(self):
        import shlex

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = resolve_provider("command", command=shlex.join([sys.executable, "-c", "import time; time.sleep(30)"]))
            supervisor = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", max_runtime_s=0.2), log=lambda _: None)
            children = []
            original = supervisor._remember_child
            def remember(child):
                children.append(child)
                original(child)
            supervisor._remember_child = remember
            result = supervisor.run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertIsNotNone(children[0].returncode)
            self.assertGreaterEqual(json.loads((root / "run/report.json").read_text())["usage"]["elapsed_s"], 0.2)

    def test_output_flood_is_bounded_and_cannot_be_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)"
            result = run_command(PreparedCommand([sys.executable, "-c", code]), Path(tmp), max_output_bytes=8192)
            self.assertEqual(result.interruption, "output limit")
            self.assertNotEqual(result.exit_code, 0)
            self.assertLessEqual(len(result.stdout.encode()) + len(result.stderr.encode()), 8192)

    def test_runtime_budget_also_interrupts_monitor_acceptance_checks(self):
        import shlex

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            check = shlex.join([sys.executable, "-c", "import time; time.sleep(30)"])
            result = Supervisor(RunConfig("task", root, worker, no_review=True, checks=[check], run_dir=root / "run", max_runtime_s=0.3), log=lambda _: None).run()
            self.assertEqual(result.status, "budget_exceeded")
            self.assertFalse(result.status == "done")
            self.assertEqual(json.loads((root / "run/report.json").read_text())["usage"]["agent_calls"], 1)

    def test_large_stdin_and_both_output_streams_do_not_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = "import sys; sys.stdout.write('x'*100000); sys.stdout.flush(); sys.stderr.write('y'*100000); sys.stderr.flush(); print(len(sys.stdin.read()))"
            result = run_command(PreparedCommand([sys.executable, "-c", code], stdin_text="z"*500000), Path(tmp), timeout_s=3)
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.stdout.endswith("500000\n"))
            self.assertEqual(len(result.stderr), 100000)


class HealthTests(unittest.TestCase):
    def test_health_distinguishes_active_stale_dead_and_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            state = {"status": "running", "rounds": [], "modules": [{"id": "A", "status": "accepted"}, {"id": "B", "status": "pending"}]}
            (run / "state.json").write_text(json.dumps(state))
            def heartbeat(seconds=0):
                (run / "heartbeat").write_text(json.dumps({"at": (datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat(), "note": "round 2: acceptance check 1"}))
            heartbeat()
            with file_lock(run / "supervisor.lock"):
                health = run_health(run)
                self.assertEqual(health["health"], "active")
                self.assertEqual(health["phase"], "validating")
                self.assertEqual(health["pending_modules"], ["B"])
                heartbeat(120)
                self.assertEqual(run_health(run)["health"], "heartbeat_stale")
                (run / "heartbeat").write_text("{interrupted")
                self.assertEqual(run_health(run)["health"], "heartbeat_missing")
            self.assertEqual(run_health(run)["health"], "monitor_exited")
            (run / "report.json").write_text(json.dumps({**state, "status": "done", "rounds": 0, "records": [], "summary": "done", "stop_reason": "done"}))
            (run / "state.json").write_text(json.dumps({**state, "status": "done"}))
            self.assertEqual(run_health(run)["health"], "finished")

    def test_status_cli_reads_without_creating_or_changing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "state.json").write_text(json.dumps({"status": "running", "rounds": []}))
            before = {path.name: path.read_bytes() for path in run.iterdir()}
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", str(run)]), 0)
            self.assertEqual(json.loads(output.getvalue())["health"], "heartbeat_missing")
            self.assertEqual({path.name: path.read_bytes() for path in run.iterdir()}, before)

    def test_waiting_permission_is_visible_as_a_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "state.json").write_text(json.dumps({"status": "running", "rounds": []}))
            (run / "heartbeat").write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "note": "round 1: worker Paseo status: awaiting_permission"}))
            with file_lock(run / "supervisor.lock"):
                self.assertEqual(run_health(run)["phase"], "waiting_for_input")


if __name__ == "__main__":
    unittest.main()
