"""Remove console task registration while retaining project and run evidence."""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import os
from pathlib import Path


def run_key(run):
    return hashlib.sha256(os.fsencode(Path(run).resolve())).hexdigest()


@contextmanager
def inactive_run(run):
    with ExitStack() as stack:
        for name in ('guardian.lock', 'supervisor.lock'):
            try:
                fd = os.open(run / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            stack.callback(os.close, fd)
            import stat
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('任务锁文件无效，不能删除')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('任务仍在执行或恢复，请先停止后再删除') from exc
        # A process recovery record can outlive its monitor. Never hide it from
        # the only UI recovery path just because the heartbeat is old.
        if (run / 'process.json').exists() or (run / 'process.json').is_symlink():
            raise ValueError('任务仍有待回收进程记录，请先停止或处理恢复异常')
        if any((run/name).exists() or (run/name).is_symlink()
               for name in ('publication.json', 'work-checkpoint.json')):
            raise ValueError('任务仍有待恢复的提交记录，请先恢复或处理异常')
        yield
