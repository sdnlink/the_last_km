import json
import unittest

from auto_moniter.verdict import parse_verdict


class VerdictTests(unittest.TestCase):
    def test_completion_requires_explicit_valid_scope(self) -> None:
        from tests.test_supervisor import _verdict

        for scope in (None, "", "unknown", "module", "requirement"):
            payload = _verdict("done")
            if scope is None:
                payload.pop("scope")
            else:
                payload["scope"] = scope
            verdict = parse_verdict(json.dumps(payload))
            self.assertEqual(verdict.status, "done" if scope in {"module", "requirement"} else "continue")
            if verdict.status == "done":
                self.assertEqual(verdict.scope, scope)

    def test_parses_fenced_json(self) -> None:
        text = """I finished the handler.

```json
{"status":"continue","summary":"route exists","done":["route"],"gaps":["tests"],"next_actions":["add tests"],"evidence":["app.py"],"blockers":[]}
```
"""
        verdict = parse_verdict(text)
        self.assertEqual(verdict.status, "continue")
        self.assertEqual(verdict.gaps, ["tests"])

    def test_done_with_gaps_is_continue(self) -> None:
        raw = json.dumps(
            {
                "status": "done",
                "summary": "mostly",
                "done": ["ui"],
                "gaps": ["error state"],
                "next_actions": [],
                "evidence": ["app.py"],
                "blockers": [],
            }
        )
        self.assertEqual(parse_verdict(raw).status, "continue")

    def test_last_conclusion_wins(self) -> None:
        early = json.dumps(
            {
                "status": "done",
                "summary": "draft",
                "done": [],
                "gaps": [],
                "next_actions": [],
                "evidence": ["old"],
                "blockers": [],
            }
        )
        final = json.dumps(
            {
                "status": "continue",
                "summary": "real",
                "done": [],
                "gaps": ["missing tests"],
                "next_actions": ["add tests"],
                "evidence": ["new"],
                "blockers": [],
            }
        )
        verdict = parse_verdict(early + "\ntool output\n" + final)
        self.assertEqual(verdict.summary, "real")
        self.assertEqual(verdict.gaps, ["missing tests"])

    def test_blocked_without_blocker_continues(self) -> None:
        raw = json.dumps(
            {
                "status": "blocked",
                "summary": "stuck",
                "done": [],
                "gaps": [],
                "next_actions": [],
                "evidence": [],
                "blockers": [],
            }
        )
        verdict = parse_verdict(raw)
        self.assertEqual(verdict.status, "continue")
        self.assertTrue(verdict.gaps)

    def test_empty_output_continues(self) -> None:
        verdict = parse_verdict("  ")
        self.assertEqual(verdict.status, "continue")
        self.assertTrue(verdict.gaps)

    def test_unstructured_output_continues(self) -> None:
        verdict = parse_verdict("All done, ship it.")
        self.assertEqual(verdict.status, "continue")
        self.assertTrue(verdict.parse_error)


if __name__ == "__main__":
    unittest.main()
