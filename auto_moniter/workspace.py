"""Workspace facts the supervisor checks itself."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_IGNORED_PARTS = {
    ".git",
    ".auto-moniter",
    "__pycache__",
    "node_modules",
    ".venv",
    "dist",
    "build",
    ".next",
    "coverage",
    "target",
}
_DIFF_LIMIT = 12000


def workspace_fingerprint(cwd: Path, *, ignore_dir: Path | None = None) -> str:
    digest = hashlib.sha256()
    for path in sorted(_workspace_files(cwd, ignore_dir)):
        try:
            token = _file_token(path)
        except FileNotFoundError:
            continue
        digest.update(str(path.relative_to(cwd)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(token.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def workspace_snapshot(cwd: Path, limit: int = 4000, *, ignore_dir: Path | None = None) -> str:
    git = _git_status(cwd)
    if git is None:
        names = []
        for path in sorted(_workspace_files(cwd, ignore_dir)):
            names.append(str(path.relative_to(cwd)))
            if len(names) >= 40:
                break
        text = "Not a git checkout. Files:\n" + "\n".join(names)
    else:
        text = git
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(snapshot truncated)"


def workspace_diff(cwd: Path, *, ignore_dir: Path | None = None, limit: int = _DIFF_LIMIT) -> str:
    git = _git_diff(cwd, ignore_dir=ignore_dir)
    if git is not None:
        text = git
    else:
        chunks: list[str] = []
        for path in sorted(_workspace_files(cwd, ignore_dir)):
            if path.is_symlink():
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    content = handle.read(1000)
            except (OSError, UnicodeDecodeError):
                continue
            chunks.append(f"--- {path.relative_to(cwd)}\n{content[:1000]}")
            if sum(len(chunk) for chunk in chunks) > limit:
                break
        text = "\n".join(chunks)
    if len(text) <= limit:
        return text or "(no diff)"
    return text[:limit] + "\n…(diff truncated)"


def evidence_paths(
    cwd: Path,
    evidence: list[str],
    *,
    created_since: float = 0,
    baseline: dict[str, str] | None = None,
    ignore_dir: Path | None = None,
) -> list[Path]:
    cwd = cwd.resolve()
    found: list[Path] = []
    for item in evidence:
        candidates = [item, *item.replace(",", " ").replace("`", " ").split()]
        candidates.extend(part for token in re.findall(r"`([^`]+)`|\]\(([^)]+)\)", item) for part in token if part)
        # A line reference may be followed immediately by Chinese prose, e.g.
        # docs/说明.md:694：已核对。 Keep literal candidates as well.
        references = []
        for token in candidates:
            match = re.match(r"^(.*?)[：:]\d+(?:[：:]\d+|[-–]\d+)?(?=$|[：:，,；;。\s\u3400-\u9fff])", token)
            if match:
                references.append(match[1])
        candidates.extend(references)
        for token in candidates:
            candidate = re.sub(r":\d+(?::\d+|[-–]\d+)?$", "", token.strip("\"'()[]{}"))
            path = Path(candidate)
            if not path.is_absolute():
                path = cwd / candidate
            try:
                path = path.resolve()
                if not _under(path, cwd) or _ignored(path, ignore_dir, cwd=cwd):
                    continue
                if not path.is_file():
                    # A removed baseline file is evidence of a deletion task.
                    # Unknown paths, replacement directories and dangling links
                    # must not masquerade as removed files.
                    if (baseline is not None and str(path) in baseline
                            and not path.exists() and not path.is_symlink()):
                        found.append(path)
                    continue
                if not _changed_during_run(path, cwd, created_since, baseline):
                    continue
                found.append(path.resolve())
            except (OSError, ValueError, RuntimeError):
                continue
    return found


def file_snapshot(cwd: Path, *, ignore_dir: Path | None = None, ignored_paths: set[Path] | None = None) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in _workspace_files(cwd.resolve(), ignore_dir, ignored_paths):
        try:
            snapshot[str(path)] = _file_token(path)
        except FileNotFoundError:
            continue
        except PermissionError:
            snapshot[str(path)] = "unreadable"
    return snapshot


def acceptance_problems(
    cwd: Path,
    evidence: list[str],
    *,
    created_since: float = 0,
    baseline: dict[str, str] | None = None,
    ignore_dir: Path | None = None,
    diff_text: str = "",
    git_base: str = "",
) -> list[str]:
    cwd = cwd.resolve()
    problems: list[str] = []
    paths = evidence_paths(cwd, evidence, created_since=created_since, baseline=baseline, ignore_dir=ignore_dir)
    if not paths:
        problems.append(
            "Review evidence does not name a workspace file created or changed during this run, or a removed baseline file."
        )
        return problems
    try:
        changed = _git_changed_paths(cwd)
        if changed is not None and git_base:
            result = subprocess.run(['git', 'diff', '--no-ext-diff', '--no-textconv', '--relative',
                                     '--name-only', '-z', git_base, '--', '.'], cwd=cwd,
                                    capture_output=True, check=False, timeout=10)
            if result.returncode:
                raise ValueError('Cannot compare evidence with task Git baseline')
            changed.update(os.fsdecode(name) for name in result.stdout.split(b'\0') if name)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return [f"Cannot verify Git workspace diff: {exc}"]
    relatives = {str(path.relative_to(cwd)) for path in paths}
    if changed is not None:
        if not relatives & changed:
            problems.append("Review evidence names a changed file that does not appear in the workspace diff.")
    elif diff_text:
        if not any(re.search(r"(?<![\w./-])" + re.escape(relative) + r"(?![\w./-])", diff_text) for relative in relatives):
            problems.append("Review evidence names a changed file that does not appear in the workspace diff.")
    return problems


def _changed_during_run(
    path: Path,
    cwd: Path,
    created_since: float,
    baseline: dict[str, str] | None,
) -> bool:
    del cwd, created_since
    if baseline is None:
        return True
    return baseline.get(str(path.resolve())) != _file_token(path)


class TreeBackup(dict[str, str]):
    """A file snapshot owning the temporary copies needed to undo review writes."""

    def __init__(self, snapshot: dict[str, str], dest: Path) -> None:
        super().__init__(snapshot)
        dest.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="review-backup-", dir=dest))
        self.saved: dict[str, Path] = {}
        self.directories: dict[Path, int] = {}
        self.ignored_paths: set[Path] = set()

    def cleanup(self) -> None:
        shutil.rmtree(self.root)


def preserve_tree(cwd: Path, dest: Path, *, ignore_dir: Path | None = None) -> TreeBackup:
    """Back up worker files before allowing a reviewer to inspect the project."""
    cwd = cwd.resolve()
    ignored_paths = _git_ignored_paths(cwd)
    directories_before: dict[Path, int] = {}
    for parent, directories, _files in os.walk(cwd, followlinks=False):
        root = Path(parent)
        directories[:] = [name for name in directories if root / name not in ignored_paths and not _ignored(root / name, ignore_dir, cwd=cwd)]
        directories_before[root] = root.stat().st_mode & 0o777
    backup = TreeBackup(file_snapshot(cwd, ignore_dir=ignore_dir, ignored_paths=ignored_paths), dest.resolve())
    backup.directories = directories_before
    backup.ignored_paths = ignored_paths
    try:
        for index, key in enumerate(backup):
            saved = backup.root / str(index)
            shutil.copy2(key, saved, follow_symlinks=False)
            backup.saved[key] = saved
        return backup
    except BaseException:
        backup.cleanup()
        raise


def restore_reviewer_writes(
    cwd: Path,
    before: dict[str, str],
    *,
    ignore_dir: Path | None = None,
) -> list[str]:
    """Undo reviewer changes, preserving the worker's files and directory tree."""
    cwd = cwd.resolve()
    backup = before if isinstance(before, TreeBackup) else None
    try:
        # Keep the original ignore policy even if the reviewer edits .gitignore.
        ignored_paths = backup.ignored_paths if backup is not None else None
        after = file_snapshot(cwd, ignore_dir=ignore_dir, ignored_paths=ignored_paths)
        if backup is not None:
            after = {key: token for key, token in after.items() if not Path(key).is_relative_to(backup.root)}
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if backup is not None:
            for directory, mode in sorted(backup.directories.items()):
                if not directory.is_dir() or directory.is_symlink() or directory.stat().st_mode & 0o777 != mode:
                    changed.add(str(directory))
                if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                    directory.unlink()
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(mode | 0o700)
        for key in after.keys() - before.keys():
            path = Path(key)
            if not path.is_dir() or path.is_symlink():
                path.unlink(missing_ok=True)
        if backup is not None:
            for key in before.keys() & changed:
                path = Path(key)
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup.saved[key], path, follow_symlinks=False)
            directories_after = []
            for parent, directories, _files in os.walk(cwd, followlinks=False):
                root = Path(parent)
                directories[:] = [name for name in directories if root / name not in backup.ignored_paths and not _ignored(root / name, ignore_dir, cwd=cwd) and root / name != backup.root and not (root / name).is_symlink()]
                directories_after.append(root)
            for directory in reversed(directories_after):
                if directory not in backup.directories and not any(directory.iterdir()):
                    directory.rmdir()
            for directory, mode in sorted(backup.directories.items(), reverse=True):
                directory.chmod(mode)
        restored = sorted(str(Path(key).relative_to(cwd)) for key in changed)
        if backup is not None:
            backup.cleanup()
        return restored
    except Exception as exc:
        if backup is not None:
            raise RuntimeError(f"Workspace restoration failed: {exc}. Backup retained at {backup.root}") from exc
        raise


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _file_token(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
    digest = hashlib.sha256()
    digest.update((path.stat().st_mode & 0o777).to_bytes(4, "big"))
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_ignored_paths(cwd: Path) -> set[Path]:
    """Ask Git for ignored roots so os.walk can prune generated directories."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--", "."],
            cwd=cwd, capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode:
        return set()
    return {cwd / os.fsdecode(name).rstrip("/") for name in result.stdout.split(b"\0") if name}


def _workspace_files(cwd: Path, ignore_dir: Path | None = None, ignored_paths: set[Path] | None = None) -> Iterator[Path]:
    if ignored_paths is None:
        ignored_paths = _git_ignored_paths(cwd)
    for parent, directories, files in os.walk(cwd, followlinks=False):
        root = Path(parent)
        for name in list(directories):
            path = root / name
            if path in ignored_paths or _ignored(path, ignore_dir, cwd=cwd):
                directories.remove(name)
            elif path.is_symlink():
                directories.remove(name)
                yield path
        for name in files:
            path = root / name
            if path not in ignored_paths and not _ignored(path, ignore_dir, cwd=cwd) and (path.is_symlink() or path.is_file()):
                yield path


def _git_changed_paths(cwd: Path) -> set[str] | None:
    # An unrelated empty .git directory above a plain project is not a repo
    # Git can discover. At the project root, even broken metadata must fail.
    marker = cwd / ".git"
    git_expected = bool(os.environ.get("GIT_DIR")) or marker.exists() or marker.is_symlink() or any(
        (parent / ".git").is_file() or (parent / ".git/HEAD").exists()
        for parent in cwd.parents
    )
    try:
        result = subprocess.run(
            ["git", "-c", "status.relativePaths=true", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
            cwd=cwd, capture_output=True, check=False, timeout=10,
        )
        prefix_result = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=cwd, capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if git_expected:
            raise ValueError(f"Git query failed ({type(exc).__name__})") from exc
        return None
    if result.returncode or prefix_result.returncode:
        if git_expected:
            raise ValueError("Git status or project-prefix query failed")
        return None
    prefix = os.fsdecode(prefix_result.stdout).rstrip("\n")
    names: set[str] = set()
    entries = iter(os.fsdecode(result.stdout).split("\0"))
    for entry in entries:
        if not entry:
            continue
        name = entry[3:]
        if name.startswith(prefix):
            names.add(name[len(prefix):])
        if "R" in entry[:2] or "C" in entry[:2]:
            original = next(entries, None)
            if original is not None and original.startswith(prefix):
                names.add(original[len(prefix):])
    return names


def _git_status(cwd: Path) -> str | None:
    try:
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status.returncode != 0:
        return None
    return "git status:\n" + status.stdout + "\ngit diff --stat HEAD:\n" + diff.stdout


def _git_diff(cwd: Path, *, ignore_dir: Path | None = None) -> str | None:
    pathspecs = [".", *[f":(exclude,glob)**/{name}/**" for name in _IGNORED_PARTS]]
    if ignore_dir is not None:
        try:
            relative = ignore_dir.resolve().relative_to(cwd.resolve())
        except ValueError:
            pass
        else:
            pathspecs.append(f":(exclude,literal){relative}")
    try:
        tracked = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--relative", "HEAD", "--", *pathspecs],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", "."],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if tracked.returncode != 0:
        return None
    chunks = [tracked.stdout]
    for name in untracked.stdout.split("\0"):
        if not name or _ignored(cwd / name, ignore_dir, cwd=cwd):
            continue
        path = cwd / name
        if path.is_symlink():
            chunks.append(f"--- /dev/null\n+++ {name}\n(symlink)\n")
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                content = handle.read(2000)
        except (OSError, UnicodeDecodeError):
            chunks.append(f"--- /dev/null\n+++ {name}\n(binary or unreadable)\n")
            continue
        chunks.append(f"--- /dev/null\n+++ {name}\n{content[:2000]}\n")
    return "\n".join(chunks)


def _ignored(path: Path, ignore_dir: Path | None = None, *, cwd: Path | None = None) -> bool:
    if ignore_dir is not None:
        try:
            path.absolute().relative_to(ignore_dir.absolute())
            return True
        except ValueError:
            pass
    parts = path.relative_to(cwd).parts if cwd is not None else path.parts
    return bool(set(parts) & _IGNORED_PARTS)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise LockError(f"another supervisor holds {path}") from exc
    except ImportError:
        pass
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    try:
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()


class LockError(RuntimeError):
    pass
