"""Inspect run storage and explicitly remove old, finished runs.

Locks and all paths are checked again while applying a preview. Never infer
termination from a stale heartbeat; interrupted checkpoints remain recoverable.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import stat
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from auto_moniter.paths import project_runs_dir


_FINAL = {"done", "failed", "stalled", "max_rounds"}
_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _json_at(directory: int, name: str) -> dict:
    descriptor = os.open(name, _FLAGS, dir_fd=directory)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
            raise ValueError(f"Invalid or oversized {name}")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {name}")
    return value


@contextmanager
def _locks(directory: int):
    with ExitStack() as stack:
        for name in ("guardian.lock", "supervisor.lock"):
            try:
                descriptor = os.open(name, _FLAGS, dir_fd=directory)
            except FileNotFoundError:
                if name == "guardian.lock":
                    continue
                raise ValueError("missing_supervisor_lock") from None
            stack.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("invalid_lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("active_lock") from None
        yield


def _size(directory: int) -> tuple[int, int]:
    logical = allocated = 0
    for name in os.listdir(directory):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue  # Active monitors atomically replace state and prune logs.
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=directory)
            except FileNotFoundError:
                continue
            try:
                nested, blocks = _size(child)
                logical += nested
                allocated += blocks
            finally:
                os.close(child)
        else:
            logical += info.st_size
        allocated += info.st_blocks * 512
    return logical, allocated


def _metadata(directory: int, cwd: Path, run: Path, include_failed: bool) -> dict:
    state = _json_at(directory, "state.json")
    saved_cwd = state.get("cwd")
    if not isinstance(saved_cwd, str) or not Path(saved_cwd).is_absolute() or Path(saved_cwd).resolve() != cwd:
        raise ValueError("different_project")
    if run == cwd or run in cwd.parents:
        raise ValueError("contains_project")
    if state.get("state_version", 1) != 1:
        raise ValueError("unsupported_state_version")
    status = state.get("status")
    if not isinstance(status, str):
        raise ValueError("invalid_status")
    result = {"status": str(status), "finished_at": None, "reason": "unfinished_or_recoverable"}
    history = state.get('git_history') or {}
    if not isinstance(history, dict):
        raise ValueError('invalid_git_history')
    result['git_archive'] = 'archive_before_delete' if history.get('enabled') else None
    if status not in _FINAL:
        return result
    report = _json_at(directory, "report.json")
    if report.get("status") != status:
        result["reason"] = "inconsistent_report"
        return result
    result["finished_at"] = os.stat("report.json", dir_fd=directory, follow_symlinks=False).st_mtime
    # An unresolved local process identity must remain available for recovery.
    try:
        os.stat("process.json", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        result["reason"] = "process_record_present"
        return result
    result["reason"] = "eligible" if status == "done" or include_failed else "failed_run_preserved"
    return result


def _root_path(cwd: Path, runs_dir: Path | None) -> Path:
    root = (runs_dir.expanduser().absolute() if runs_dir is not None else project_runs_dir(cwd))
    # Reject symlink roots/ancestors, including explicit legacy/custom locations.
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError("runs directory must not contain symbolic links")
    return root.resolve()


def _check_mounts(run: Path) -> None:
    # rmtree must never traverse a mounted filesystem (including bind mounts).
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
        if mount == run or run in mount.parents:
            raise ValueError("mounted_tree")


def run_storage(cwd: Path, *, runs_dir: Path | None = None, older_than: float = 7 * 86400,
                keep_runs: int = 5, include_failed: bool = False, apply: bool = False) -> dict:
    """Return an inventory/cleanup plan for one project's immediate run children.

    Successful finished runs are eligible by default. Failures require opt-in;
    resumable states are always excluded. No directories are created by a preview.
    """
    if not math.isfinite(older_than) or older_than < 0 or keep_runs < 0:
        raise ValueError("Invalid retention age or count")
    if apply and not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("Safe directory removal is unavailable on this platform")
    cwd = cwd.expanduser().resolve()
    root = _root_path(cwd, runs_dir)
    result = {"cwd": str(cwd), "runs_dir": str(root), "applied": apply,
              "older_than_seconds": older_than, "keep_runs": keep_runs,
              "total_bytes": 0, "allocated_bytes": 0, "candidate_bytes": 0,
              "removed_bytes": 0, "runs": [], "errors": []}
    try:
        root_fd = os.open(root, _FLAGS | os.O_DIRECTORY)
    except FileNotFoundError:
        return result
    try:
        now = time.time()
        for name in sorted(os.listdir(root_fd)):
            try:
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            row = {"name": name, "run_dir": str(root / name), "bytes": 0, "allocated_bytes": 0,
                   "status": "unknown", "finished_at": None, "reason": "invalid_run", "selected": False}
            result["runs"].append(row)
            try:
                directory = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=root_fd)
                try:
                    identity = os.fstat(directory)
                    row["directory_id"] = [identity.st_dev, identity.st_ino]
                    _check_mounts(root / name)
                    row["bytes"], row["allocated_bytes"] = _size(directory)
                    # Examine metadata even for active runs so inventory includes their status.
                    row.update(_metadata(directory, cwd, root / name, include_failed))
                    with _locks(directory):
                        pass
                finally:
                    os.close(directory)
            except (OSError, ValueError) as exc:
                row["reason"] = str(exc)
            result["total_bytes"] += row["bytes"]
            result["allocated_bytes"] += row["allocated_bytes"]

        # Keep the newest N finished runs, including failures, for investigation.
        finished = sorted((row for row in result["runs"] if row["finished_at"] is not None),
                          key=lambda row: (row["finished_at"], row["name"]), reverse=True)
        for index, row in enumerate(finished):
            if row["reason"] != "eligible":
                continue
            if index < keep_runs:
                row["reason"] = "keep_recent"
            elif now - row["finished_at"] < older_than:
                row["reason"] = "too_recent"
            else:
                row["selected"] = True
                result["candidate_bytes"] += row["bytes"]

        if apply:
            for row in result["runs"]:
                if not row["selected"]:
                    continue
                try:
                    _remove(root_fd, root, row, cwd, include_failed, now - older_than)
                    row["reason"] = "removed"
                    result["removed_bytes"] += row["bytes"]
                except (OSError, ValueError) as exc:
                    row["reason"] = f"not_removed: {exc}"
                    result["errors"].append({"run_dir": row["run_dir"], "error": str(exc)})
        return result
    finally:
        os.close(root_fd)


def _remove(root_fd: int, root: Path, row: dict, cwd: Path, include_failed: bool, cutoff: float) -> None:
    name = row["name"]
    directory = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=root_fd)
    try:
        with _locks(directory):
            _check_mounts(root / name)
            current = _metadata(directory, cwd, root / name, include_failed)
            if (current["reason"] != "eligible" or current["finished_at"] > cutoff
                    or current["finished_at"] != row["finished_at"]):
                raise ValueError("run_changed_since_preview")
            original = os.fstat(directory)
            actual = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if ([original.st_dev, original.st_ino] != row["directory_id"]
                    or (original.st_dev, original.st_ino) != (actual.st_dev, actual.st_ino)):
                raise ValueError("run_directory_changed")
            if current.get('git_archive'):
                from auto_moniter.git_maintenance import archive, handle, documents
                from auto_moniter.locking import workspace_lease
                from auto_moniter.git_history import GitHistoryError
                import hashlib
                try:
                    with workspace_lease(cwd):
                        archive(handle(cwd), root/name, documents(root/name),
                                hashlib.sha256(os.fsencode((root/name).resolve())).hexdigest()[:20])
                except GitHistoryError as exc:
                    raise ValueError('Cannot archive task Git history: ' + str(exc)) from exc
            shutil.rmtree(name, dir_fd=root_fd)
    finally:
        os.close(directory)
