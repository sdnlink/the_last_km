import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from auto_moniter.providers import PreparedCommand, Provider
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import workspace_fingerprint


class ScriptedProvider(Provider):
    def __init__(self, name: str, scripts: list[dict]) -> None:
        super().__init__(name, "scripted", True)
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        return self._command("", prompt_file, read_only)

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        return self._command(session_id, prompt_file, read_only)

    def _command(self, session_id: str, prompt_file: Path, read_only: bool) -> PreparedCommand:
        if not self.scripts:
            raise AssertionError(f"{self.name} has no scripted turns left")
        script = self.scripts.pop(0)
        prompt = prompt_file.read_text(encoding="utf-8")
        self.calls.append({"session_id": session_id, "prompt": prompt, "read_only": read_only})
        payload = {
            "type": "result",
            "session_id": script.get("session_id", f"{self.name}-session"),
            "status": "idle",
            "provider": self.name,
            "result": script.get("text", json.dumps(script["verdict"])),
        }
        code = script.get("exit_code", 0)
        script_path = prompt_file.with_suffix(".script.py")
        evidence = script.get("touch")
        touch_line = ""
        if evidence:
            touch_line = f"Path({str(prompt_file.parent.parent)!r}, {evidence!r}).write_text('changed\\n', encoding='utf-8')"
        script_path.write_text(
            textwrap.dedent(
                f"""
                import json, sys
                from pathlib import Path
                {touch_line}
                print(json.dumps({payload!r}))
                sys.exit({code})
                """
            ),
            encoding="utf-8",
        )
        return PreparedCommand([sys.executable, str(script_path)])


def _verdict(
    status: str,
    gaps: list[str] | None = None,
    actions: list[str] | None = None,
    evidence: list[str] | None = None,
    scope: str = "requirement",
    closed: list[str] | None = None,
) -> dict:
    return {
        "status": status,
        "scope": scope,
        "modules": [{"id": "main", "title": "Original requirement", "acceptance": ["All original behavior and checks pass"]}],
        "module_ids": ["main"],
        "summary": status,
        "done": [],
        "gaps": gaps or [],
        "next_actions": actions or [],
        "evidence": ["script"] if evidence is None else evidence,
        "blockers": ["need a secret"] if status == "blocked" else [],
        "issue_resolutions": [{"id": ident, "summary": "Independently verified the correction",
                               "evidence": evidence or ["app.py"]} for ident in closed or []],
    }


class SupervisorTests(unittest.TestCase):
    def test_reviewer_sends_worker_back_until_accepted(self) -> None:
        worker = ScriptedProvider(
            "worker",
            [
                {"verdict": _verdict("done", evidence=["app.py"]), "session_id": "11111111-1111-1111-1111-111111111111", "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"]), "session_id": "11111111-1111-1111-1111-111111111111"},
            ],
        )
        reviewer = ScriptedProvider(
            "reviewer",
            [
                {"verdict": _verdict("continue", ["missing tests"], ["add tests"])},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Add a health endpoint.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "done")
        self.assertEqual(report.rounds, 2)
        self.assertEqual(worker.calls[1]["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertIn("missing tests", worker.calls[1]["prompt"])
        self.assertEqual(reviewer.calls[0]["session_id"], "")
        self.assertEqual(reviewer.calls[1]["session_id"], "")
        self.assertTrue(reviewer.calls[0]["read_only"])
        self.assertFalse(worker.calls[0]["read_only"])

    def test_done_without_evidence_is_not_accepted(self) -> None:
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=[])}])
        reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=[])}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Ship it.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    max_rounds=1,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "max_rounds")
        self.assertEqual(report.records[-1].verdict.status, "continue")
        self.assertIn("without evidence", " ".join(report.records[-1].verdict.gaps))

    def test_stall_requires_unchanged_tree(self) -> None:
        same = _verdict("continue", ["same gap"], ["retry"])
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done")} for _ in range(4)])
        reviewer = ScriptedProvider("reviewer", [{"verdict": same} for _ in range(4)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "marker.txt"
            marker.write_text("0", encoding="utf-8")

            def changing_log(message: str) -> None:
                if message.startswith("round ") and "worker running" in message:
                    marker.write_text(str(int(marker.read_text(encoding="utf-8")) + 1), encoding="utf-8")

            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Ship it.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    stall_limit=2,
                    max_rounds=4,
                    run_dir=root / "run",
                ),
                log=changing_log,
            ).run()
        self.assertEqual(report.status, "max_rounds")
        self.assertEqual(report.rounds, 4)

    def test_stall_stops_when_tree_is_unchanged(self) -> None:
        same = _verdict("continue", ["same gap"], ["retry"])
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done")} for _ in range(4)])
        reviewer = ScriptedProvider("reviewer", [{"verdict": same} for _ in range(4)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Ship it.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    stall_limit=2,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "stalled")
        self.assertLess(report.rounds, 4)

    def test_worker_blocker_needs_reviewer_confirmation(self) -> None:
        worker = ScriptedProvider(
            "worker",
            [
                {"verdict": _verdict("blocked"), "session_id": "11111111-1111-1111-1111-111111111111"},
                {"verdict": _verdict("done", evidence=["app.py"]), "session_id": "11111111-1111-1111-1111-111111111111", "touch": "app.py"},
            ],
        )
        reviewer = ScriptedProvider(
            "reviewer",
            [
                {"verdict": _verdict("continue", ["blocker is not external"], ["keep going"])},
                {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Deploy.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "done")
        self.assertEqual(len(reviewer.calls), 2)

    def test_reviewer_alone_cannot_block(self) -> None:
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])}, {"verdict": _verdict("done", evidence=["app.py"])}])
        reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("blocked")}, {"verdict": _verdict("continue", ["still open"], ["keep going"])}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Deploy.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    max_rounds=2,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "max_rounds")
        self.assertEqual(len(reviewer.calls), 2)

    def test_done_without_real_file_is_rejected(self) -> None:
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])}])
        reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["looks good"])}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Add health.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    max_rounds=1,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "max_rounds")
        self.assertTrue(any("does not name a workspace file" in gap for gap in report.records[-1].verdict.gaps))

    def test_same_gaps_do_not_stop_by_default(self) -> None:
        same = _verdict("continue", ["same gap"], ["retry"])
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"])} for _ in range(3)])
        reviewer = ScriptedProvider("reviewer", [{"verdict": same} for _ in range(3)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Ship it.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    max_rounds=3,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "max_rounds")
        self.assertEqual(report.rounds, 3)

    def test_reviewer_confirmed_blocker_continues_until_deliverable(self) -> None:
        worker = ScriptedProvider("worker", [
            {"verdict": _verdict("blocked")},
            {"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"},
        ])
        reviewer = ScriptedProvider("reviewer", [
            {"verdict": _verdict("blocked")},
            {"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Deploy.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "done")
        self.assertEqual(report.rounds, 2)
        self.assertIn("need a secret", worker.calls[1]["prompt"])

    def test_process_failure_retries_then_stops(self) -> None:
        worker = ScriptedProvider(
            "worker",
            [{"verdict": _verdict("done"), "exit_code": 1} for _ in range(3)],
        )
        reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done")}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Keep going.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    failure_limit=2,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "failed")
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(worker.calls[1]["session_id"], "")

    def test_resume_uses_saved_session(self) -> None:
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done", evidence=["app.py"]), "touch": "app.py"}])
        reviewer = ScriptedProvider("reviewer", [{"verdict": _verdict("done", evidence=["app.py"], closed=["AUD-1"])}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "requirement.md").write_text("Add a health endpoint.\n", encoding="utf-8")
            state = {
                "status": "running",
                "session_id": "11111111-1111-1111-1111-111111111111",
                "reviewer_session_id": "",
                "next_round": 2,
                "stall": 0,
                "gap_key": "missing tests",
                "tree": "old",
                "last_worker": _verdict("done"),
                "last_review": _verdict("continue", ["missing tests"], ["add tests"]),
                "rounds": [],
            }
            (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Add a health endpoint.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    run_dir=run_dir,
                    resume=True,
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "done")
        self.assertEqual(worker.calls[0]["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertIn("missing tests", worker.calls[0]["prompt"])

    def test_reviewer_parse_failure_does_not_replay_worker_gaps(self) -> None:
        worker = ScriptedProvider(
            "worker",
            [
                {"verdict": _verdict("done", evidence=["app.py"]), "session_id": "11111111-1111-1111-1111-111111111111", "touch": "app.py"},
                {"verdict": _verdict("done", evidence=["app.py"]), "session_id": "11111111-1111-1111-1111-111111111111"},
            ],
        )
        reviewer = ScriptedProvider(
            "reviewer",
            [
                {"verdict": _verdict("done"), "exit_code": 2},
                {"verdict": _verdict("done", evidence=["app.py"])},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="Add health.",
                    cwd=root,
                    provider=worker,
                    reviewer=reviewer,
                    failure_limit=3,
                    run_dir=root / "run",
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "done")
        self.assertNotIn("missing tests", worker.calls[1]["prompt"])
        self.assertIn("review did not finish", worker.calls[1]["prompt"])

    def test_resume_refuses_finished_run(self) -> None:
        worker = ScriptedProvider("worker", [{"verdict": _verdict("done")}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "requirement.md").write_text("old requirement\n", encoding="utf-8")
            (run_dir / "state.json").write_text(
                json.dumps({"status": "failed", "next_round": 4, "session_id": "11111111-1111-1111-1111-111111111111"}),
                encoding="utf-8",
            )
            report = Supervisor(
                RunConfig(
                    retry_delay_s=0,
                    requirement="new requirement",
                    cwd=root,
                    provider=worker,
                    no_review=True,
                    run_dir=run_dir,
                    resume=True,
                ),
                log=lambda _message: None,
            ).run()
        self.assertEqual(report.status, "failed")
        self.assertEqual(worker.calls, [])
        self.assertIn("already finished", report.summary)

    def test_fingerprint_changes_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a.txt"
            target.write_text("one", encoding="utf-8")
            before = workspace_fingerprint(root)
            target.write_text("two", encoding="utf-8")
            after = workspace_fingerprint(root)
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
