"""Structured conclusion contract shared by workers and reviewers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from auto_moniter.audit import resolutions


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["continue", "done", "blocked"],
        },
        "summary": {"type": "string"},
        "scope": {"type": "string", "enum": ["module", "requirement"]},
        "module_ids": {"type": "array", "items": {"type": "string"}},
        "modules": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "acceptance": {"type": "array", "items": {"type": "string"}}, "depends_on": {"type": "array", "items": {"type": "string"}}, "files": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "title", "acceptance", "depends_on", "files"],
        }},
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
        "issue_resolutions": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^AUD-[1-9][0-9]{0,11}$",
                        "minLength": 5,
                    },
                    "summary": {"type": "string", "minLength": 1},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "required": ["id", "summary", "evidence"],
            },
        },
    },
    "required": [
        "status",
        "summary",
        "scope",
        "module_ids",
        "modules",
        "done",
        "gaps",
        "next_actions",
        "evidence",
        "blockers",
        "issue_resolutions",
    ],
}

_STATUS = {"continue", "done", "blocked"}

_INPUT_REQUEST = re.compile(
    r"(?:would you like me to|shall I|should I|may I|can I)\s+(?:continue|proceed|start|implement|fix|make|run|go ahead)"
    r"|(?:do you (?:want|wish)|are you (?:sure|ready))\b[^\n]{0,180}\?"
    r"|(?:please|waiting for|awaiting|need (?:your|user))[^\n]{0,80}(?:confirm|approval|permission|input|response)"
    r"|(?:confirm|approve|allow|continue|proceed)[^\n]{0,60}(?:\[y/n\]|\[Y/n\]|\(y/n\)|\[yes/no\])"
    r"|(?:是否|要不要|需要我|可以|能否)[^\n]{0,60}(?:继续|开始|执行|实现|修改|修复|运行|确认)[^\n]{0,30}[？?]"
    r"|(?:请(?:你|您|用户)?(?:先|明确|手动|人工)?|等待(?:你|您|用户)?的?|需要(?:你|您|用户)(?:先|明确|手动|人工)?)(?:确认|批准|授权|回复|选择|提供)[^\n。！？；]{0,70}",
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
    scope: str = "requirement"
    module_ids: list[str] = field(default_factory=list)
    modules: list[dict] = field(default_factory=list)
    issue_resolutions: list[dict] = field(default_factory=list)

    task_id: str = ""
    task_status: str = ""
    next_task: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        data.pop("parse_error", None)
        data.pop("structured", None)
        if not self.task_id and not self.task_status and self.next_task is None:
            for key in ("task_id", "task_status", "next_task"):
                data.pop(key)
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


def _normalize(data: dict[str, Any], raw: str, *, structured: bool, function_gate: bool = False,
               worker_submission: bool = False) -> Verdict:
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
        scope=str(data.get("scope") or ""),
        module_ids=_string_list(data.get("module_ids")),
        modules=data.get("modules", []),
        issue_resolutions=data.get("issue_resolutions", []),
        task_id=data.get("task_id", ""), task_status=data.get("task_status", ""), next_task=data.get("next_task"),
    )
    # Older conclusions may omit the closure array. They cannot close any issue.
    invalid = [name for name in VERDICT_SCHEMA["required"] if name not in data and name != "issue_resolutions"]
    if function_gate:
        required = set(VERDICT_SCHEMA['required']) | {'task_id', 'task_status', 'next_task'}
        invalid.extend(required ^ data.keys())
        if not isinstance(data.get('task_id'), str) or not data['task_id']:
            invalid.append('task_id')
        if data.get('task_status') not in ('done', 'incomplete', 'accepted', 'changes_required'):
            invalid.append('task_status')
        if data.get('next_task') is not None and not isinstance(data['next_task'], dict):
            invalid.append('next_task')
    invalid.extend(
        name for name in ("done", "gaps", "next_actions", "evidence", "blockers", "module_ids")
        if name in data and (not isinstance(data[name], list) or not all(isinstance(item, str) for item in data[name]))
    )
    if not isinstance(data.get("summary"), str) or not verdict.summary:
        invalid.append("summary")
    if verdict.scope not in {"module", "requirement"}:
        invalid.append("scope")
    if not isinstance(verdict.modules, list):
        invalid.append("modules")
    if not isinstance(data.get("status"), str) or data["status"] not in _STATUS:
        invalid.append("status")
    resolution_error = ""
    try:
        resolutions(verdict.issue_resolutions)
    except ValueError as exc:
        invalid.append("issue_resolutions")
        resolution_error = str(exc).strip() or "issue_resolutions invalid"
        # Reject the closure payload; keep the specific reason for the next agent.
        verdict.issue_resolutions = []
    if invalid:
        labels = sorted(set(invalid))
        if resolution_error:
            verdict.parse_error = "invalid issue_resolutions: " + resolution_error
            # to_dict drops parse_error; always put the concrete reason in gaps for
            # continue/done/blocked so the next worker/reviewer prompt can see it.
            feedback = "Invalid issue_resolutions: " + resolution_error
            if feedback not in verdict.gaps:
                verdict.gaps.append(feedback)
        else:
            verdict.parse_error = "invalid conclusion fields"
        if verdict.status == "done":
            verdict.status = "continue"
            detail = ", ".join(labels)
            completion = "Completion conclusion has missing or invalid fields: " + detail
            if resolution_error:
                completion += ". " + resolution_error
            if completion not in verdict.gaps:
                verdict.gaps.append(completion)
    # A claim of done with leftover work is not done.
    if (verdict.status == "done" and (verdict.gaps or verdict.next_actions or verdict.blockers)
            and not (worker_submission and verdict.task_status == 'done')):
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
        verdict.parse_error = "input request instead of completion"
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


def parse_verdict(text: str, *, structured: bool = False, function_gate: bool = False,
                  worker_submission: bool = False) -> Verdict:
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
        verdict = _normalize(chosen, raw, structured=structured, function_gate=function_gate,
                             worker_submission=worker_submission)
        request = input_request(stripped[conclusion_end:])
        if request:
            verdict.status = "continue"
            verdict.parse_error = "input request after conclusion"
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
            parse_error="input request without structured conclusion",
        )

    return Verdict(
        status="continue",
        summary="Could not parse a structured conclusion.",
        gaps=["The agent did not end with the required JSON conclusion."],
        next_actions=[
            "Finish the current work, then emit exactly one JSON object with status, scope, summary, done, gaps, next_actions, evidence, and blockers."
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
        "- scope is module or requirement. A module is a bounded part of the original requirement; requirement means the entire original requirement.\n"
        "- In your first turn, declare ALL modules from the original requirement in modules: [{id, title, acceptance: [criterion, ...], depends_on: [module_id, ...]}]. Use depends_on=[] when there are no dependencies. Include integration and failure-path checks. Do not omit unfinished modules.\n"
        "- IDs are stable ASCII identifiers. Definitions and criteria cannot be removed or weakened. Later turns may use modules=[] or append newly discovered missing modules.\n"
        "- Worker module declarations use files=[]. Only reviewers may supply files as conservative relative path/glob impact scopes including implementation, tests, shared callers and dependencies. User-fixed files remain authoritative.\n"
        "- module_ids names the modules completed and verified in this conclusion. Reviewers list only IDs they independently accept. The monitor requires joint coverage of every pending module before final delivery.\n"
        "- summary, and every item in done, gaps, next_actions, evidence, and blockers, is a string.\n"
        "- Use done only when every item within the declared scope is implemented, checked, and has no remaining gap.\n"
        "- done with scope=module submits a completed module for review; it does not end the original requirement. Name the module in summary and done.\n"
        "- A module done verdict has no gaps, next_actions, or blockers within that module; the supervisor continues the remaining original modules after acceptance.\n"
        "- Waiting for independent review is not an implementation gap. Submit a verified module as done now; keep unrelated pending modules in the ledger, not in this module's gaps.\n"
        "- done with scope=requirement explicitly confirms ALL original modules, integration paths, and acceptance checks are complete and ready for delivery.\n"
        "- Questions, requests for confirmation, plans, incomplete modules, and waiting for input are continue, never done.\n"
        "- Continue authorized work without waiting for a person to reply. Use reasonable defaults within the stated scope.\n"
        "- If any requirement, edge case, test, or unwired path within the declared scope is still open, status must be continue and gaps must name it.\n"
        "- next_actions are concrete steps for the next turn.\n"
        "- Reviewers reporting an existing unresolved issue prefix its gaps entry with the monitor's AUD ID, e.g. AUD-1: assertion still fails. Use plain text for genuinely new findings and never invent AUD IDs.\n"
        "- issue_resolutions is an array of at most 128 objects {id, summary, evidence}; use [] if none. "
        "Each id must match ^AUD-[1-9][0-9]{0,11}$ and be a monitor-assigned open issue. "
        "summary must be a non-empty string. evidence must be 1 to 4 non-empty strings naming current changed files "
        "(the monitor rejects 0 or more than 4). Only the independent reviewer may close issues after inspecting the fix "
        "or showing why the finding is invalid. Workers implement fixes and leave issue_resolutions=[]. "
        "Omitted or unresolved issues stay open; never invent issue IDs. "
        "If a prior turn failed validation, the gap text includes the exact reason "
        "(for example 'Issue resolution needs 1-4 evidence strings'); fix that reason rather than repeating oversized lists.\n"
        "- evidence lists files, commands, or observations that support the summary. Both worker and reviewer done conclusions must cite a workspace file created, changed or removed during this run and present in the current diff. A removed file must have existed in the initial monitor baseline; cite its original path without recreating it.\n"
        "- blockers are only external facts a person must resolve, such as a missing credential. Missing ideas are not blockers.\n"
        "- Do not stop early. A conclusion is a checkpoint, not permission to idle."
    )
