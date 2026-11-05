"""Structured conclusion contract shared by workers and reviewers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["continue", "done", "blocked"],
        },
        "summary": {"type": "string"},
        "done": {
            "type": "array",
            "items": {"type": "string"},
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
        },
        "next_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "status",
        "summary",
        "done",
        "gaps",
        "next_actions",
        "evidence",
        "blockers",
    ],
}

_STATUS = {"continue", "done", "blocked"}

_INPUT_REQUEST = re.compile(
    r"(?:would you like me to|shall I|should I|may I|can I)\s+(?:continue|proceed|start|implement|fix|make|run|go ahead)"
    r"|(?:do you (?:want|wish)|are you (?:sure|ready))\b[^\n]{0,180}\?"
    r"|(?:please|waiting for|awaiting|need (?:your|user))[^\n]{0,80}(?:confirm|approval|permission|input|response)"
    r"|(?:confirm|approve|allow|continue|proceed)[^\n]{0,60}(?:\[y/n\]|\[Y/n\]|\(y/n\)|\[yes/no\])"
    r"|(?:是否|要不要|需要我|可以|能否)[^\n]{0,60}(?:继续|开始|执行|实现|修改|修复|运行|确认)[^\n]{0,30}[？?]"
    r"|(?:请|等待|需要)[^\n]{0,35}(?:你|您|用户)?(?:确认|批准|授权|回复|选择|提供)[^\n]{0,70}",
    re.IGNORECASE,
)


def input_request(text: str) -> str:
    """Identify an explicit request to pause for a person's answer."""
    match = _INPUT_REQUEST.search(text)
    return match.group(0).strip() if match else ""


@dataclass
class Verdict:
    status: str
    summary: str = ""
    done: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    raw: str = ""
    parse_error: str = ""
    structured: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        data.pop("parse_error", None)
        data.pop("structured", None)
        return data


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif isinstance(item, dict):
                text = str(
                    item.get("text")
                    or item.get("item")
                    or item.get("gap")
                    or item.get("action")
                    or item.get("description")
                    or ""
                ).strip()
                if text:
                    items.append(text)
        return items
    return []


def _normalize(data: dict[str, Any], raw: str, *, structured: bool) -> Verdict:
    status = str(data.get("status", "")).strip().lower()
    if status not in _STATUS:
        status = "continue"
    verdict = Verdict(
        status=status,
        summary=str(data.get("summary") or "").strip(),
        done=_string_list(data.get("done")),
        gaps=_string_list(data.get("gaps")),
        next_actions=_string_list(data.get("next_actions")),
        evidence=_string_list(data.get("evidence")),
        blockers=_string_list(data.get("blockers")),
        raw=raw,
        structured=structured,
    )
    if verdict.status == "done":
        invalid = [name for name in VERDICT_SCHEMA["required"] if name not in data]
        invalid.extend(
            name for name in ("done", "gaps", "next_actions", "evidence", "blockers")
            if name in data and (not isinstance(data[name], list) or not all(isinstance(item, str) for item in data[name]))
        )
        if not isinstance(data.get("summary"), str) or not verdict.summary:
            invalid.append("summary")
        if invalid:
            verdict.status = "continue"
            verdict.gaps.append("Completion conclusion has missing or invalid fields: " + ", ".join(sorted(set(invalid))))
    # A claim of done with leftover work is not done.
    if verdict.status == "done" and (verdict.gaps or verdict.next_actions or verdict.blockers):
        verdict.status = "continue"
        if verdict.blockers and not verdict.gaps:
            verdict.gaps.append("Done was reported with unresolved external blockers.")
    if verdict.status == "blocked" and not verdict.blockers:
        verdict.blockers = ["Agent reported blocked without a concrete blocker."]
        verdict.status = "continue"
        verdict.gaps.append("Blocked was reported without a concrete external blocker.")
    request = input_request(verdict.summary)
    if request:
        verdict.status = "continue"
        verdict.gaps.append(f"Agent paused to request input: {request}")
        verdict.next_actions.append("Continue the authorized task using reasonable defaults; do not wait for a reply.")
    return verdict


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None


def _is_verdict(data: dict[str, Any]) -> bool:
    return "status" in data or "gaps" in data or "next_actions" in data


def _balanced_objects(text: str) -> list[str]:
    """Return every top-level JSON object, last occurrence last."""
    found: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth:
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start != -1:
                found.append(text[start : index + 1])
                start = -1
    return found


def _candidate_objects(text: str) -> list[str]:
    return _balanced_objects(text)


def parse_verdict(text: str, *, structured: bool = False) -> Verdict:
    """Extract the last structured conclusion from an agent turn."""
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        return Verdict(
            status="continue",
            summary="Empty agent output.",
            gaps=["The agent produced no conclusion."],
            next_actions=["Produce the required JSON conclusion and continue the unfinished work."],
            raw=raw,
            parse_error="empty output",
        )

    last_error = "no JSON object found"
    chosen: dict[str, Any] | None = None
    conclusion_end = 0
    for candidate in _candidate_objects(stripped):
        data = _loads_object(candidate)
        if data is None:
            last_error = "invalid JSON object"
            continue
        if _is_verdict(data):
            chosen = data
            conclusion_end = stripped.rfind(candidate) + len(candidate)
            last_error = ""
    if chosen is not None:
        verdict = _normalize(chosen, raw, structured=structured)
        request = input_request(stripped[conclusion_end:])
        if request:
            verdict.status = "continue"
            verdict.gaps.append(f"Agent requested input after its conclusion: {request}")
        return verdict

    request = input_request(stripped)
    if request:
        return Verdict(
            status="continue",
            summary="Agent requested input instead of completing the task.",
            gaps=[f"Agent paused to request input: {request}"],
            next_actions=["Continue the authorized task now. Choose reasonable defaults within its scope and emit the required JSON conclusion."],
            raw=raw,
        )

    return Verdict(
        status="continue",
        summary="Could not parse a structured conclusion.",
        gaps=["The agent did not end with the required JSON conclusion."],
        next_actions=[
            "Finish the current work, then emit exactly one JSON object with status, summary, done, gaps, next_actions, evidence, and blockers."
        ],
        raw=raw,
        parse_error=last_error,
    )


def verdict_instructions() -> str:
    return (
        "When this turn ends, your final message must contain exactly one JSON object "
        "and no prose after it. Use this shape:\n"
        f"{json.dumps({name: value['type'] for name, value in VERDICT_SCHEMA['properties'].items()}, ensure_ascii=False)}\n"
        "Field rules:\n"
        "- status is continue, done, or blocked.\n"
        "- summary, and every item in done, gaps, next_actions, evidence, and blockers, is a string.\n"
        "- Use done only when every stated requirement is implemented, checked, and you found no remaining gap.\n"
        "- A done summary must explicitly confirm that the task is complete and ready for delivery.\n"
        "- Questions, requests for confirmation, plans, partial progress, and waiting for input are continue, never done.\n"
        "- Continue authorized work without waiting for a person to reply. Use reasonable defaults within the stated scope.\n"
        "- If any requirement, edge case, test, or unwired path is still open, status must be continue and gaps must name it.\n"
        "- next_actions are concrete steps for the next turn.\n"
        "- evidence lists files, commands, or observations that support the summary. A done review must include evidence.\n"
        "- blockers are only external facts a person must resolve, such as a missing credential. Missing ideas are not blockers.\n"
        "- Do not stop early. A conclusion is a checkpoint, not permission to idle."
    )
