"""Read-only task inventory for scheduled supervision."""

import json
from http.client import HTTPConnection, HTTPException


def snapshot(port: int) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError("Console port must be between 1 and 65535")

    # Contact the loopback console directly. Never follow redirects or use proxy settings.
    connection = HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        connection.request("GET", "/api/tasks")
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"Console task list returned HTTP {response.status}")
        data = json.load(response)
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
