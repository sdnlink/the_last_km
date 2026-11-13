"""Restart the console owning a specific catalog, without stopping its tasks."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from auto_moniter.processes import process_identity
from console.server import build_parser


def lock_owner(path: Path) -> int | None:
    """Identify the exclusive flock holder; an open but unlocked file is not ownership."""
    info = path.stat()
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
            continue
        major, minor, inode = fields[5].split(':')
        if (int(major, 16), int(minor, 16), int(inode)) == (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino):
            return int(fields[4])
    return None


def running_options(pid: int, data_dir: Path):
    """Read the actual command as well as metadata to support older console instances."""
    command = Path(f'/proc/{pid}/cmdline').read_bytes().decode().strip('\0').split('\0')
    if ['-m', 'console'] == command[1:3]:
        arguments = command[3:]
    elif any(Path(item).name == 'auto-moniter-console' for item in command[:2]):
        index = next(i for i, item in enumerate(command[:2]) if Path(item).name == 'auto-moniter-console')
        arguments = command[index + 1:]
    else:
        raise ValueError('数据目录锁由其他程序持有，未停止该进程')
    parser = build_parser()
    process_cwd = Path(f'/proc/{pid}/cwd').resolve()
    parser.set_defaults(cwd=process_cwd)
    args = parser.parse_args(arguments)
    # Relative console paths are interpreted against the original process directory.
    def resolve(path):
        path = path.expanduser()
        return (path if path.is_absolute() else process_cwd / path).resolve()
    args.cwd = resolve(args.cwd)
    args.data_dir = resolve(args.data_dir) if args.data_dir else args.cwd / '.auto-moniter/console'
    if args.data_dir != data_dir:
        raise ValueError('持锁控制台的数据目录不匹配，未停止该进程')
    args.runs_dir = [resolve(path) for path in args.runs_dir]
    try:
        saved = json.loads((data_dir / 'console-process.json').read_text())
    except (FileNotFoundError, ValueError):
        saved = {}
    if isinstance(saved, dict) and saved.get('identity') == process_identity(pid):
        args.port = saved['port']
    return args


def restart(options, *, stop_timeout=15, start_timeout=60) -> int:
    cwd = (options.cwd or Path.cwd()).expanduser().resolve()
    data_dir = (options.data_dir or cwd / '.auto-moniter/console').expanduser().resolve()
    if not cwd.is_dir() or options.port is not None and not 0 <= options.port <= 65535:
        raise ValueError('项目目录必须存在，端口必须在 0 到 65535 之间')
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data_dir / 'console-restart.lock').open('a') as restart_lock:
        try:
            fcntl.flock(restart_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('该数据目录正在重启，请等待完成') from exc
        lock_path = data_dir / 'console.lock'
        lock_path.touch(exist_ok=True)
        with lock_path.open('a') as console_lock:
            try:
                fcntl.flock(console_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pid = lock_owner(lock_path)
                if pid is None or pid <= 1 or Path(f'/proc/{pid}').stat().st_uid != os.getuid():
                    raise ValueError('无法确认控制台持锁进程，未停止任何进程')
                identity = process_identity(pid)
                previous = running_options(pid, data_dir)
                if options.cwd is None:
                    cwd = previous.cwd
                if options.port is None:
                    options.port = previous.port
                if options.runs_dir is None:
                    options.runs_dir = previous.runs_dir
                print(f'正在停止控制台（PID {pid}），已启动任务继续运行…', flush=True)
                if process_identity(pid) != identity:
                    raise ValueError('控制台进程已变化，请重试')
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + stop_timeout
                while True:
                    try:
                        fcntl.flock(console_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ValueError('控制台尚未退出，已取消重启；请检查后重试')
                        time.sleep(.1)
            # Let the replacement acquire its usual catalog lock.
            fcntl.flock(console_lock, fcntl.LOCK_UN)
        port = options.port if options.port is not None else 8765
        arguments = [sys.executable, '-m', 'console', '--cwd', str(cwd), '--data-dir', str(data_dir), '--port', str(port)]
        for path in options.runs_dir or []:
            arguments.extend(['--runs-dir', str(path.expanduser().resolve())])
        log_path = data_dir / 'console-server.log'
        with log_path.open('ab') as log:
            log.write(f'\n[{datetime.now(timezone.utc).isoformat()}] 启动控制台\n'.encode())
            log.flush()
            child = subprocess.Popen(arguments, cwd=Path(__file__).resolve().parent.parent,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        deadline = time.monotonic() + start_timeout
        process_file = data_dir / 'console-process.json'
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ValueError(f'控制台启动失败（退出码 {child.returncode}），请查看日志：{log_path}')
            try:
                saved = json.loads(process_file.read_text())
                if isinstance(saved, dict) and saved.get('identity') == process_identity(child.pid):
                    connection = HTTPConnection('127.0.0.1', saved['port'], timeout=1)
                    try:
                        connection.request('GET', '/api/tasks')
                        response = connection.getresponse()
                        response.read()
                        if response.status == 200:
                            print(f'控制台已就绪：http://127.0.0.1:{saved["port"]}\nPID：{child.pid}\n日志：{log_path}', flush=True)
                            return 0
                    finally:
                        connection.close()
            except (OSError, ValueError, HTTPException):
                pass
            time.sleep(.2)
        raise ValueError(f'等待控制台启动超时，进程可能仍在初始化，请查看日志：{log_path}')


def main(argv=None) -> int:
    parser = build_parser()
    parser.prog = 'restart.sh'
    parser.description = '重启指定数据目录的控制台并在后台运行；保留任务和队列'
    parser.epilog = '重启已有实例时，未指定的端口和扫描目录沿用原配置；未运行时直接启动。自定义数据目录需传入同一个 --data-dir。'
    parser.set_defaults(port=None, cwd=None, runs_dir=None)
    options = parser.parse_args(argv)
    try:
        return restart(options)
    except (OSError, ValueError) as exc:
        print(f'重启失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
