import contextlib
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.cli import main
from auto_moniter.supervisor import RunConfig, Supervisor

from auto_moniter.providers import (
    extract_session_id,
    extract_text,
    extract_error,
    ProviderError,
    resolve_provider,
)


def _files(tmp: str) -> tuple[Path, Path, Path]:
    root = Path(tmp)
    prompt = root / "prompt.md"
    prompt.write_text("do the work {not_a_field}", encoding="utf-8")
    schema = root / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    return root, prompt, schema


class ProviderParseTests(unittest.TestCase):
    def test_extracts_last_explicit_session(self) -> None:
        blob = "\n".join(
            [
                json.dumps({"type": "tool", "id": "tool-call-id-1234", "session_id": "11111111-1111-1111-1111-111111111111"}),
                json.dumps({"type": "result", "session_id": "22222222-2222-2222-2222-222222222222", "result": "ok"}),
            ]
        )
        self.assertEqual(extract_session_id(blob), "22222222-2222-2222-2222-222222222222")

    def test_ignores_uuid_in_prose(self) -> None:
        blob = "see 33333333-3333-3333-3333-333333333333 in the log"
        self.assertEqual(extract_session_id(blob), "")

    def test_ignores_generic_id(self) -> None:
        blob = json.dumps({"id": "not-a-session", "type": "tool"})
        self.assertEqual(extract_session_id(blob), "")

    def test_tool_status_does_not_override_agent_id(self) -> None:
        agent_id = "502117db-57af-4831-b9a6-552decd651f7"
        tool_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        blob = "\n".join(
            [
                json.dumps({"type": "tool", "id": tool_id, "status": "running", "provider": "shell"}),
                json.dumps({"type": "result", "agentId": agent_id, "status": "idle", "provider": "claude", "result": "final"}),
            ]
        )
        self.assertEqual(extract_session_id(blob), agent_id)

    def test_paseo_text_transcript_keeps_last_section(self) -> None:
        from auto_moniter.providers import _last_transcript_section

        blob = "\n".join(
            [
                "---",
                '{"status":"done","summary":"old"}',
                "---",
                '{"status":"continue","summary":"latest","gaps":["tests"]}',
            ]
        )
        latest = _last_transcript_section(blob)
        self.assertIn("latest", latest)
        self.assertNotIn("old", latest)

    def test_paseo_transcript_uses_assistant_message(self) -> None:
        from auto_moniter.providers import _assistant_texts

        blob = json.dumps(
            [
                {"item": {"type": "tool_call", "text": "ignored"}},
                {"item": {"type": "assistant_message", "text": "real conclusion"}},
            ]
        )
        self.assertEqual(_assistant_texts(blob), ["real conclusion"])

    def test_extracts_paseo_run_result(self) -> None:
        agent_id = "502117db-57af-4831-b9a6-552decd651f7"
        blob = json.dumps(
            {
                "agentId": agent_id,
                "status": "idle",
                "provider": "claude",
                "cwd": "/tmp/proj",
                "title": "auto-moniter:worker:claude",
            }
        )
        self.assertEqual(extract_session_id(blob), agent_id)

    def test_extracts_paseo_agent_id_fields(self) -> None:
        agent_id = "502117db-57af-4831-b9a6-552decd651f7"
        listed = json.dumps({"id": agent_id, "provider": "codex", "status": "idle", "cwd": "~/proj"})
        inspected = json.dumps({"Id": agent_id, "Provider": "codex", "Status": "idle"})
        self.assertEqual(extract_session_id(listed), agent_id)
        self.assertEqual(extract_session_id(inspected), agent_id)

    def test_extracts_last_result_not_longest_tool_output(self) -> None:
        blob = "\n".join(
            [
                json.dumps({"type": "tool", "result": "x" * 80}),
                json.dumps({"type": "result", "result": "{\"status\":\"continue\"}"}),
            ]
        )
        text, structured = extract_text(blob)
        self.assertIn("continue", text)
        self.assertNotIn("x" * 20, text)
        self.assertTrue(structured)

    def test_plain_text_passes_through(self) -> None:
        text, structured = extract_text("hello")
        self.assertEqual(text, "hello")
        self.assertFalse(structured)

    def test_paseo_roles_are_distinct_instances(self) -> None:
        worker = resolve_provider("paseo", paseo_backend="codex", paseo_mode="full-access", role="worker")
        reviewer = resolve_provider("paseo", paseo_backend="claude", paseo_mode="plan", role="reviewer")
        self.assertIsNot(worker, reviewer)
        self.assertEqual(worker.backend, "codex")
        self.assertEqual(reviewer.role, "reviewer")

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(Exception):
            resolve_provider("workbuddy")

    def test_command_template_keeps_braces_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            provider = resolve_provider(
                "command",
                command="echo {prompt_file} {session_id}",
                resume_command="echo resume {session_id} {prompt}",
            )
            rendered = provider.resume_command("agent_123", prompt, cwd, schema).argv[-1]
            self.assertIn("agent_123", rendered)
            self.assertNotIn("{session_id}", rendered)
            self.assertIn("not_a_field", rendered)


class CommandShapeTests(unittest.TestCase):
    def test_claude_prompt_goes_to_stdin_and_review_is_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            provider = resolve_provider("claude")
            start = provider.start_command(prompt, cwd, schema)
            review = provider.start_command(prompt, cwd, schema, read_only=True)
            resume = provider.resume_command(
                "11111111-1111-1111-1111-111111111111", prompt, cwd, schema
            )
        self.assertNotIn("do the work", start.argv)
        self.assertIn("{not_a_field}", start.stdin_text or "")
        self.assertIn("plan", review.argv)
        self.assertNotIn("--dangerously-skip-permissions", review.argv)
        self.assertIn("--resume", resume.argv)
        self.assertIn("11111111-1111-1111-1111-111111111111", resume.argv)

    def test_codex_resume_keeps_workdir_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            provider = resolve_provider("codex")
            resume = provider.resume_command("22222222-2222-2222-2222-222222222222", prompt, cwd, schema)
            review = provider.start_command(prompt, cwd, schema, read_only=True)
        self.assertEqual(resume.argv[1:4], ["exec", "resume", "22222222-2222-2222-2222-222222222222"])
        self.assertNotIn("-C", resume.argv)
        self.assertNotIn(str(cwd), resume.argv)
        self.assertEqual(resume.argv[-1], "-")
        self.assertIn("read-only", review.argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", review.argv)

    def test_grok_is_single_turn_and_file_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            provider = resolve_provider("grok")
            start = provider.start_command(prompt, cwd, schema, read_only=True)
        self.assertIn("--prompt-file", start.argv)
        self.assertNotIn("-p", start.argv)
        self.assertIn("plan", start.argv)
        self.assertNotIn("do the work", start.argv)

    def test_paseo_send_uses_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            provider = resolve_provider("paseo", paseo_backend="claude", role="worker")
            start = provider.start_command(prompt, cwd, schema)
            resume = provider.resume_command("agent_abc", prompt, cwd, schema)
            long_prompt = cwd / "long.md"
            long_prompt.write_text("x" * 120_000, encoding="utf-8")
            long_start = provider.start_command(long_prompt, cwd, schema)
            self.assertIn(".auto-moniter/inbox-prompt.md", long_start.argv[-1])
            self.assertTrue((cwd / ".auto-moniter" / "inbox-prompt.md").exists())
        self.assertNotIn("--output-schema", start.argv)
        self.assertIn("--no-wait", start.argv)
        self.assertIn("--no-wait", resume.argv)
        self.assertIn("auto-moniter:worker:claude", start.argv)
        self.assertIn("--prompt-file", resume.argv)
        self.assertIn(str(prompt), resume.argv)


class ModeCompatibilityTests(unittest.TestCase):
    def test_original_codex_paseo_bypass_command_uses_full_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            for backend in ["codex", "codex/gpt-5.4"]:
                for mode in ["bypass", "bypassPermissions", "yolo", "full-access", " FULL_ACCESS "]:
                    with self.subTest(backend=backend, mode=mode):
                        cmd = resolve_provider("paseo", paseo_backend=backend, paseo_mode=mode).start_command(prompt, cwd, schema).argv
                        self.assertEqual(cmd[cmd.index("--mode") + 1], "full-access")
                        self.assertEqual(cmd[cmd.index("--provider") + 1], backend)

    def test_claude_paseo_translates_native_permission_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            for mode, expected in [("full-access", "bypass"), ("bypassPermissions", "bypass"), ("accept-edits", "acceptEdits"), ("read-only", "plan"), ("auto", "auto")]:
                with self.subTest(mode=mode):
                    cmd = resolve_provider("paseo", paseo_backend="claude", mode=mode).start_command(prompt, cwd, schema).argv
                    self.assertEqual(cmd[cmd.index("--mode") + 1], expected)

    def test_native_permission_modes_survive_start_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            for name in ["claude", "grok"]:
                for mode, expected in [("bypass", "bypassPermissions"), ("full-access", "bypassPermissions"), ("accept-edits", "acceptEdits"), ("dontAsk", "dontAsk"), ("read-only", "plan")]:
                    with self.subTest(provider=name, mode=mode):
                        provider = resolve_provider(name, mode=mode)
                        for cmd in [provider.start_command(prompt, cwd, schema), provider.resume_command("session", prompt, cwd, schema)]:
                            self.assertEqual(cmd.argv[cmd.argv.index("--permission-mode") + 1], expected)
                            approval_flag = "--always-approve" if name == "grok" else "--dangerously-skip-permissions"
                            self.assertEqual(approval_flag in cmd.argv, expected == "bypassPermissions")

    def test_codex_plan_and_auto_sandbox_flags_work_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            for mode, sandbox in [("plan", "read-only"), ("auto", "workspace-write")]:
                with self.subTest(mode=mode):
                    provider = resolve_provider("codex", mode=mode)
                    start = provider.start_command(prompt, cwd, schema).argv
                    resume = provider.resume_command("session", prompt, cwd, schema).argv
                    for cmd in [start, resume]:
                        self.assertEqual(cmd[cmd.index("--sandbox") + 1], sandbox)
                        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
                    self.assertLess(resume.index("--sandbox"), resume.index("resume"))

    def test_paseo_review_defaults_match_backend_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            for backend, expected in [("codex", "auto"), ("claude", "plan"), ("grok", None), ("custom-acp", None)]:
                with self.subTest(backend=backend):
                    provider = resolve_provider("paseo", paseo_backend=backend, role="reviewer")
                    cmd = provider.start_command(prompt, cwd, schema, read_only=True).argv
                    if expected:
                        self.assertEqual(cmd[cmd.index("--mode") + 1], expected)
                    else:
                        self.assertNotIn("--mode", cmd)

    def test_grok_acp_uses_default_and_reports_unsupported_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            for mode in ["", "default"]:
                cmd = resolve_provider("paseo", paseo_backend="grok", mode=mode).start_command(prompt, cwd, schema).argv
                self.assertNotIn("--mode", cmd)
        with self.assertRaisesRegex(ProviderError, "--provider grok --mode bypass"):
            resolve_provider("paseo", paseo_backend="grok", mode="bypass")

    def test_custom_paseo_mode_ids_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, prompt, schema = _files(tmp)
            cmd = resolve_provider("paseo", paseo_backend="custom/model", mode="CustomMode").start_command(prompt, cwd, schema).argv
            self.assertEqual(cmd[cmd.index("--mode") + 1], "CustomMode")

    def test_invalid_or_incompatible_modes_fail_before_supervisor_starts(self):
        for mode_args in [
            ["--provider", "codex", "--mode", "typo"],
            ["--provider", "paseo", "--paseo-backend", "codex", "--paseo-mode", "plan"],
            ["--provider", "paseo", "--mode", "plan", "--paseo-mode", "bypass"],
            ["--provider", "command", "--exec", "echo task", "--mode", "plan"],
        ]:
            with self.subTest(args=mode_args), tempfile.TemporaryDirectory() as tmp:
                with patch("auto_moniter.cli.Supervisor") as supervisor, contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(["run", "--cwd", tmp, "--no-review", *mode_args, "task"]), 2)
                    supervisor.assert_not_called()
                self.assertTrue(error.getvalue().strip())
                self.assertFalse((Path(tmp) / ".auto-moniter").exists())

    def test_cli_passes_worker_and_reviewer_modes_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("auto_moniter.cli.Supervisor") as supervisor, contextlib.redirect_stdout(io.StringIO()):
                supervisor.return_value.run.return_value.status = "done"
                supervisor.return_value.run.return_value.stop_reason = "done"
                supervisor.return_value.run.return_value.rounds = 1
                supervisor.return_value.run.return_value.summary = "complete"
                result = main(["run", "--cwd", tmp, "--provider", "paseo", "--paseo-backend", "codex", "--mode", "bypass", "--reviewer", "grok", "--reviewer-mode", "plan", "task"])
                self.assertEqual(result, 0)
                config = supervisor.call_args.args[0]
                self.assertEqual(config.provider.mode, "full-access")
                self.assertEqual(config.reviewer.mode, "plan")


class ErrorDetailTests(unittest.TestCase):
    def test_paseo_pretty_error_after_workspace_preamble_keeps_message(self):
        blob = 'Created workspace wks_123\n' + json.dumps({"error": {"code": "AGENT_CREATE_FAILED", "message": "Invalid mode 'bypass' for provider 'codex'. Available modes: auto, full-access"}}, indent=2)
        detail = extract_error(blob)
        self.assertIn("AGENT_CREATE_FAILED", detail)
        self.assertIn("Invalid mode 'bypass'", detail)
        self.assertNotEqual(detail, "}")

    def test_json_error_on_stdout_is_preferred_over_stderr_progress(self):
        detail = extract_error("Creating workspace...", json.dumps({"type": "error", "message": "Invalid mode", "code": "BAD_MODE"}))
        self.assertEqual(detail, "BAD_MODE: Invalid mode")

    def test_failure_console_and_state_keep_the_real_error(self):
        error = json.dumps({"error": {"code": "AGENT_CREATE_FAILED", "message": "Invalid mode 'bypass' for provider 'codex'"}}, indent=2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = f"import sys; print('Created workspace wks_123', file=sys.stderr); print({error!r}, file=sys.stderr); sys.exit(1)"
            provider = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            messages = []
            report = Supervisor(RunConfig("task", root, provider, no_review=True, failure_limit=1, retry_delay_s=0, run_dir=root / "run"), log=messages.append).run()
            self.assertEqual(report.status, "failed")
            self.assertIn("AGENT_CREATE_FAILED", "\n".join(messages))
            self.assertIn("Invalid mode 'bypass'", report.records[0].verdict.gaps[0])
            state = json.loads((root / "run/state.json").read_text())
            self.assertIn("AGENT_CREATE_FAILED", state["rounds"][0]["failure"])
            self.assertIn(error, (root / "run/001-worker.log").read_text())


if __name__ == "__main__":
    unittest.main()
