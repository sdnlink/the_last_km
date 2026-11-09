"""Serve the local console and its JSON API on loopback."""

from __future__ import annotations

import argparse
import fcntl
import json
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from auto_moniter.redaction import redact
from console.tasks import TaskError, TaskStore
from console.pages import page_route, render_page, asset


STATIC = Path(__file__).with_name('static')


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: TaskStore):
        self.store = store
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: ConsoleServer
    server_version = 'AutoMoniterConsole/1.0'

    def _send(self, status: int, body: bytes, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status=200):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def _check_host(self):
        port = self.server.server_address[1]
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in allowed:
            raise TaskError('仅允许通过本机地址访问控制台', 403)
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{host}' for host in allowed}:
            raise TaskError('请求来源不匹配', 403)

    def do_GET(self):
        try:
            self._check_host()
            url = urlsplit(self.path)
            route = page_route(url.path)
            static = asset(url.path) if route is None else None
            if route:
                self._send(200, render_page(*route, self.server.store.cwd), 'text/html; charset=utf-8')
            elif static:
                self._send(200, static[0].read_bytes(), static[1])
            elif url.path == '/api/config':
                self._json({**self.server.store.config(), 'csrf_token': self.server.csrf})
            elif url.path == '/api/overview':
                self._json(self.server.store.dashboard())
            elif url.path == '/api/tasks':
                self._json(self.server.store.task_summaries())
            elif url.path == '/api/resources':
                self._json(self.server.store.resource_overview())
            elif url.path.startswith('/api/tasks/'):
                parts = url.path.strip('/').split('/')
                if len(parts) == 4 and parts[3] == 'logs':
                    name = parse_qs(url.query).get('file', [''])[0]
                    self._json(self.server.store.logs(parts[2], name))
                elif len(parts) == 4 and parts[3] == 'progress':
                    self._json(self.server.store.progress(parts[2]))
                elif len(parts) == 5 and parts[3] == 'git':
                    self._json(self.server.store.git_record(parts[2], parts[4]))
                elif len(parts) == 3:
                    self._json(self.server.store.detail(parts[2]))
                else:
                    raise TaskError('接口不存在', 404)
            else:
                raise TaskError('页面不存在', 404)
        except (BrokenPipeError, ConnectionResetError):
            return  # Navigating away cancels in-flight requests.
        except (TaskError, OSError, ValueError) as exc:
            self._error(exc)

    def do_POST(self):
        try:
            self._check_host()
            if not secrets.compare_digest(self.headers.get('X-Console-Token', '').encode('utf-8'), self.server.csrf.encode('utf-8')):
                raise TaskError('会话已更新，请刷新页面后重试', 403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise TaskError('请求必须使用 application/json', 415)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise TaskError('请求长度不正确') from None
            if not 0 < length <= 128 * 1024:
                raise TaskError('请求内容为空或超过 128 KiB', 413)
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise TaskError('请求必须是 JSON 对象')
            path = urlsplit(self.path).path
            if path == '/api/tasks':
                self._json(self.server.store.create(data), 201)
            elif path == '/api/agents/refresh':
                self._json(self.server.store.refresh_agents())
            elif path in ('/api/acceptance/scan', '/api/acceptance/preflight'):
                self._json(self.server.store.acceptance_options(data, prepare=path.endswith('/preflight')))
            else:
                parts = path.strip('/').split('/')
                if len(parts) != 4 or parts[:2] != ['api', 'tasks']:
                    raise TaskError('接口不存在', 404)
                if parts[3] == 'directions':
                    self._json(self.server.store.add_direction(parts[2], data), 201)
                else:
                    self._json(self.server.store.action(parts[2], parts[3], data))
        except (BrokenPipeError, ConnectionResetError):
            return
        except (TaskError, OSError, ValueError) as exc:
            self._error(exc)

    def _error(self, exc):
        if isinstance(exc, TaskError):
            self._json({'error': str(exc)}, exc.status)
        elif isinstance(exc, (ValueError, UnicodeError)):
            self._json({'error': '请求或任务数据格式不正确'}, 400)
        else:
            self._json({'error': redact(f'读取或写入失败：{exc}')}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format, *args):
        if args and str(args[1]) not in {'200', '201'}:
            super().log_message(format, *args)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='auto-moniter 本地 Web 控制台（无需额外依赖）')
    parser.add_argument('--port', type=int, default=8765, help='监听端口，默认 8765')
    parser.add_argument('--cwd', type=Path, default=Path.cwd(), help='新建任务的默认项目目录')
    parser.add_argument('--data-dir', type=Path, help='任务定义和运行记录目录，默认 <cwd>/.auto-moniter/console')
    parser.add_argument('--runs-dir', type=Path, action='append', default=[], help='额外扫描的已有运行目录，可重复指定')
    args = parser.parse_args(argv)
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir() or not 0 <= args.port <= 65535:
        parser.error('项目目录必须存在，端口必须在 0 到 65535 之间')
    data_dir = (args.data_dir or cwd / '.auto-moniter/console').expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One writer per catalog; multiple HTTP request threads use the store's RLock.
    with (data_dir / 'console.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('该数据目录已有控制台运行，请使用原实例或另一个 --data-dir', file=sys.stderr)
            return 1
        store = TaskStore(data_dir, cwd, args.runs_dir)
        store.refresh_agents()
        try:
            with ConsoleServer(('127.0.0.1', args.port), store) as server:
                print(f'控制台已启动：http://127.0.0.1:{server.server_address[1]}', flush=True)
                print(f'任务数据：{data_dir}', flush=True)
                server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print('\n控制台已关闭，已启动的任务继续执行。')
        except OSError as exc:
            print(f'控制台启动失败：{exc}', file=sys.stderr)
            return 1
    return 0
