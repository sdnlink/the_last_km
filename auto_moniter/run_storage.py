"""Maintain run-local storage without modifying checkpoints or check evidence."""

from contextlib import contextmanager
import gzip
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

_ROUND_OUTPUT = re.compile(r'(\d+)-(worker|referee|reviewer(?:-repair-\d+|-preflight)?)(\.log|-(?:text|prompt)\.md)')
_TEMP_JSON = re.compile(r'\.(state\.json|report\.json|alert\.json|acceptance-results\.json|heartbeat)-[a-z0-9_]{8}')
_ARCHIVE_TEMP = re.compile(r'\.log-archive-[a-z0-9_]{8}')
_MIN_SIZE = 4096
_CACHE_AGE = 3600


def archive_path(path: Path) -> Path:
    return path.with_name(path.name + '.gz')


def available_log(path: Path) -> bool:
    return any(not item.is_symlink() and item.is_file() for item in (path, archive_path(path)))


@contextmanager
def open_log(path: Path):
    """Prefer live content; transparently fall back when a completed log moves."""
    def open_file(name):
        handle = os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            handle.close()
            raise OSError('Invalid log file')
        return handle

    try:
        raw = open_file(path)
    except FileNotFoundError:
        with open_file(archive_path(path)) as raw:
            with gzip.GzipFile(fileobj=raw) as stream:
                yield stream, os.fstat(raw.fileno()), True
    else:
        with raw:
            yield raw, os.fstat(raw.fileno()), False


def clean_run_cache(run: Path, *, now: float) -> None:
    """Remove only abandoned temporary copies of known run artifacts."""
    for path in run.iterdir():
        match = _TEMP_JSON.fullmatch(path.name)
        original = (run / match[1]) if match else run / 'baseline.json.gz'
        is_archive_temp = _ARCHIVE_TEMP.fullmatch(path.name)
        if not (match or path.name == 'baseline.json.gz.tmp' or is_archive_temp):
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or now - info.st_mtime < _CACHE_AGE:
                continue
            if is_archive_temp or (not original.is_symlink() and original.is_file()):
                path.unlink()
        except OSError:
            continue


def _compress(path: Path) -> int:
    target = archive_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        before = os.fstat(source.fileno())
        with tempfile.NamedTemporaryFile(prefix='.log-archive-', dir=path.parent, delete=False) as raw:
            temporary = Path(raw.name)
            try:
                with gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=1, mtime=0) as archive:
                    shutil.copyfileobj(source, archive)
                raw.flush()
                if raw.tell() >= before.st_size:
                    return 0
                after = path.lstat()
                signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                if signature(before) != signature(after):
                    return 0
                os.fsync(raw.fileno())
                os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
                temporary.replace(target)
                # Persist the archive name before removing the original.
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                path.unlink()
                return before.st_size - raw.tell()
            finally:
                temporary.unlink(missing_ok=True)


def archive_completed_logs(run: Path, *, before_round: int, size: int, limit: int, active_round=None, referenced=()) -> int:
    threshold = limit * 4 // 5
    if size < threshold:
        return size
    candidates = []
    for path in run.iterdir():
        match = _ROUND_OUTPUT.fullmatch(path.name)
        if not match or path.name in referenced or int(match[1]) == active_round:
            continue
        index = int(match[1])
        # Planning, coverage and referee artifacts have their own sequences.
        # A final turn log, or its archive, confirms its prompt can be archived.
        special = match[2] == 'referee' or match[2].startswith('reviewer') and (not index or index >= 900000)
        completed = special and available_log(path.with_name(f'{match[1]}-{match[2]}.log'))
        if index < before_round or completed:
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_size >= _MIN_SIZE:
                    candidates.append((info.st_size, path))
            except OSError:
                continue
    for _, path in sorted(candidates, reverse=True):
        try:
            size -= _compress(path)
        except OSError:
            continue
        if size < threshold:
            break
    return size


def maintain_run_storage(monitor) -> None:
    tick = time.monotonic()
    limit = monitor.config.max_run_bytes
    if tick - getattr(monitor, '_last_cache_check', float('-inf')) >= 60:
        monitor._last_cache_check = tick
        try:
            clean_run_cache(monitor.run_dir, now=time.time())
        except OSError:
            pass
    if not limit or tick - monitor._last_space_check < 1:
        return
    monitor._last_space_check = tick
    size = 0
    for parent, dirs, files in os.walk(monitor.run_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        for name in files:
            try:
                size += (Path(parent) / name).lstat().st_size
            except FileNotFoundError:
                continue
    last_archive = getattr(monitor, '_last_archive_check', float('-inf'))
    if size >= limit or tick - last_archive >= 60:
        monitor._last_archive_check = tick
        # Keep recent candidates and any explicitly selected active review round intact.
        active_round = getattr(monitor, '_last_invocation', {}).get('round')
        before_round = min(monitor._next_round - 2, active_round) if active_round is not None else monitor._next_round - 2
        if active_round is not None and (active_round == 0 or active_round >= 900000):
            before_round = monitor._next_round - 2
        referenced = {getattr(monitor, 'requirements_review', {}).get('output_file')}
        saved_round = getattr(monitor.config, 'review_saved_round', None)
        if saved_round is not None:
            referenced.add(f'{saved_round:03d}-worker.log')
        function_tasks = getattr(monitor, 'function_tasks', None)
        accepted = {entry['task']['id'] for entry in getattr(function_tasks, 'accepted', [])}
        for record in getattr(monitor, '_records', ()):
            if (record.role == 'reviewer' and record.watcher_result.get('status') == 'blocked'
                    and record.verdict.task_id not in accepted):
                referenced.update((Path(record.output_path).name, f'{record.index:03d}-worker.log'))
        try:
            size = archive_completed_logs(monitor.run_dir, before_round=before_round, size=size, limit=limit,
                                          active_round=active_round, referenced=referenced)
        except OSError:
            pass
    monitor._space_exceeded = size >= limit
