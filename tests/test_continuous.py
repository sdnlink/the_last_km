import json
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import build_parser
from auto_moniter.providers import PreparedCommand, ProviderError, resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from auto_moniter.verdict import parse_verdict
from auto_moniter.workspace import file_snapshot, preserve_tree, restore_reviewer_writes
from tests.test_supervisor import ScriptedProvider, _verdict


SESSION = "11111111-1111-1111-1111-111111111111"


class ContinuousLoopTests(unittest.TestCase):
    def test_questions_and_failure_continue_until_worker_and_reviewer_are_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("continue"), "text": "Would you like me to continue with the tests?", "session_id": SESSION, "touch": "app.py"},
                {"verdict": _verdict("blocked"), "session_id": SESSION},
                {"verdict": _verdict("done"), "exit_code": 1},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("blocked")},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
            ])
            report = Supervisor(RunConfig("Complete the health endpoint and tests", root, worker, reviewer=reviewer, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertEqual(report.rounds, 4)
            self.assertEqual(len(reviewer.calls), 2)
            self.assertEqual(worker.calls[1]["session_id"], SESSION)
            self.assertIn("Would you like me to continue", worker.calls[1]["prompt"])
            self.assertIn("Continue the authorized task now", worker.calls[1]["prompt"])
            self.assertIn("need a secret", worker.calls[2]["prompt"])
            self.assertEqual(worker.calls[3]["session_id"], "")
            self.assertNotEqual(report.records[1].verdict.status, "done")

    def test_no_review_does_not_accept_waiting_for_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending = _verdict("done", evidence=["app.py"])
            pending["summary"] = "代码已完成，是否继续运行测试？"
            worker = ScriptedProvider("worker", [
                {"verdict": pending, "touch": "app.py", "session_id": SESSION},
                {"verdict": _verdict("done", evidence=["app.py"]), "session_id": SESSION},
            ])
            report = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertEqual(report.rounds, 2)
            self.assertIn("是否继续运行测试", worker.calls[1]["prompt"])

    def test_transient_command_preparation_error_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            original = worker.start_command
            attempts = 0

            def flaky(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ProviderError("temporary backend unavailable")
                return original(*args, **kwargs)

            with patch.object(worker, "start_command", side_effect=flaky):
                report = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertEqual(report.rounds, 2)
            self.assertEqual(report.records[0].exit_code, 127)

    def test_no_output_watchdog_restarts_worker_until_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = (
                "from pathlib import Path; import time, json; "
                "counter=Path('attempt'); first=not counter.exists(); counter.write_text('started'); "
                "time.sleep(30) if first else None; Path('app.py').write_text('healthy=True'); "
                f"print(json.dumps({_verdict('done', evidence=['app.py'])!r}))"
            )
            provider = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            report = Supervisor(RunConfig("task", root, provider, no_review=True, idle_timeout_s=0.1, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertEqual(report.rounds, 2)
            self.assertTrue(report.records[0].timed_out)
            self.assertIn("no output", report.records[0].verdict.summary)

    def test_reviewer_changes_stop_run_without_backup_or_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
            ])
            reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done"), "touch": "unexpected.py"}])
            report = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, retry_delay_s=0, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(report.status, "failed")
            self.assertEqual(report.rounds, 1)
            self.assertIn("Workspace changed during read-only review", report.summary)
            self.assertEqual((root / "app.py").read_text(), "changed\n")
            self.assertTrue((root / "unexpected.py").exists())
            self.assertEqual(list((root / "run").glob("review-backup-*")), [])
            self.assertEqual(json.loads((root / "run/report.json").read_text())["status"], "failed")

    def test_retry_wait_is_interruptible_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supervisor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), retry_delay_s=5), log=lambda _: None)
            supervisor.run_dir.mkdir(parents=True)
            messages = []
            supervisor.log = messages.append
            with patch.object(supervisor._stop_event, "wait") as wait, patch("auto_moniter.supervisor.time.monotonic", side_effect=[0, 0, 0, 60]):
                supervisor._retry_pause(1000, "failure")
                wait.assert_called_once_with(0.2)
            self.assertIn("retrying in 60s", messages[0])
            started = threading.Event()

            def wait_for_retry():
                started.set()
                supervisor._retry_pause(1, "failure")

            thread = threading.Thread(target=wait_for_retry)
            thread.start()
            started.wait(1)
            supervisor.request_stop()
            thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_old_blocked_run_can_resume_and_preserves_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            (run / "requirement.md").write_text("task")
            (run / "state.json").write_text(json.dumps({"status": "blocked", "acceptance": "Verify health endpoint", "next_round": 2, "baseline_files": {}}))
            worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
            report = Supervisor(RunConfig("task", root, worker, no_review=True, resume=True, run_dir=run, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertIn("Verify health endpoint", worker.calls[0]["prompt"])


class DeliveryVerdictTests(unittest.TestCase):
    def test_questions_in_either_language_are_not_delivery(self):
        for text in ["Would you like me to proceed?", "Please confirm the implementation plan.", "是否继续？", "请确认后我继续执行。"]:
            with self.subTest(text=text):
                verdict = parse_verdict(text)
                self.assertEqual(verdict.status, "continue")
                self.assertTrue(verdict.next_actions)
                self.assertTrue(verdict.raw)

    def test_done_followed_by_question_cannot_complete(self):
        text = json.dumps(_verdict("done", evidence=["app.py"])) + "\nShall I continue with the tests?"
        self.assertEqual(parse_verdict(text).status, "continue")

    def test_malformed_done_cannot_hide_remaining_work(self):
        for fields in [{"gaps": [42]}, {"gaps": {"missing": "tests"}}, {"summary": ""}]:
            with self.subTest(fields=fields):
                data = _verdict("done", evidence=["app.py"])
                data.update(fields)
                self.assertEqual(parse_verdict(json.dumps(data)).status, "continue")
        self.assertEqual(parse_verdict('{"status":"done","summary":"complete","evidence":["app.py"]}').status, "continue")

    def test_cli_continuous_defaults_and_watchdog(self):
        args = build_parser().parse_args(["run", "task", "--no-review"])
        self.assertEqual((args.max_rounds, args.stall_limit, args.failure_limit), (0, 0, 0))
        self.assertIsNone(args.idle_timeout)
        self.assertIsNone(args.retry_delay)


class ReviewerRollbackTests(unittest.TestCase):
    def test_contents_deletions_modes_symlinks_and_directories_are_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, run = root / "project", root / "run"
            project.mkdir()
            (project / "empty").mkdir()
            (project / "empty").chmod(0o755)
            target = project / "app.py"
            target.write_text("worker implementation")
            target.chmod(0o755)
            (project / "deleted.py").write_text("must restore")
            (project / "link.py").symlink_to("app.py")
            before = file_snapshot(project)
            backup = preserve_tree(project, run)
            target.write_text("reviewer damage")
            target.chmod(0o600)
            (project / "deleted.py").unlink()
            (project / "empty").rmdir()
            (project / "new/dir").mkdir(parents=True)
            (project / "new/dir/file").write_text("reviewer")
            (project / "link.py").unlink()
            (project / "link.py").symlink_to("deleted.py")
            changed = restore_reviewer_writes(project, backup)
            self.assertIn("app.py", changed)
            self.assertIn("deleted.py", changed)
            self.assertEqual(before, file_snapshot(project))
            self.assertTrue((project / "empty").is_dir())
            self.assertFalse((project / "new").exists())
            self.assertFalse(backup.root.exists())

    def test_reviewer_cannot_break_restoration_with_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            (project / "src").mkdir(parents=True)
            target = project / "src/app.py"
            target.write_text("worker")
            backup = preserve_tree(project, root / "run")
            target.chmod(0)
            (project / "src").chmod(0)
            try:
                changed = restore_reviewer_writes(project, backup)
                self.assertIn("src/app.py", changed)
                self.assertEqual(target.read_text(), "worker")
            finally:
                (project / "src").chmod(0o755)
                target.chmod(0o644)

    def test_replaced_directory_symlink_does_not_modify_external_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, outside = root / "project", root / "outside"
            (project / "src").mkdir(parents=True)
            outside.mkdir()
            (project / "src/app.py").write_text("worker")
            (outside / "app.py").write_text("external")
            backup = preserve_tree(project, root / "run")
            (project / "src/app.py").unlink()
            (project / "src").rmdir()
            (project / "src").symlink_to(outside, target_is_directory=True)
            restore_reviewer_writes(project, backup)
            self.assertEqual((project / "src/app.py").read_text(), "worker")
            self.assertEqual((outside / "app.py").read_text(), "external")


class LiveInterruptionTests(unittest.TestCase):
    def test_live_confirmation_is_detected_without_waiting_for_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            result = run_command(PreparedCommand([sys.executable, "-c", "import time; print('是否继续执行？', flush=True); time.sleep(30)"]), Path(tmp), input_grace_s=0.1)
            self.assertEqual(result.interruption, "input request")
            self.assertFalse(result.timed_out)
            self.assertLess(time.monotonic() - started, 3)
            self.assertIn("是否继续", result.stdout)

    def test_tool_output_containing_a_question_is_not_a_live_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = json.dumps({"type": "tool_result", "result": "Would you like me to proceed?"})
            code = f"import time; print({event!r}, flush=True); time.sleep(0.4); print('finished', flush=True)"
            result = run_command(PreparedCommand([sys.executable, "-c", code]), Path(tmp), input_grace_s=0.05)
            self.assertEqual(result.interruption, "")
            self.assertEqual(result.exit_code, 0)

    def test_paseo_pending_permission_is_stopped_and_same_agent_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-paseo"
            code = f'''#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
phase_file = Path('run/phase')
phase = int(phase_file.read_text()) if phase_file.exists() else 0
if args[0] in ('run', 'send'):
    assert '--no-wait' in args
    if args[0] == 'send':
        assert Path('run/stopped').exists()
        assert json.loads(Path('run/state.json').read_text())['session_id'] == {SESSION!r}
    phase += 1
    phase_file.write_text(str(phase))
    if phase == 2:
        Path('app.py').write_text('deliverable')
    print(json.dumps({{'agentId': {SESSION!r}, 'status': 'running'}}))
elif args[0] == 'inspect':
    print(json.dumps({{'agentId': {SESSION!r}, 'status': 'awaiting_permission' if phase == 1 else 'idle'}}))
elif args[0] == 'logs':
    print('Please confirm before I continue.' if phase == 1 else json.dumps({_verdict('done', evidence=['app.py'])!r}))
elif args[:2] == ['agent', 'stop']:
    Path('run/stopped').write_text('yes')
elif args[:2] == ['agent', 'archive']:
    Path('run/archived').write_text('yes')
else:
    sys.exit(2)
'''
            executable.write_text(code)
            executable.chmod(0o755)
            worker = resolve_provider("paseo", paseo_backend="claude")
            worker.binary = str(executable)
            report = Supervisor(RunConfig("task", root, worker, no_review=True, run_dir=root / "run", retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, "done")
            self.assertEqual(report.rounds, 2)
            self.assertEqual(report.session_id, SESSION)
            self.assertTrue((root / "run/archived").exists())


if __name__ == "__main__":
    unittest.main()
