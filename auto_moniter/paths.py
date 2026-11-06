"""Private monitor state, outside the project's normal writable tree."""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path


def state_root() -> Path:
    return Path(os.environ.get("AUTO_MONITER_STATE_DIR", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-moniter"))).expanduser().resolve()


def default_run_dir(cwd: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return project_runs_dir(cwd) / stamp


def project_runs_dir(cwd: Path) -> Path:
    project = hashlib.sha256(os.fsencode(cwd.resolve())).hexdigest()[:20]
    return state_root() / project / "runs"


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def workspace_lock(cwd: Path) -> Path:
    project = hashlib.sha256(os.fsencode(cwd.resolve())).hexdigest()[:20]
    return state_root() / "locks" / (project + ".lock")
