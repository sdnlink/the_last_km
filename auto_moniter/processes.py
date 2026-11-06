"""Verify Linux process ownership before recovering an interrupted invocation."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def _stat(pid: int) -> dict | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
        return {"state": fields[0], "group": int(fields[2]), "session": int(fields[3]), "started": int(fields[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def process_identity(pid: int) -> dict:
    state = _stat(pid)
    if not state:
        return {}
    return {"pid": pid, "started": state["started"], "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "namespace": os.readlink("/proc/self/ns/pid"), "uid": os.getuid()}


def _members(group: int) -> list[int]:
    found = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        stat = _stat(int(path.name))
        if stat and stat["group"] == group and stat["state"] != "Z":
            found.append(int(path.name))
    return found


def reap_background(group: int, grace_s: float = 0.3) -> bool:
    """Close live members of an invocation's own group after its leader exits."""
    if type(group) is not int or group <= 1:
        raise ValueError("Expected an invocation process-group ID greater than 1")
    # Popen.poll()/wait() reaped the leader. With no remaining group members,
    # this probe avoids scanning every process on the host while draining pipes.
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Existence is uncertain; retain the original verification path.
    if not _members(group):
        return False
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace_s
    while _members(group) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _members(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 1
        while _members(group) and time.monotonic() < deadline:
            time.sleep(0.02)
    if _members(group):
        raise RuntimeError("Background processes did not exit; refusing another invocation")
    return True


def recover_process(saved: dict, timeout_s: float = 5) -> str:
    """Terminate only a process group whose leader still has the saved identity."""
    if not saved:
        return "none"
    pid = saved.get("pid")
    if not isinstance(pid, int) or pid <= 1 or not isinstance(saved.get("started"), int):
        raise ValueError("invalid saved process identity")
    if saved.get("boot") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        return "previous_boot"
    if saved.get("namespace") != os.readlink("/proc/self/ns/pid") or saved.get("uid") != os.getuid():
        raise RuntimeError("Cannot verify previous process in a different PID namespace or user context")
    current = _stat(pid)
    if current is None:
        if _members(pid):
            raise RuntimeError("Previous process leader exited but its group still has live members; ownership cannot be verified")
        return "already_exited"
    if current["started"] != saved["started"]:
        return "pid_reused"
    if current["group"] != pid or current["session"] != pid:
        raise RuntimeError("Saved process is not its original process-group leader")
    if not _members(pid):
        return "already_exited"
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_exited"
    deadline = time.monotonic() + timeout_s
    while _members(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _members(pid):
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 1
        while _members(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    if _members(pid):
        raise RuntimeError("Previous process group did not exit; refusing to launch another worker")
    return "terminated"
