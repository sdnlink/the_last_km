import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

from auto_moniter import __version__
from auto_moniter.adaptive_recovery import RecoveryJournal
from auto_moniter.cli import build_parser, main
from auto_moniter.console_watch import MAX_RESPONSE_BYTES, snapshot as console_snapshot
from auto_moniter.events import record as record_event
from auto_moniter.failures import classify
from auto_moniter.health import run_health
from auto_moniter.live import LiveOutput
from auto_moniter.metering import UsageMeter
from auto_moniter.quality import Quality
from auto_moniter.quota import history_roots
from auto_moniter.redaction import redact, register_secrets, safe
from auto_moniter.referee import Journal as RefereeJournal
from auto_moniter.runlog import diagnostic_summary
from auto_moniter.storage import write_json
from console.acceptance import preflight as acceptance_preflight
from console.activity import ActivityReader
from console.diagnostics import diagnose
from console.dispatch import page as dispatch_page
from console.localization import localize_response, prefixes, translate
from console.logs import LogReader
from console.migration import build as build_migration
from console.pages import render_page
from console.roles import prepare as prepare_roles
from console.server import ConsoleServer, build_parser as build_console_parser
from console.workflow import work_details


class StorageAndCliImportTests(unittest.TestCase):
    def test_write_json_sets_mode_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.json"
            write_json(path, {"api_key": "value"}, durable=True, mode=0o600)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"api_key": "value"})
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_cli_and_console_parsers_load_and_report_version(self):
        self.assertEqual(build_parser().prog, "auto-moniter")
        self.assertIsNotNone(build_console_parser())
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exited:
            main(["--version"])
        self.assertEqual(exited.exception.code, 0)
        self.assertIn(__version__, output.getvalue())

    def test_doctor_returns_structured_platform_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["doctor", "--cwd", tmp])
            report = json.loads(output.getvalue())
            self.assertEqual(report["version"], __version__)
            self.assertEqual(report["checks"]["linux"], sys.platform == "linux")
            self.assertEqual(code, 0 if report["supported"] else 2)

    def test_referee_activity_and_role_card_localize_accurately(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "events.jsonl").write_text(
                json.dumps({"at": "2026-11-22T12:00:00Z", "event": "agent_start", "role": "referee", "round": 4}) + "\n",
                encoding="utf-8",
            )
            snap = ActivityReader().snapshot(run, {"cwd": tmp}, {"phase": "refereeing"}, "running", "codex")
            phase_events = [e for e in snap["events"] if e.get("console_event") == "agent_start"]
            self.assertEqual(len(phase_events), 1)
            self.assertIn("裁判开始工作", phase_events[0]["text"])

            details = work_details(
                {"referee": {"enabled": True, "model": "gpt-ref"}},
                {"phase": "working", "activity": "round 4: worker"},
                snap,
                "running",
                {},
                {},
                "codex",
                "claude",
            )
            localized = localize_response({"work": details}, "en", "/api/tasks/abc")["work"]
            self.assertEqual(localized["roles"]["referee"]["current_work"],
                             "Intervenes only for interruptions, repeated failures or stalls.")
            self.assertEqual(localized["roles"]["referee"]["wait_reason"],
                             "Intervenes only for interruptions, repeated failures or stalls.")
            localized_starts = [e for e in localized["workflow"] if e.get("console_event") == "agent_start"]
            self.assertEqual(localized_starts[0]["display_text"], "Round 4 · Referee started")

            # AI-authored referee summary must remain verbatim while generated current_work translates.
            details_with_decision = work_details(
                {"referee": {"enabled": True, "model": "gpt-ref", "decision": {"summary": "保留候选并重试审核"}}},
                {"phase": "refereeing", "activity": "round 4: referee"},
                snap,
                "running",
                {},
                {},
                "codex",
                "claude",
            )
            localized_active = localize_response({"work": details_with_decision}, "en", "/api/tasks/abc")["work"]
            self.assertEqual(localized_active["roles"]["referee"]["current_work"],
                             "The referee is diagnosing interruptions and stalls and arranging the remaining plan.")
            self.assertEqual(localized_active["roles"]["referee"]["wait_reason"], "保留候选并重试审核")

    def test_localization_prefix_cache_and_ascii_fast_path(self):
        self.assertGreater(len(prefixes()), 0)
        lengths = [len(item[0]) for item in prefixes()]
        self.assertEqual(lengths, sorted(lengths, reverse=True))
        self.assertEqual(translate("plain-ascii-id-12345", "en"), "plain-ascii-id-12345")
        self.assertEqual(translate("", "en"), "")

    def test_redaction_precompiled_and_thread_safe(self):
        secret = "sk-test-secret-9876543210"
        errors = []

        def worker():
            try:
                for _ in range(50):
                    register_secrets([secret])
                    out = safe({"msg": f"Bearer abc123 token={secret} https://u:p@example.com/x api_key=xyz999"})
                    if secret in out["msg"] or "abc123" in out["msg"] or "u:p@" in out["msg"] or "xyz999" in out["msg"]:
                        errors.append(out["msg"])
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(redact(""), "")

    def test_console_post_accepts_case_insensitive_json_content_type(self):
        store = SimpleNamespace(
            cwd=Path("."),
            data_dir=Path("."),
            agents=SimpleNamespace(initialized=lambda: True),
            refresh_agents=lambda: {"ok": True},
        )
        with ConsoleServer(("127.0.0.1", 0), store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request(
                    "POST",
                    "/api/agents/refresh",
                    body=b"{}",
                    headers={
                        "Content-Type": "Application/JSON; charset=utf-8",
                        "X-Console-Token": server.csrf,
                    },
                )
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                conn.close()
                self.assertEqual(resp.status, 200)
                self.assertEqual(body, {"ok": True})
            finally:
                server.shutdown()
                thread.join(3)

    def test_utf8_journals_roundtrip_chinese_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            recovery = RecoveryJournal(run)
            self.assertTrue(recovery.admit({"step": "计划"}, "缺少模块验证：需要补充边界断言"))
            self.assertEqual(recovery.read()["episodes"][0]["reasons"][0], "缺少模块验证：需要补充边界断言")

            journal = RefereeJournal(run)
            item = journal.begin({"task": "中文任务"}, "连续失败需要诊断")
            self.assertEqual(item["reason"], "连续失败需要诊断")
            self.assertEqual(journal.read()["decisions"][0]["reason"], "连续失败需要诊断")

    def test_log_reader_concurrent_reads_and_cached_page_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            log_path.write_text("\n".join(f"line-{i} password=secret-{i}" for i in range(400)) + "\n", encoding="utf-8")
            reader = LogReader()
            errors = []

            def read_pages():
                try:
                    for page in (1, 5, 10, 0):
                        result = reader.read(log_path, page, 20)
                        if result["total_lines"] != 400 or "secret-" in result["content"]:
                            errors.append(result)
                except Exception as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=read_pages) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])

        first = render_page("detail", "a" * 32, "logs", Path("."), language="en").decode("utf-8")
        second = render_page("detail", "b" * 32, "logs", Path("."), language="en").decode("utf-8")
        self.assertIn(f'/tasks/{"a" * 32}/logs', first)
        self.assertIn(f'/tasks/{"b" * 32}/logs', second)
        self.assertNotIn("a" * 32, second)

    def test_acceptance_preflight_handles_damaged_or_missing_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = {
                "cwd": str(root),
                "python": sys.executable,
                "selected": [{
                    "id": "npm-test",
                    "title": "npm test",
                    "executable": sys.executable,
                    "command": "npm test",
                    "dependencies": [],
                }],
            }
            missing = acceptance_preflight(plan)
            self.assertFalse(missing["ready"])
            self.assertIn("无法读取 package.json", missing["checks"][0]["errors"])

            (root / "package.json").write_text("[]", encoding="utf-8")
            non_object = acceptance_preflight(plan)
            self.assertFalse(non_object["ready"])
            self.assertIn("package.json 格式无效", non_object["checks"][0]["errors"])

    def test_referee_diagnostics_phase_and_english_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "summary": "referee decision rejected",
                "rounds": [{"index": 3, "role": "referee", "failure": "invalid decision"}],
            }
            diag = diagnose("action_required", state, {}, {"health": "stopped"}, {}, Path(tmp))
            self.assertEqual(diag["role"], "referee")
            self.assertEqual(diag["phase"], "裁判诊断")
            localized = localize_response({"failure": diag}, "en", "/api/tasks/abc")["failure"]
            self.assertEqual(localized["phase"], "Referee diagnosis")

    def test_verification_blocker_has_chinese_reason_and_preserves_original_log(self):
        raw = 'Verification needs an external prerequisite before it can continue. AUD-3：夹具改动尚未验证。'
        diag = diagnose('action_required', {'summary': raw}, {}, {}, {}, Path('.'))
        self.assertEqual(diag['code'], 'verification_waiting')
        self.assertEqual(diag['reason'], '验收受阻，补齐以下条件后可继续。AUD-3：测试准备的改动尚未验证。')
        self.assertEqual(diag['raw_summary'], raw)

    def test_console_watch_rejects_invalid_json_and_oversized_body(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class RawHandler(BaseHTTPRequestHandler):
            payload = b"not-json"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(self.payload)))
                self.end_headers()
                self.wfile.write(self.payload)

            def log_message(self, *_args):
                pass

        with HTTPServer(("127.0.0.1", 0), RawHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaisesRegex(ValueError, "invalid JSON"):
                    console_snapshot(server.server_port)
                RawHandler.payload = b"x" * (MAX_RESPONSE_BYTES + 2)
                with self.assertRaisesRegex(ValueError, "exceeds size limit"):
                    console_snapshot(server.server_port)
            finally:
                server.shutdown()
                thread.join(3)

    def test_migration_handles_missing_or_invalid_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            meta = {"id": "a" * 32, "title": "demo", "cwd": tmp, "requirement": "do work", "max_rounds": 10}
            state = {"usage": {"max_runtime_s": 3600, "elapsed_s": 60, "max_agent_calls": 20, "agent_calls": 2},
                     "next_round": None, "modules": []}
            payload, _, _, _ = build_migration(meta, state, run, "do work")
            self.assertEqual(payload["max_rounds"], 10)
            state["next_round"] = "bad"
            with self.assertRaisesRegex(ValueError, "原任务的轮次记录无效"):
                build_migration(meta, state, run, "do work")

    def test_roles_prepare_rejects_bad_cli_without_sys_exit_and_strips_short_provider_flag(self):
        with self.assertRaises(ValueError):
            prepare_roles(["run", "--unknown-flag-xyz"], {"provider": "codex"}, Path("."))
        updated, values, _ = prepare_roles(
            ["run", "-p", "codex", "--reviewer", "claude", "--cwd", ".", "do work"],
            {"provider": "grok", "reviewer": "claude"},
            Path("."),
        )
        self.assertEqual(values["provider"], "grok")
        self.assertNotIn("-p", updated)
        self.assertIn("--provider=grok", updated)

    def test_usage_meter_copies_saved_state_rejects_non_dict_and_skips_plain_lines(self):
        saved = {"tokens": 10, "cost_usd": 0.1, "reported_turns": 1, "unreported_turns": 0}
        meter = UsageMeter(saved)
        meter.record("plain text line\n  more prose\n" + json.dumps({"type": "result", "usage": {"input_tokens": 3, "output_tokens": 4}, "total_cost_usd": 0.05}))
        self.assertEqual(saved["tokens"], 10)  # Caller dict not mutated.
        self.assertEqual(meter.state["tokens"], 17)
        self.assertEqual(UsageMeter({}).state["tokens"], 0)
        for bad in ([], "", 0, False):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                UsageMeter(bad)
        self.assertEqual(classify("HTTP 429 rate limit exceeded"), ("rate_limit", "backoff"))
        self.assertEqual(classify("401 Unauthorized"), ("authentication", "stop"))

    def test_dispatch_page_tolerates_null_modules_and_cycles(self):
        result = dispatch_page({"function_dispatch": True, "modules": None, "requirement_cycles": None})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["plan_state"], "not_generated")

    def test_health_events_runlog_and_live_handle_edge_cases_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            state = {"status": "running", "rounds": None, "records": ["bad", {"role": "worker"}],
                     "modules": ["bad", {"status": "accepted"}, {"id": "API", "status": "accepted"}]}
            (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
            health = run_health(run)
            self.assertEqual(health["rounds"], 1)
            self.assertEqual(health["accepted_modules"], ["API"])

            record_event(run, "check", detail="Bearer tok12345678", note="password=mysecret")
            saved_event = json.loads((run / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertNotIn("tok12345678", saved_event["detail"])
            self.assertNotIn("mysecret", saved_event["note"])

            live = LiveOutput(run, "worker", 1, "codex", announce=False)
            live.line("plain output line", 0)
            live.line("  not json line", 1)
            live.line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done msg"}}), 0)
            live.close()
            entries = [json.loads(line) for line in (run / "live.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([e["kind"] for e in entries], ["output", "error", "message"])

        summary = diagnostic_summary("", "fatal: boom\n", failed=False)
        self.assertIn("fatal: boom", summary)
        json_stderr = json.dumps({"error": "rate limited"}) + "\n"
        self.assertIn("rate limited", diagnostic_summary("", json_stderr, failed=True))

    def test_quality_copies_state_rejects_falsy_non_dict_and_handles_bad_shlex(self):
        saved = {"milestones": ["m1"], "checks": {"cmd": True}, "blocker_key": "", "blocker_count": 0}
        quality = Quality({}, {}, saved)
        quality.milestone("m2")
        self.assertEqual(saved["milestones"], ["m1"])
        self.assertEqual(Quality({}, {}, {}).state["milestones"], [])
        for bad in ([], "", 0, False):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Quality({}, {}, bad)
        with tempfile.TemporaryDirectory() as tmp:
            quality.bind_checks(Path(tmp), ["echo 'unterminated quote"])
            self.assertEqual(quality.command_assets, set())

    def test_quota_history_roots_tolerates_missing_git(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("auto_moniter.quota.subprocess.run", side_effect=FileNotFoundError("git")):
                self.assertEqual(history_roots(root), [root / ".auto-moniter/history"])


if __name__ == "__main__":
    unittest.main()
