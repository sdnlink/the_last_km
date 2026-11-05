import contextlib
import gzip
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import _parse_duration, main
from auto_moniter.providers import PreparedCommand, extract_session_id, extract_text, resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from auto_moniter.verdict import parse_verdict
from auto_moniter.workspace import (
    acceptance_problems,
    file_lock,
    file_snapshot,
    preserve_tree,
    restore_reviewer_writes,
    workspace_diff,
    workspace_fingerprint,
)
from tests.test_supervisor import ScriptedProvider, _verdict


class OutputRegressionTests(unittest.TestCase):
    def test_latest_conclusion_wins_over_earlier_fence(self):
        early = json.dumps(_verdict("done"))
        final = json.dumps(_verdict("continue", ["broken endpoint"]))
        verdict = parse_verdict(f'An unmatched " in prose\n```json\n{early}\n```\n{final}')
        self.assertEqual(verdict.status, "continue")
        self.assertEqual(verdict.gaps, ["broken endpoint"])

    def test_done_with_blockers_cannot_pass(self):
        data = _verdict("done")
        data["blockers"] = ["missing deployment credential"]
        self.assertEqual(parse_verdict(json.dumps(data)).status, "continue")

    def test_codex_item_message_is_extracted(self):
        conclusion = json.dumps(_verdict("done", evidence=["app.py"]))
        blob = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": conclusion}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        ])
        self.assertEqual(extract_text(blob)[0], conclusion)

    def test_claude_structured_output_takes_priority(self):
        blob = json.dumps({"type": "result", "result": "intermediate", "structured_output": _verdict("done")})
        text, structured = extract_text(blob)
        self.assertTrue(structured)
        self.assertEqual(json.loads(text)["status"], "done")

    def test_tool_and_user_output_cannot_supply_conclusion(self):
        for event in [
            {"type": "tool_result", "result": _verdict("done")},
            {"type": "user", "message": {"role": "user", "content": json.dumps(_verdict("done"))}},
        ]:
            with self.subTest(event=event):
                self.assertEqual(extract_text(json.dumps(event)), ("", False))

    def test_pretty_json_keeps_top_level_session(self):
        session = "11111111-1111-1111-1111-111111111111"
        blob = json.dumps({"session_id": session, "metadata": {"nested": True}}, indent=2)
        self.assertEqual(extract_session_id(blob), session)

    def test_command_prompt_placeholders_are_not_expanded_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            text = "literal {cwd} {schema} {session_id} {model} ' $(echo injected) `whoami`"
            prompt.write_text(text)
            provider = resolve_provider("command", command="printf %s {prompt}")
            result = run_command(provider.start_command(prompt, root, prompt), root)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, text)

    def test_claude_receives_schema_json_and_codex_resume_has_valid_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt, schema = root / "prompt.md", root / "schema.json"
            prompt.write_text("task")
            schema.write_text('{"type":"object"}')
            cmd = resolve_provider("claude").start_command(prompt, root, schema).argv
            self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), {"type": "object"})
            cmd = resolve_provider("codex").resume_command("session", prompt, root, schema, read_only=True).argv
            self.assertNotIn("--color", cmd[cmd.index("resume"):])
            self.assertNotIn("--sandbox", cmd[cmd.index("resume"):])
            self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")

    def test_paseo_prompt_limit_counts_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("需求" * 25_000)
            command = resolve_provider("paseo").start_command(prompt, root, prompt)
            self.assertLess(len(command.argv[-1].encode("utf-8")), 100_000)
            self.assertEqual((root / ".auto-moniter/inbox-prompt.md").read_text(), prompt.read_text())


class WorkspaceRegressionTests(unittest.TestCase):
    def test_outside_evidence_and_run_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            outside = root / "outside.py"
            outside.write_text("outside")
            (project / "link.py").symlink_to(outside)
            run = project / "run"
            run.mkdir()
            (run / "proof.md").write_text("log")
            for evidence in ["../outside.py", str(outside), "link.py", "run/proof.md"]:
                with self.subTest(evidence=evidence):
                    self.assertTrue(acceptance_problems(project, [evidence], baseline={}, ignore_dir=run))

    def test_evidence_supports_line_numbers_spaces_and_extensionless_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, evidence in [("app.py", "app.py:12"), ("my app.py", "`my app.py:2`"), ("Dockerfile", "Dockerfile")]:
                (root / name).write_text("changed")
                with self.subTest(evidence=evidence):
                    self.assertEqual(acceptance_problems(root, [evidence], baseline={}), [])

    def test_reviewer_deletions_are_reported_and_empty_directories_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("worker")
            (root / "empty").mkdir()
            before = preserve_tree(root, root / "unused")
            (root / "app.py").unlink()
            self.assertIn("app.py", restore_reviewer_writes(root, before))
            self.assertTrue((root / "empty").is_dir())

    def test_reviewer_symlink_does_not_delete_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            outside = root / "outside.txt"
            outside.write_text("keep")
            before = preserve_tree(project, root / "unused")
            (project / "link.txt").symlink_to(outside)
            self.assertIn("link.txt", restore_reviewer_writes(project, before))
            self.assertEqual(outside.read_text(), "keep")
            self.assertFalse((project / "link.txt").is_symlink())

    def test_fingerprint_reads_full_files_and_excludes_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            target = root / "big.txt"
            target.write_bytes(b"a" * 1_100_000)
            run = root / "run"
            run.mkdir()
            before = workspace_fingerprint(root, ignore_dir=run)
            (run / "log.txt").write_text("heartbeat")
            self.assertEqual(before, workspace_fingerprint(root, ignore_dir=run))
            stat = target.stat()
            with target.open("r+b") as handle:
                handle.seek(-1, 2)
                handle.write(b"b")
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertNotEqual(before, workspace_fingerprint(root, ignore_dir=run))

    def test_git_acceptance_uses_paths_not_truncated_diff_or_matching_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "a").mkdir()
            (root / "b").mkdir()
            first, second = root / "a/app.py", root / "b/app.py"
            first.write_text("old")
            baseline = file_snapshot(root)
            first.write_text("new")
            second.write_text("new")
            # Ignore a/app.py; the untracked b/app.py must not validate it.
            (root / ".gitignore").write_text("a/\n")
            self.assertTrue(acceptance_problems(root, ["a/app.py"], baseline=baseline, diff_text="b/app.py"))
            self.assertEqual(acceptance_problems(root, ["b/app.py"], baseline=baseline, diff_text="(truncated)"), [])

    def test_git_subdirectory_diff_excludes_tracked_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            project = root / "project"
            run = project / "run"
            run.mkdir(parents=True)
            target, log = project / "app.py", run / "log.md"
            target.write_text("old")
            log.write_text("original log")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "baseline"], check=True)
            baseline = file_snapshot(project, ignore_dir=run)
            target.write_text("new")
            log.write_text("should not be in review diff")
            diff = workspace_diff(project, ignore_dir=run)
            self.assertIn("app.py", diff)
            self.assertNotIn("should not be in review diff", diff)
            self.assertEqual(acceptance_problems(project, ["app.py"], baseline=baseline, ignore_dir=run), [])


class LifecycleRegressionTests(unittest.TestCase):
    def test_resume_rejects_different_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            (run / "requirement.md").write_text("task")
            state = json.dumps({"status": "running", "cwd": str(root / "other")})
            (run / "state.json").write_text(state)
            report = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), no_review=True, resume=True, run_dir=run), log=lambda _: None).run()
            self.assertEqual(report.stop_reason, "invalid")
            self.assertEqual((run / "state.json").read_text(), state)

    def test_resume_keeps_failure_count_and_drops_failed_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            (run / "requirement.md").write_text("task")
            (run / "state.json").write_text(json.dumps({
                "status": "running", "next_round": 2,
                "session_id": "11111111-1111-1111-1111-111111111111",
                "rounds": [{"index": 1, "role": "worker", "status": "continue", "failure": "exit 1", "exit_code": 1}],
            }))
            worker = ScriptedProvider("worker", [{"verdict": _verdict("continue"), "exit_code": 1}])
            report = Supervisor(RunConfig("task", root, worker, no_review=True, resume=True, failure_limit=2, run_dir=run), log=lambda _: None).run()
            self.assertEqual(report.status, "failed")
            self.assertIn("failed 2 times", report.summary)
            self.assertEqual(report.rounds, 2)
            self.assertEqual(worker.calls[0]["session_id"], "")

    def test_relative_run_directory_works_with_different_project_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            worker = resolve_provider("command", command="test -f {prompt_file} && test -f {schema} && printf '%s' '" + json.dumps(_verdict("continue", ["open gap"])) + "'")
            config = RunConfig("task", project, worker, no_review=True, max_rounds=1, run_dir=Path(os.path.relpath(root / "run")))
            report = Supervisor(config, log=lambda _: None).run()
            self.assertEqual(report.status, "max_rounds")
            self.assertEqual(report.records[0].exit_code, 0)
            self.assertEqual(report.records[0].verdict.gaps, ["open gap"])

    def test_resume_keeps_baseline_history_and_clears_stale_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            worker = ScriptedProvider("worker", [
                {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            reviewer = ScriptedProvider("reviewer", [
                {"verdict": _verdict("continue", ["verify again"])},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ])
            first = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run), log=lambda _: None)
            first.log = lambda message: first.request_stop() if message.startswith("round 1: reviewer status=") else None
            self.assertEqual(first.run().status, "stopped")
            state = json.loads((run / "state.json").read_text())
            with gzip.open(run / state["baseline_file"], "rt", encoding="utf-8") as handle:
                self.assertNotIn(str(root / "app.py"), json.load(handle))

            def check_report(message):
                if message == "round 2: worker running":
                    self.assertFalse((run / "report.json").exists())

            resumed = Supervisor(RunConfig("task", root, worker, reviewer=reviewer, run_dir=run, resume=True), log=check_report).run()
            self.assertEqual(resumed.status, "done")
            self.assertEqual(resumed.rounds, 2)
            self.assertEqual(resumed.records[0].verdict.evidence, ["app.py"])
            self.assertTrue(resumed.records[0].output_path)

    def test_existing_or_invalid_run_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            (run / "requirement.md").write_text("original task")
            (run / "report.json").write_text('{"status":"done"}')
            worker = ScriptedProvider("worker", [])
            for resume, state in [(False, '{"status":"done"}'), (True, '{"status":"done"}'), (True, "{broken"), (True, "[]")]:
                with self.subTest(resume=resume, state=state):
                    (run / "state.json").write_text(state)
                    before = file_snapshot(run)
                    report = Supervisor(RunConfig("new task", root, worker, no_review=True, run_dir=run, resume=resume), log=lambda _: None).run()
                    self.assertEqual(report.stop_reason, "invalid")
                    # The lock records its holder; all run artifacts are preserved.
                    after = file_snapshot(run)
                    after.pop(str(run / "supervisor.lock"), None)
                    before.pop(str(run / "supervisor.lock"), None)
                    self.assertEqual(before, after)
            self.assertEqual(worker.calls, [])

    def test_lock_refusal_does_not_write_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            with file_lock(run / "supervisor.lock"):
                report = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), run_dir=run), log=lambda _: None).run()
                self.assertEqual(report.status, "locked")
                self.assertFalse((run / "verdict.schema.json").exists())

    def test_signal_handlers_restored_and_background_thread_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            supervisor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), no_review=True, run_dir=root / "run"), log=lambda _: None)
            supervisor.request_stop()
            self.assertEqual(supervisor.run().status, "stopped")
            self.assertEqual(previous, {sig: signal.getsignal(sig) for sig in previous})
            other = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []), no_review=True, run_dir=root / "other"), log=lambda _: None)
            other.request_stop()
            results = []
            thread = threading.Thread(target=lambda: results.append(other.run()))
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0].status, "stopped")

    def test_launch_failure_is_recorded_and_respects_failure_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = ScriptedProvider("worker", [])
            with patch.object(worker, "start_command", return_value=PreparedCommand([str(root / "missing-executable")])):
                report = Supervisor(RunConfig("task", root, worker, no_review=True, failure_limit=1, run_dir=root / "run"), log=lambda _: None).run()
            self.assertEqual(report.status, "failed")
            self.assertEqual(report.records[0].exit_code, 127)


class ProcessAndCliRegressionTests(unittest.TestCase):
    def test_timeout_kills_descendant_holding_pipes_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "group.pid"
            helper = textwrap.dedent('''
                import os, signal, sys, time
                from pathlib import Path
                from auto_moniter.providers import PreparedCommand
                from auto_moniter.supervisor import run_command
                code = "import os, signal, time; pid=os.fork(); signal.signal(signal.SIGTERM, signal.SIG_IGN) if pid == 0 else None; time.sleep(30) if pid == 0 else None"
                result = run_command(PreparedCommand([sys.executable, '-c', code]), Path(sys.argv[1]), timeout_s=0.3,
                    on_child=lambda child: Path(sys.argv[2]).write_text(str(child.pid)))
                assert result.timed_out
            ''')
            try:
                result = subprocess.run([sys.executable, "-c", helper, str(root), str(pid_file)], capture_output=True, text=True, timeout=4)
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                if pid_file.exists():
                    try:
                        os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sigterm_stops_ignoring_worker_and_produces_stopped_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "worker.pid"
            worker = root / "worker.py"
            worker.write_text("import os, signal, time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath('worker.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
            import shlex

            process = subprocess.Popen([
                sys.executable, "-m", "auto_moniter", "run", "task", "--cwd", str(root),
                "--run-dir", str(root / "run"), "--provider", "command", "--no-review", "--failure-limit", "1",
                "--exec", shlex.join([sys.executable, str(worker)]),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_file.exists())
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, stdout + stderr)
                self.assertEqual(json.loads((root / "run/report.json").read_text())["status"], "stopped")
            finally:
                if pid_file.exists():
                    try:
                        os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_timeout_collects_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_command(PreparedCommand([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"]), Path(tmp), timeout_s=0.3)
            self.assertTrue(result.timed_out)
            self.assertIn("started", result.stdout)
            self.assertNotEqual(result.exit_code, 0)

    def test_invalid_duration_values_and_missing_files_are_cli_errors(self):
        for value in ["-1s", "nan", "inf", "nonsense", "1e999h"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_duration(value)
        self.assertEqual(_parse_duration("1.5m"), 90)
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["run", "-f", "/nonexistent-auto-moniter-requirement.md", "--no-review"]), 2)
        self.assertNotIn("Traceback", error.getvalue())

    def test_resume_does_not_wait_on_stdin(self):
        with patch("auto_moniter.cli.sys.stdin") as stdin, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "--resume", "--no-review"]), 2)
            stdin.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
