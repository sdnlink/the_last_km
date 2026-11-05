"""Small run logs containing conclusions, errors, and failed checks."""

from __future__ import annotations

import json
import re

from auto_moniter.providers import _iter_json_values


def clipped(text: str, limit: int = 16000) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore") + "\n…(truncated)"


def diagnostic_summary(stdout: str, stderr: str, *, failed: bool) -> str:
    errors: list[str] = []
    checks: list[str] = []
    for blob in (stdout, stderr):
        for value in _iter_json_values(blob):
            events = value if isinstance(value, list) else [value]
            for event in events:
                if not isinstance(event, dict):
                    continue
                item = event.get("item", event)
                if not isinstance(item, dict):
                    continue
                error = event.get("error")
                if error:
                    errors.append(clipped(json.dumps({"error": error}, ensure_ascii=False, indent=2), 2000))
                elif item.get("type") == "error":
                    errors.append(clipped(str(item.get("message") or item.get("text") or ""), 2000))
                if item.get("type") == "command_execution" and item.get("exit_code") not in (None, 0):
                    command_lines = str(item.get("command") or "").splitlines()
                    command = command_lines[0][:240] if command_lines else "(command unavailable)"
                    output = str(item.get("aggregated_output") or "")
                    if not output.strip():
                        continue
                    checks.append(f"exit={item['exit_code']} {command}\n" + clipped(output[-2000:], 2000))
                elif item.get("type") == "tool_result" and item.get("is_error"):
                    checks.append(clipped(str(item.get("content") or item.get("result") or "tool failed"), 2000))
    if stderr.strip() and not _iter_json_values(stderr):
        lines = stderr.splitlines()
        if not failed:
            lines = [line for line in lines if re.search(r"\b(error|fatal|exception)\b|错误|失败", line, re.I)]
        if lines:
            errors.append(clipped("\n".join(lines[-12:]), 2000))
    useful = []
    if errors:
        useful.append("----- errors -----\n" + "\n".join(dict.fromkeys(errors[-8:])))
    if checks:
        useful.append("----- failed checks -----\n" + "\n".join(checks[-8:]))
    return "\n".join(useful)
