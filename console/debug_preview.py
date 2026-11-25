"""Start a task's Vite preview in an isolated source snapshot."""

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from auto_moniter.directions import Directions
from auto_moniter.processes import process_identity, recover_process
from auto_moniter.storage import write_json
from auto_moniter.workspace import _workspace_files

CONFIGS = ('vite.config.ts', 'vite.config.js', 'vite.config.mts', 'vite.config.mjs',
           'vite.config.cts', 'vite.config.cjs')
ENV_FILES = ('.env', '.env.local', '.env.development', '.env.development.local')


def available(workspace):
    return (workspace / 'package.json').is_file() and any((workspace / name).is_file() for name in CONFIGS)


def status(directory):
    saved = Directions(directory).read('preview.json')
    if not saved:
        return {'status': 'idle'}
    result = Directions(directory / saved['launch']).read('result.json')
    identity = saved['identity']
    alive = process_identity(identity['pid']) == identity
    if result.get('status') in ('needs_input', 'failed'):
        return result
    if alive and result.get('status') == 'ready':
        return result
    if alive and time.time() - saved['started'] < 90:
        return {'status': 'starting'}
    if alive:
        return {'status': 'failed', 'message': '前端启动超时，请重试。'}
    return {'status': 'failed', 'message': '本地调试服务已退出，请重试。'}


def stop(directory):
    saved = Directions(directory).read('preview.json')
    if saved:
        recover_process(saved['identity'])
        (directory / 'preview.json').unlink(missing_ok=True)
        shutil.rmtree(directory / saved['launch'])
    return {'status': 'idle'}


def remove(directory):
    stop(directory)
    (directory / 'backend.json').unlink(missing_ok=True)
    if directory.is_dir():
        directory.rmdir()


def backend_url(value):
    if not isinstance(value, str) or len(value) > 500 or any(ch.isspace() for ch in value):
        raise ValueError('请输入有效的后端地址。')
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError('请输入有效的后端地址。') from None
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or '\\' in value):
        raise ValueError('请输入不含账号、密码或查询参数的 HTTP 或 HTTPS 地址。')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('后端端口必须在 1 到 65535 之间。')
    return value


def start(directory, workspace, data):
    current = status(directory)
    if current['status'] in ('starting', 'ready'):
        return current
    backend = backend_url(data['backend']) if 'backend' in data else ''
    if backend and current['status'] != 'needs_input':
        raise ValueError('当前调试环节无需填写后端地址。')
    node = shutil.which('node')
    if not node or not (workspace / 'node_modules/vite/package.json').is_file():
        return {'status': 'failed', 'message': '本地调试需要 Node.js 和项目已安装的 Vite 依赖。'}
    stop(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    launch = directory / 'session'
    project = launch / 'source'
    project.mkdir(parents=True)
    try:
        _copy_source(workspace, project, directory)
        runner = project / '.auto-monitor-preview.mjs'
        shutil.copyfile(Path(__file__).with_suffix('.mjs'), runner)
        env = os.environ.copy()
        if backend:
            write_json(directory / 'backend.json', {'url': backend}, durable=True, mode=0o600)
        else:
            backend = Directions(directory).read('backend.json').get('url', '')
        if backend:
            env['VITE_BASEURL'] = backend_url(backend)
        with (launch / 'preview.log').open('wb') as stream:
            process = subprocess.Popen([node, str(runner)], cwd=project, env=env,
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        identity = process_identity(process.pid)
        if not identity:
            raise ValueError('本地调试服务启动失败。')
        threading.Thread(target=process.wait, daemon=True).start()
        write_json(directory / 'preview.json', {'identity': identity, 'launch': launch.name,
                                               'started': time.time()}, durable=True, mode=0o600)
    except BaseException:
        if 'process' in locals() and process.poll() is None:
            recover_process(process_identity(process.pid))
        shutil.rmtree(launch)
        raise
    return status(directory)


def _copy_source(workspace, project, directory):
    for source in _workspace_files(workspace, ignore_dir=directory):
        relative = source.relative_to(workspace)
        if source.is_symlink() or 'node_modules' in relative.parts:
            continue
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name in ENV_FILES:
        path = workspace / name
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, project / name)
    (project / 'node_modules').symlink_to(workspace / 'node_modules', target_is_directory=True)
