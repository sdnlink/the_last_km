import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict


class DeliveryTests(unittest.TestCase):
    def test_module_delivery_starts_fresh_agents_until_entire_requirement_passes(self):
        for worker_scope, review_scope in [("module", "module"), ("module", "requirement"), ("requirement", "module")]:
            with self.subTest(scopes=(worker_scope, review_scope)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                requirement = "Deliver module A and module B, including their integration."
                first = _verdict("done", evidence=["module_a.py"], scope=worker_scope)
                first["summary"] = "Module A is deliverable"
                review = _verdict("done", evidence=["module_a.py"], scope=review_scope)
                review["summary"] = "Module A passed"
                worker = ScriptedProvider("worker", [
                    {"verdict": first, "touch": "module_a.py", "session_id": "11111111-1111-1111-1111-111111111111"},
                    {"verdict": _verdict("done", evidence=["module_b.py"]), "touch": "module_b.py", "session_id": "22222222-2222-2222-2222-222222222222"},
                ])
                reviewer = ScriptedProvider("reviewer", [
                    {"verdict": review, "session_id": "33333333-3333-3333-3333-333333333333"},
                    {"verdict": _verdict("done", evidence=["module_b.py"]), "session_id": "44444444-4444-4444-4444-444444444444"},
                ])
                checkpoints = []

                def observe(message):
                    if "module accepted;" in message:
                        checkpoints.append(message)
                        self.assertFalse((root / "run/report.json").exists())

                supervisor = Supervisor(RunConfig(requirement, root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0, fresh_review=False), log=observe)
                with patch.object(supervisor, "_close_delivered_sessions", return_value=[]) as close:
                    result = supervisor.run()
                self.assertEqual(result.status, "done")
                self.assertEqual(result.rounds, 2)
                self.assertEqual(len(checkpoints), 1)
                self.assertEqual(close.call_count, 2)
                self.assertEqual([call["session_id"] for call in worker.calls], ["", ""])
                self.assertEqual([call["session_id"] for call in reviewer.calls], ["", ""])
                self.assertTrue(all(call["read_only"] for call in reviewer.calls))
                self.assertIn(requirement, worker.calls[1]["prompt"])
                self.assertIn("Module A passed", worker.calls[1]["prompt"])
                self.assertIn("next unfinished module", worker.calls[1]["prompt"])
                state = json.loads((root / "run/state.json").read_text())
                self.assertEqual(state["rounds"][1]["scope"], "module")
                self.assertEqual(state["last_review"]["scope"], "requirement")
                self.assertEqual(list((root / "run").glob("review-backup-*")), [])

    def test_no_review_module_delivery_still_continues_original_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", scope="module", evidence=["a.py"]), "touch": "a.py"},
                {"verdict": _verdict("done", evidence=["b.py"]), "touch": "b.py"},
            ])
            result = Supervisor(RunConfig("Build A and B", root, worker, no_review=True, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(result.status, "done")
            self.assertEqual(result.rounds, 2)
            self.assertEqual(worker.calls[1]["session_id"], "")

    def test_module_checkpoint_resumes_with_history_and_fresh_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", scope="module", evidence=["a.py"]), "touch": "a.py"}])
            accepted = _verdict("done", scope="module", evidence=["a.py"])
            accepted["summary"] = "Module A accepted"
            reviewer = ScriptedProvider("reviewer", [{"verdict": accepted}])
            first = Supervisor(RunConfig("Build A and B", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None)
            first.log = lambda message: first.request_stop() if "module accepted;" in message else None
            self.assertEqual(first.run().status, "stopped")
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["b.py"]), "touch": "b.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["b.py"])}])
            resumed = Supervisor(RunConfig("Build A and B", root, worker, reviewer=reviewer, run_dir=root / "run", resume=True, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(resumed.status, "done")
            self.assertEqual(resumed.rounds, 2)
            self.assertEqual(worker.calls[0]["session_id"], "")
            self.assertEqual(reviewer.calls[0]["session_id"], "")
            self.assertIn("Module A accepted", worker.calls[0]["prompt"])
            self.assertIn("Accepted module milestones", worker.calls[0]["prompt"])

    def test_module_cleanup_failure_prevents_next_module_from_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", scope="module", evidence=["a.py"]), "touch": "a.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", scope="module", evidence=["a.py"])}])
            supervisor = Supervisor(RunConfig("Build A and B", root, worker, reviewer=reviewer, run_dir=root / "run"), log=lambda _: None)
            with patch.object(supervisor, "_close_delivered_sessions", return_value=["worker stop failed"]):
                result = supervisor.run()
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.rounds, 1)
            self.assertIn("Module accepted, but agent cleanup failed", result.summary)
            self.assertEqual(len(worker.calls), 1)

    def test_native_worker_and_reviewer_are_reaped_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"])}])
            supervisor = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=root / "run"), log=lambda _: None)
            children = []
            remember = supervisor._remember_child

            def track(child):
                children.append(child)
                remember(child)

            with patch.object(supervisor, "_remember_child", side_effect=track):
                report = supervisor.run()
            self.assertEqual(report.status, "done")
            self.assertEqual(len(children), 2)
            self.assertTrue(all(child.returncode == 0 for child in children))
            for child in children:
                with self.assertRaises(ChildProcessError):
                    os.waitpid(child.pid, os.WNOHANG)

    def test_paseo_agents_close_before_delivery_and_cleanup_failures_are_reported(self):
        for fail in ["", "stop", "archive", "reviewer-archive", "modules"]:
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                executable = root / "fake-paseo"
                worker_id = "11111111-1111-1111-1111-111111111111"
                reviewer_id = "22222222-2222-2222-2222-222222222222"
                executable.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
with Path('run/paseo-events.jsonl').open('a') as output:
    output.write(json.dumps(args) + '\\n')
if args[0] == 'run':
    role = 'reviewer' if ':reviewer:' in args[args.index('--title') + 1] else 'worker'
    count_path = Path('run/' + role + '-count')
    count = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(count))
    agent_id = {reviewer_id!r} if role == 'reviewer' else {worker_id!r}
    if {fail!r} == 'modules' and count == 2:
        agent_id = '44444444-4444-4444-4444-444444444444' if role == 'reviewer' else '33333333-3333-3333-3333-333333333333'
    if role == 'worker':
        Path('app.py').write_text('deliverable')
    print(json.dumps({{'agentId': agent_id, 'status': 'running'}}))
elif args[0] == 'inspect':
    print(json.dumps({{'status': 'idle'}}))
elif args[0] == 'logs':
    verdict = {_verdict('done', evidence=['app.py'])!r}
    if {fail!r} == 'modules' and args[-1] in ({worker_id!r}, {reviewer_id!r}):
        verdict['scope'] = 'module'
    print(json.dumps(verdict))
elif args[:2] in (['agent', 'stop'], ['agent', 'archive']):
    assert not Path('run/report.json').exists(), 'cleanup must precede delivery report'
    if (args[-1] == {worker_id!r} and args[1] == {fail!r}) or ({fail!r} == 'reviewer-archive' and args[-1] == {reviewer_id!r} and args[1] == 'archive'):
        sys.exit(1)
else:
    sys.exit(2)
''')
                executable.chmod(0o755)
                worker = resolve_provider("paseo", paseo_backend="claude")
                reviewer = resolve_provider("paseo", paseo_backend="claude", role="reviewer")
                worker.binary = reviewer.binary = str(executable)
                report = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
                events = [json.loads(line) for line in (root / "run/paseo-events.jsonl").read_text().splitlines()]
                if fail == "reviewer-archive":
                    self.assertEqual(report.status, "failed")
                    self.assertIn("Could not close Paseo reviewer", report.summary)
                    self.assertEqual(sum(event[0] == "run" for event in events), 2)
                else:
                    self.assertIn(["agent", "stop", "--json", reviewer_id], events)
                    self.assertIn(["agent", "archive", "--json", reviewer_id], events)
                    final_worker = "33333333-3333-3333-3333-333333333333" if fail == "modules" else worker_id
                    self.assertEqual(events[-2:], [["agent", "stop", "--json", final_worker], ["agent", "archive", "--json", final_worker]])
                    self.assertEqual(report.status, "failed" if fail in {"stop", "archive"} else "done")
                    if fail in {"stop", "archive"}:
                        self.assertIn("cleanup failed", report.summary)
                    if fail == "modules":
                        starts = [index for index, event in enumerate(events) if event[0] == "run"]
                        self.assertEqual(len(starts), 4)
                        self.assertFalse(any(event[0] == "send" for event in events))
                        self.assertLess(events.index(["agent", "archive", "--json", worker_id]), starts[2])
                        self.assertLess(events.index(["agent", "archive", "--json", reviewer_id]), starts[2])
                        self.assertEqual(report.rounds, 2)
                self.assertEqual(json.loads((root / "run/report.json").read_text())["status"], report.status)

    def test_cli_returns_success_after_one_delivered_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = f"from pathlib import Path; import json; Path('app.py').write_text('done'); print(json.dumps({_verdict('done', evidence=['app.py'])!r}))"
            import shlex

            result = subprocess.run([
                sys.executable, "-m", "auto_moniter", "run", "task", "--cwd", str(root),
                "--provider", "command", "--exec", shlex.join([sys.executable, "-c", code]), "--no-review",
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("run directory:", result.stdout)
            run = Path(next(line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("run directory:")))
            self.assertFalse(run.is_relative_to(root))
            self.assertEqual(json.loads((run / "report.json").read_text())["status"], "done")


if __name__ == "__main__":
    unittest.main()
