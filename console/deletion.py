"""Delete task logs under run locks while preserving project files and metadata."""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat


def run_key(run):
    return hashlib.sha256(os.fsencode(Path(run).resolve())).hexdigest()


@contextmanager
def inactive_run(run):
    run = Path(run)
    if any(path.is_symlink() for path in (run, *run.parents)):
        raise ValueError('任务运行目录不能包含符号链接')
    with ExitStack() as stack:
        try:
            directory = os.open(run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            yield None
            return
        stack.callback(os.close, directory)
        for name in ('guardian.lock', 'supervisor.lock'):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                continue
            stack.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('任务锁文件无效，不能删除')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('任务仍在执行或恢复，请先停止后再删除') from exc
        # A process recovery record can outlive its monitor. Never hide it from
        # the only UI recovery path just because the heartbeat is old.
        names = set(os.listdir(directory))
        if 'process.json' in names:
            raise ValueError('任务仍有待回收进程记录，请先停止或处理恢复异常')
        if names.intersection({'publication.json', 'work-checkpoint.json'}):
            raise ValueError('任务仍有待恢复的提交记录，请先恢复或处理异常')
        yield directory


def validate_log_location(run: Path, cwd: Path) -> None:
    run, cwd = run.absolute(), cwd.resolve()
    if run == cwd or run in cwd.parents:
        raise ValueError('任务运行目录包含项目文件，不能清理该目录中的日志')


def purge_run_logs(directory: int | None) -> dict:
    """Unlink generated logs directly in the pinned directory without following links."""
    removed, size = 0, 0
    if directory is not None:
        for name in sorted(os.listdir(directory)):
            if not (re.search(r'\.(?:log|jsonl)(?:\.\d+)?(?:\.gz)?$', name)
                    or re.fullmatch(r'\d+-(?:worker|reviewer(?:-repair-\d+)?)-(?:text|prompt)\.md', name)):
                continue
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError(f'日志路径不是普通文件：{name}')
            os.unlink(name, dir_fd=directory)
            removed += 1
            size += info.st_size
        if removed:
            os.fsync(directory)
    return {'log_files_removed': removed, 'log_bytes_removed': size}
