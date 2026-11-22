"""Read-only task inventory for scheduled supervision."""

import json
import re
from http.client import HTTPConnection, HTTPException


MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def snapshot(port: int, *, task_id: str | None = None) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError("Console port must be between 1 and 65535")
    if task_id is not None and not re.fullmatch(r'(?:[0-9a-f]{32}|ext-[0-9a-f]{20})', task_id):
        raise ValueError("Invalid console task ID")

    # Contact the loopback console directly. Never follow redirects or use proxy settings.
    connection = HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        path = f"/api/tasks/{task_id}/snapshot" if task_id else "/api/tasks"
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"Console task snapshot returned HTTP {response.status}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Console task snapshot exceeds size limit")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Console returned invalid JSON") from exc
    except (OSError, HTTPException) as exc:
        raise ValueError(f"Cannot read the local console task list: {exc}") from exc
    finally:
        connection.close()

    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or any(
        not isinstance(task, dict) for task in data["tasks"]
    ):
        raise ValueError("Console returned an invalid task list")
    return {"at": data.get("at"), "tasks": [_task(task) for task in data["tasks"]],
            "discovery": data.get("discovery", {})}


def _task(task: dict) -> dict:
    health = task.get("health") or {}
    usage = health.get("usage") or {}
    return {
        **{key: task.get(key) for key in (
            "id", "title", "cwd", "project_cwd", "status", "control",
            "accepted_modules", "total_modules", "failure", "resume_unavailable",
        )},
        **{key: health.get(key) for key in ("health", "phase", "rounds", "heartbeat_age_s")},
        "usage": {key: usage.get(key) for key in (
            "elapsed_s", "max_runtime_s", "agent_calls", "max_agent_calls",
        )},
    }
