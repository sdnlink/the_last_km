"""Hierarchical leases: ancestors shared, selected workspace exclusive."""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
import subprocess
from pathlib import Path

from auto_moniter.paths import workspace_lock
from auto_moniter.workspace import LockError


def workspace_root(cwd: Path) -> Path:
    cwd = cwd.resolve()
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return cwd


def workspaces_overlap(left: Path, right: Path) -> bool:
    left, right = workspace_root(left), workspace_root(right)
    return left.is_relative_to(right) or right.is_relative_to(left)


@contextmanager
def workspace_lease(cwd: Path):
    root = workspace_root(cwd)
    # State directories can differ between consoles. A per-user rendezvous in
    # /tmp keeps those instances mutually exclusive too; never unlink lock files.
    shared = Path('/tmp') / f'auto-moniter-leases-{os.getuid()}'
    shared.mkdir(mode=0o700, exist_ok=True)
    info = shared.lstat()
    if shared.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LockError('Unsafe shared workspace lease directory')
    with ExitStack() as stack:
        for path in [*reversed(root.parents), root]:
            key = hashlib.sha256(os.fsencode(path)).hexdigest()
            for lock in (shared / (key + '.lock'), workspace_lock(path)):
                lock.parent.mkdir(parents=True, exist_ok=True)
                handle = stack.enter_context(lock.open('a+'))
                try:
                    fcntl.flock(handle, (fcntl.LOCK_EX if path == root else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LockError(f'Another supervisor owns an overlapping workspace: {root}') from exc
        yield
