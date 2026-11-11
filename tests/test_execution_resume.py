import contextlib
import io
import json
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from auto_moniter.cli import main
from auto_moniter.supervisor import RunConfig, Supervisor, _EXECUTION_DEFAULTS
from auto_moniter.providers import resolve_provider
from tests.test_supervisor import ScriptedProvider, _verdict


class ExecutionResumeTests(unittest.TestCase):
    def test_pause_during_retry_wait_does_not_wait_for_backoff_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            run.mkdir()
            (run / 'control.json').write_text(json.dumps({'action': 'pause'}))
            monitor = Supervisor(RunConfig('task', root, ScriptedProvider('worker', []),
                                           no_review=True, run_dir=run, retry_delay_s=30), log=lambda _: None)
            paused = threading.Event()
            beat = monitor._beat

            def observe(note):
                beat(note)
                if note == 'paused at a turn boundary':
                    paused.set()

            monitor._beat = observe
            thread = threading.Thread(target=monitor._retry_pause, args=(1, 'unfinished turn'))
            thread.start()
            try:
                self.assertTrue(paused.wait(2), 'Pause must be handled before the 30-second retry deadline')
            finally:
                monitor.request_stop()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def checkpoint(self, root, **settings):
        monitor = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                       no_review=True, run_dir=root / "run", **settings), log=lambda _: None)
        monitor.request_stop()
        self.assertEqual(monitor.run().status, "stopped")
        return root / "run"

    def test_all_execution_settings_are_inherited_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = {"turn_timeout_s": 5400, "idle_timeout_s": 0, "retry_delay_s": 2,
                        "check_timeout_s": 3600, "max_output_bytes": 32 * 1024 * 1024}
            run = self.checkpoint(root, **settings)
            resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=run, resume=True), log=lambda _: None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status, "stopped")
            self.assertEqual(json.loads((run / "state.json").read_text())["execution_settings"], settings)

    def test_explicit_default_values_can_override_saved_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.checkpoint(root, idle_timeout_s=0, check_timeout_s=3600, max_output_bytes=1024)
            resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=run, resume=True, **_EXECUTION_DEFAULTS), log=lambda _: None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status, "stopped")
            self.assertEqual(json.loads((run / "state.json").read_text())["execution_settings"], _EXECUTION_DEFAULTS)

    def test_legacy_checkpoint_uses_existing_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.checkpoint(root)
            state = json.loads((run / "state.json").read_text())
            state.pop("execution_settings")
            (run / "state.json").write_text(json.dumps(state))
            resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                           no_review=True, run_dir=run, resume=True), log=lambda _: None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status, "stopped")
            self.assertEqual(json.loads((run / "state.json").read_text())["execution_settings"], _EXECUTION_DEFAULTS)

    def test_corrupt_settings_refuse_resume_without_overwriting_checkpoint(self):
        cases = [None, [], {"idle_timeout_s": -1}, {"check_timeout_s": 0}, {"retry_delay_s": float("nan")},
                 {"max_output_bytes": 1.5}, {"max_output_bytes": True}, {"unknown": 1}]
        for settings in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = self.checkpoint(root)
                state = json.loads((run / "state.json").read_text())
                state["execution_settings"] = settings
                (run / "state.json").write_text(json.dumps(state))
                before = (run / "state.json").read_bytes()
                resumed = Supervisor(RunConfig("task", root, ScriptedProvider("worker", []),
                                               no_review=True, run_dir=run, resume=True), log=lambda _: None)
                self.assertEqual(resumed.run().status, "invalid")
                self.assertEqual((run / "state.json").read_bytes(), before)

    def test_nonempty_function_audit_references_survive_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.checkpoint(root)
            state = json.loads((run/'state.json').read_text())
            state['function_issue_ids'] = ['AUD-1']
            from auto_moniter.audit import AuditLedger
            audit = AuditLedger()
            audit.record(['A focused regression remains'], 1)
            state['audit_issues'] = audit.export()
            (run/'state.json').write_text(json.dumps(state))
            resumed = Supervisor(RunConfig('task',root,ScriptedProvider('worker',[]),
                no_review=True,run_dir=run,resume=True),log=lambda _:None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status,'stopped')
            restored = json.loads((run/'state.json').read_text())
            self.assertEqual(restored['function_issue_ids'],['AUD-1'])
            self.assertEqual(restored['audit_issues'],state['audit_issues'])

    def test_unexpected_load_error_never_overwrites_any_recovery_artifact(self):
        from unittest.mock import patch
        from auto_moniter.health import read_run_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.checkpoint(root)
            names = ('state.json','report.json','handoff.json','baseline.json.gz')
            before = {name:(run/name).read_bytes() for name in names}
            resumed = Supervisor(RunConfig('task',root,ScriptedProvider('worker',[]),
                no_review=True,run_dir=run,resume=True),log=lambda _:None)
            with patch.object(resumed,'_load_resume',side_effect=NameError("name 're' is not defined")):
                self.assertEqual(resumed.run().status,'recovery_failed')
            self.assertEqual({name:(run/name).read_bytes() for name in names},before)
            snapshot = read_run_snapshot(run)
            self.assertEqual(snapshot['state']['status'],'recovery_failed')
            self.assertTrue(snapshot['state']['resume_error'])
            self.assertIn('NameError',snapshot['state']['summary'])
            resumed = Supervisor(RunConfig('task',root,ScriptedProvider('worker',[]),
                no_review=True,run_dir=run,resume=True),log=lambda _:None)
            resumed.request_stop()
            self.assertEqual(resumed.run().status,'stopped')
            self.assertFalse((run/'resume-error.json').exists())

    def test_cli_resume_distinguishes_omission_from_explicit_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            base = ["run", "task", "-C", str(root), "--provider", "command", "--exec", "echo unused",
                    "--no-review", "--run-dir", str(run)]
            # A stop request lets us exercise the real CLI without launching an agent.
            run.mkdir()
            (run / "control.json").write_text(json.dumps({"action": "stop"}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(base + ["--idle-timeout", "0", "--check-timeout", "1h", "--max-output-bytes", "1024"]), 1)
                self.assertEqual(main(base + ["--resume"]), 1)
            settings = json.loads((run / "state.json").read_text())["execution_settings"]
            self.assertEqual(settings["idle_timeout_s"], 0)
            self.assertEqual(settings["check_timeout_s"], 3600)
            self.assertEqual(settings["max_output_bytes"], 1024)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(base + ["--resume", "--idle-timeout", "15m", "--max-output-bytes", "8388608"]), 1)
            settings = json.loads((run / "state.json").read_text())["execution_settings"]
            self.assertEqual(settings["idle_timeout_s"], 900)
            self.assertEqual(settings["max_output_bytes"], 8388608)
            self.assertEqual(settings["check_timeout_s"], 3600)

    def test_inherited_output_limit_is_actually_enforced_after_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdict = _verdict("done", evidence=["app.py"])
            verdict["summary"] = "x" * 2000
            code = f"from pathlib import Path; import json; Path('app.py').write_text('ok'); print(json.dumps({verdict!r}))"
            provider = resolve_provider("command", command=shlex.join([sys.executable, "-c", code]))
            first = Supervisor(RunConfig("task", root, provider, no_review=True, run_dir=root / "run",
                                         max_output_bytes=1024, retry_delay_s=0), log=lambda _: None)
            first.request_stop()
            self.assertEqual(first.run().status, "stopped")
            resumed = Supervisor(RunConfig("task", root, provider, no_review=True, resume=True,
                                           max_rounds=1, run_dir=root / "run"), log=lambda _: None)
            result = resumed.run()
            self.assertEqual(result.status, "max_rounds")
            self.assertEqual(result.records[0].interruption, "output limit")


if __name__ == "__main__":
    unittest.main()
