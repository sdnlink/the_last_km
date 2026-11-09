"""issue_resolutions schema and specific validation feedback for agents."""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from auto_moniter.audit import resolutions
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.verdict import VERDICT_SCHEMA, parse_verdict, verdict_instructions
from tests.test_supervisor import ScriptedProvider, _verdict


def _base(**extra):
    data = {
        "status": "done",
        "scope": "module",
        "module_ids": ["CYCLE-1"],
        "modules": [{"id": "CYCLE-1", "title": "m", "acceptance": ["a"], "depends_on": []}],
        "summary": "close issues",
        "done": ["item"],
        "gaps": [],
        "next_actions": [],
        "evidence": ["app.py"],
        "blockers": [],
        "issue_resolutions": [],
    }
    data.update(extra)
    return data


def _closure(ident="AUD-1", summary="Verified the fix independently", evidence=None):
    return {"id": ident, "summary": summary,
            "evidence": ["app.py"] if evidence is None else evidence}


def _section(prompt: str, heading: str) -> str:
    """Extract text under a prompt heading until the next blank-line-separated title-like line."""
    marker = heading
    start = prompt.find(marker)
    if start < 0:
        return ""
    rest = prompt[start + len(marker):]
    # Stop before the fixed instructions block so template text cannot satisfy assertions.
    for stop in ("\nWhen this turn ends,", "\nField rules:", "\nWorkspace snapshot:",
                 "\nAccepted module milestones:", "\nOpen audit issues"):
        at = rest.find(stop)
        if at >= 0:
            rest = rest[:at]
            break
    return rest


class ResolutionSchemaTests(unittest.TestCase):
    def test_schema_documents_resolution_bounds(self):
        field = VERDICT_SCHEMA["properties"]["issue_resolutions"]
        self.assertEqual(field["maxItems"], 128)
        item = field["items"]
        self.assertEqual(item["properties"]["id"]["pattern"], r"^AUD-[1-9][0-9]{0,11}$")
        self.assertGreaterEqual(item["properties"]["summary"]["minLength"], 1)
        evidence = item["properties"]["evidence"]
        self.assertEqual(evidence["minItems"], 1)
        self.assertEqual(evidence["maxItems"], 4)
        self.assertGreaterEqual(evidence["items"]["minLength"], 1)
        text = verdict_instructions()
        self.assertIn("1 to 4", text)
        self.assertIn("AUD-[1-9]", text)
        self.assertIn("Issue resolution needs 1-4 evidence strings", text)

    def test_valid_one_and_four_evidence_strings_parse_as_done(self):
        for count in (1, 4):
            with self.subTest(count=count):
                payload = _base(issue_resolutions=[
                    _closure(evidence=[f"file-{index}.py" for index in range(count)])])
                verdict = parse_verdict(json.dumps(payload))
                self.assertEqual(verdict.status, "done", verdict.gaps)
                self.assertEqual(len(verdict.issue_resolutions[0]["evidence"]), count)
                self.assertFalse(verdict.parse_error)

    def test_zero_or_five_evidence_strings_are_rejected_with_specific_reason(self):
        for evidence in ([], ["a.py", "b.py", "c.py", "d.py", "e.py"]):
            with self.subTest(n=len(evidence)):
                payload = _base(issue_resolutions=[_closure(evidence=evidence)])
                verdict = parse_verdict(json.dumps(payload))
                self.assertEqual(verdict.status, "continue")
                self.assertEqual(verdict.issue_resolutions, [])
                self.assertIn("Issue resolution needs 1-4 evidence strings", verdict.parse_error)
                self.assertTrue(any("Issue resolution needs 1-4 evidence strings" in gap
                                    for gap in verdict.gaps))
                serialized = json.dumps(verdict.to_dict(), ensure_ascii=False)
                self.assertIn("Issue resolution needs 1-4 evidence strings", serialized)
                self.assertNotIn("parse_error", serialized)

    def test_continue_status_keeps_business_gaps_and_surfaces_resolution_error(self):
        payload = _base(
            status="continue",
            gaps=["still missing route coverage"],
            next_actions=["add the route test"],
            issue_resolutions=[_closure(evidence=[f"e{i}.py" for i in range(7)])],
        )
        verdict = parse_verdict(json.dumps(payload))
        self.assertEqual(verdict.status, "continue")
        self.assertEqual(verdict.issue_resolutions, [])
        self.assertIn("still missing route coverage", verdict.gaps)
        self.assertIn("add the route test", verdict.next_actions)
        self.assertTrue(any(gap.startswith("Invalid issue_resolutions:")
                            and "1-4 evidence strings" in gap for gap in verdict.gaps))
        self.assertIn("1-4 evidence strings", json.dumps(verdict.to_dict()))

    def test_blocked_status_also_surfaces_resolution_error_in_gaps(self):
        payload = _base(
            status="blocked",
            blockers=["need external credential"],
            issue_resolutions=[_closure(evidence=[])],
        )
        verdict = parse_verdict(json.dumps(payload))
        # blocked without lasting blockers is normalized, but resolution feedback must remain.
        self.assertTrue(any("1-4 evidence strings" in gap for gap in verdict.gaps))
        self.assertIn("1-4 evidence strings", json.dumps(verdict.to_dict()))

    def test_illegal_id_and_blank_fields_are_rejected_with_specific_reason(self):
        cases = [
            (_closure(ident="AUD-0"), "Issue resolution IDs must be unique monitor-assigned AUD numbers"),
            (_closure(ident="issue-1"), "Issue resolution IDs must be unique monitor-assigned AUD numbers"),
            (_closure(summary="   "), "Audit text must be nonempty"),
            (_closure(evidence=["  "]), "Audit text must be nonempty"),
        ]
        for proposal, needle in cases:
            with self.subTest(proposal=proposal):
                for status in ("done", "continue"):
                    with self.subTest(status=status):
                        verdict = parse_verdict(json.dumps(_base(status=status, issue_resolutions=[proposal])))
                        self.assertEqual(verdict.status, "continue")
                        self.assertEqual(verdict.issue_resolutions, [])
                        self.assertIn(needle, verdict.parse_error)
                        self.assertTrue(any(needle in gap for gap in verdict.gaps))
                        self.assertIn(needle, json.dumps(verdict.to_dict()))

    def test_audit_resolutions_helper_still_enforces_one_to_four(self):
        with self.assertRaisesRegex(ValueError, "1-4 evidence strings"):
            resolutions([_closure(evidence=[f"f{i}.py" for i in range(5)])])
        with self.assertRaisesRegex(ValueError, "1-4 evidence strings"):
            resolutions([_closure(evidence=[])])
        ok = resolutions([_closure(evidence=["a.py", "b.py", "c.py", "d.py"])])
        self.assertEqual(len(ok[0]["evidence"]), 4)


class ResolutionSupervisorFeedbackTests(unittest.TestCase):
    BRIEF = {
        "status": "ready",
        "summary": "Requirement clarified",
        "refined_requirement": "Implement original behavior and reject invalid inputs.",
        "acceptance_criteria": ["valid input succeeds", "invalid input rejected"],
        "constraints": ["Preserve original scope"],
        "assumptions": ["Use existing project conventions"],
        "blockers": [],
    }

    def _git_project(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / "app.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    def _brief(self):
        return {"verdict": {}, "text": json.dumps(self.BRIEF)}

    def test_continue_oversized_closure_feeds_next_prompt_keeps_auds_open_no_commit(self):
        """Invalid continue closures must not close AUDs or approve commits; next prompt sees prior gaps."""
        with tempfile.TemporaryDirectory(prefix='am-res-fb-', dir='/tmp') as tmp:
            root = Path(tmp)
            self._git_project(root)
            oversized = [
                _closure("AUD-1", evidence=[f"e{i}.py" for i in range(7)]),
                _closure("AUD-2", evidence=["app.py"]),
                _closure("AUD-3", evidence=[f"x{i}.py" for i in range(5)]),
            ]
            worker = ScriptedProvider("worker", [
                {"verdict": {**_verdict("done", evidence=["app.py"]), "issue_resolutions": []},
                 "touch": "app.py"},
                {"verdict": {**_verdict("done", evidence=["app.py"]), "issue_resolutions": []}},
                {"verdict": {**_verdict("done", evidence=["app.py"]), "issue_resolutions": []}},
            ])
            # calls[0]=requirements; [1]=open AUDs; [2]=oversized continue; [3]=follow-up sees prior.
            reviewer = ScriptedProvider("reviewer", [
                self._brief(),
                {"verdict": {**_verdict("continue",
                                       gaps=["gap-one", "gap-two", "gap-three"],
                                       evidence=["app.py"]),
                             "issue_resolutions": []}},
                {"verdict": {**_verdict("continue",
                                       gaps=["remaining product gap"],
                                       evidence=["app.py"]),
                             "issue_resolutions": oversized}},
                {"verdict": {**_verdict("continue", gaps=["still open"], evidence=["app.py"]),
                             "issue_resolutions": []}},
            ])
            config = RunConfig(
                "task", root, worker, reviewer=reviewer, run_dir=root / "run",
                retry_delay_s=0, max_rounds=3, review_every=1, git_history="required",
                workflow="review-first",
            )
            result = Supervisor(config, log=lambda _: None).run()
            self.assertNotEqual(result.status, "done")
            report = json.loads((root / "run" / "report.json").read_text(encoding="utf-8"))
            reviews = [item for item in report["records"] if item["role"] == "reviewer"]
            failed = next(
                item for item in reviews
                if any("Invalid issue_resolutions:" in gap and "1-4 evidence" in gap
                       for gap in item.get("gaps", []))
            )
            self.assertEqual(failed["status"], "continue")
            self.assertEqual(failed.get("issue_resolutions") or [], [])
            self.assertIn("remaining product gap", failed.get("gaps", []))
            watcher = failed.get("watcher_result") or {}
            self.assertEqual(watcher.get("status"), "not_applied")
            self.assertEqual(watcher.get("closed") or [], [])
            issues = watcher.get("issues") or {}
            for ident in ("AUD-1", "AUD-2", "AUD-3"):
                self.assertEqual(issues.get(ident), "open", msg=f"{ident} must stay open: {issues}")
            # Reviewer call after oversized failure (index 3: brief, open, fail, follow-up).
            self.assertGreaterEqual(len(reviewer.calls), 4)
            follow = reviewer.calls[3]["prompt"]
            previous = _section(follow, "Previous independent review and monitor acceptance feedback:")
            self.assertIn("Invalid issue_resolutions:", previous)
            self.assertIn("Issue resolution needs 1-4 evidence strings", previous)
            self.assertIn("remaining product gap", previous)
            instructions = _section(follow, "When this turn ends,")
            self.assertNotIn("Invalid issue_resolutions:", instructions)
            self.assertGreaterEqual(len(worker.calls), 3)
            worker_prompt = worker.calls[2]["prompt"]
            # Protocol repair was handled by the reviewer. Only its real remaining
            # product finding belongs to the next worker turn.
            self.assertNotIn("Invalid issue_resolutions:", worker_prompt)
            self.assertIn("still open", worker_prompt)
            history = json.loads((root / "run" / "git-history.json").read_text(encoding="utf-8"))
            self.assertTrue(history.get("approval_only"))
            self.assertEqual(history.get("head") or "", "")
            # Branch must not exist until a joint approval commit.
            ref = history.get("ref") or ""
            if ref:
                probe = subprocess.run(
                    ["git", "-C", str(root), "show-ref", "--verify", ref],
                    capture_output=True,
                )
                self.assertNotEqual(probe.returncode, 0)
            state = json.loads((root / "run" / "state.json").read_text(encoding="utf-8"))
            by_id = {item["id"]: item for item in state.get("audit_issues", {}).get("items", [])}
            for ident in ("AUD-1", "AUD-2", "AUD-3"):
                self.assertEqual(by_id[ident]["status"], "open")
                self.assertIsNone(by_id[ident].get("resolution"))

    def test_valid_compressed_closure_can_progress_after_feedback(self):
        """After a failed oversized attempt, a 1-evidence-each closure may resolve the three AUDs."""
        with tempfile.TemporaryDirectory(prefix='am-res-ok-', dir='/tmp') as tmp:
            root = Path(tmp)
            self._git_project(root)
            oversized = [
                _closure("AUD-1", evidence=[f"e{i}.py" for i in range(7)]),
                _closure("AUD-2", evidence=["app.py"]),
                _closure("AUD-3", evidence=[f"x{i}.py" for i in range(5)]),
            ]
            valid = [
                _closure("AUD-1", evidence=["app.py"]),
                _closure("AUD-2", evidence=["app.py"]),
                _closure("AUD-3", evidence=["app.py"]),
            ]

            class CitingReviewer(ScriptedProvider):
                def _command(inner, session, prompt_file, read_only):
                    prompt = prompt_file.read_text(encoding="utf-8")
                    if inner.calls:  # skip requirements brief
                        match = re.search(r"Worker checkpoint: ([0-9a-f]{40,64})", prompt)
                        if match and inner.scripts:
                            evidence = list(inner.scripts[0]["verdict"].get("evidence") or [])
                            token = match.group(1)
                            if token not in evidence:
                                evidence.append(token)
                            inner.scripts[0]["verdict"]["evidence"] = evidence
                    return super()._command(session, prompt_file, read_only)

            worker = ScriptedProvider("worker", [
                {"verdict": {**_verdict("done", evidence=["app.py"]), "issue_resolutions": []},
                 "touch": "app.py"},
                {"verdict": {**_verdict("done", evidence=["app.py"]), "issue_resolutions": []}},
                {"verdict": {**_verdict("done", evidence=["app.py"]), "issue_resolutions": []}},
            ])
            reviewer = CitingReviewer("reviewer", [
                self._brief(),
                {"verdict": {**_verdict("continue",
                                       gaps=["gap-one", "gap-two", "gap-three"],
                                       evidence=["app.py"]),
                             "issue_resolutions": []}},
                {"verdict": {**_verdict("continue", gaps=["still open"], evidence=["app.py"]),
                             "issue_resolutions": oversized}},
                {"verdict": {**_verdict("done", evidence=["app.py"]),
                             "issue_resolutions": valid}},
            ])
            config = RunConfig(
                "task", root, worker, reviewer=reviewer, run_dir=root / "run",
                retry_delay_s=0, max_rounds=5, review_every=1, git_history="required",
                workflow="review-first", checks=["test -f app.py"],
            )
            Supervisor(config, log=lambda _: None).run()
            report = json.loads((root / "run" / "report.json").read_text(encoding="utf-8"))
            reviews = [item for item in report["records"] if item["role"] == "reviewer"]
            failed = next(
                item for item in reviews
                if any("Invalid issue_resolutions:" in gap for gap in item.get("gaps", []))
            )
            self.assertEqual((failed.get("watcher_result") or {}).get("closed") or [], [])
            for ident in ("AUD-1", "AUD-2", "AUD-3"):
                self.assertEqual((failed.get("watcher_result") or {}).get("issues", {}).get(ident), "open")
            applied = [item for item in reviews
                       if (item.get("watcher_result") or {}).get("status") == "applied"
                       and set((item.get("watcher_result") or {}).get("closed") or []) >= {"AUD-1", "AUD-2", "AUD-3"}]
            self.assertTrue(applied, msg="valid 1-evidence closures must eventually apply")
            # Prompt after the failure must carry the concrete prior-conclusion error.
            self.assertGreaterEqual(len(reviewer.calls), 4)
            previous = _section(reviewer.calls[3]["prompt"],
                                "Previous independent review and monitor acceptance feedback:")
            self.assertIn("Invalid issue_resolutions:", previous)
            self.assertIn("1-4 evidence strings", previous)
