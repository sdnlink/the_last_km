"""Persist task definitions and delegate execution to the existing supervisor CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.health import read_run_snapshot, run_health
from auto_moniter.paths import state_root
from auto_moniter.processes import process_identity
from auto_moniter.providers import discover_providers
from auto_moniter.redaction import redact, safe
from auto_moniter.storage import write_json
from console.metrics import Metrics, process_tree
from console.activity import ActivityReader


PROVIDERS = ('codex', 'claude', 'grok', 'gemini', 'cursor', 'antigravity', 'paseo')
RESUMABLE = {'stopped', 'blocked', 'budget_exceeded', 'recovery_failed', 'action_required', 'interrupted'}


class TaskError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def tail(path: Path, size: int = 64 * 1024) -> str:
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            length = stream.tell()
            stream.seek(max(0, length - size))
            return redact(stream.read(size).decode('utf-8', errors='replace'))
    except OSError:
        return ''


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, data_dir: Path, cwd: Path, runs_dirs: list[Path] | None = None):
        self.data_dir = data_dir.resolve()
        self.cwd = cwd.resolve()
        self.tasks_dir = self.data_dir / 'tasks'
        self.runs_dir = self.data_dir / 'runs'
        for path in (self.data_dir, self.tasks_dir, self.runs_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs_dirs = [state_root(), self.cwd / '.auto-moniter', *(runs_dirs or [])]
        self.metrics = Metrics(self.data_dir)
        self.activity = ActivityReader()
        self._lock = threading.RLock()
        self._catalog_at = 0.0
        self._external = {}
        self._children = {}

    def config(self) -> dict:
        available = discover_providers()
        return {'cwd': str(self.cwd), 'data_dir': str(self.data_dir),
                'providers': [{'name': name, 'available': bool(available.get(name))} for name in PROVIDERS],
                'poll_interval_ms': 3000}

    def _catalog(self) -> dict:
        tasks = {}
        for path in self.tasks_dir.glob('*.json'):
            meta = read_json(path)
            if meta.get('id') == path.stem and re.fullmatch(r'[0-9a-f]{32}', path.stem):
                tasks[meta['id']] = meta
        if time.monotonic() - self._catalog_at > 5:
            external = {}
            for root in self.runs_dirs:
                root = root.expanduser().resolve()
                visited = 0
                for current, dirs, files in os.walk(root, followlinks=False):
                    visited += 1
                    path = Path(current)
                    depth = len(path.relative_to(root).parts)
                    dirs[:] = [name for name in dirs if not name.startswith('.') and name not in
                               {'node_modules', 'site-packages', 'validation-env', 'venv', '__pycache__'}]
                    if depth >= 5 or visited > 2000:
                        dirs[:] = []
                    if visited > 2000:
                        break
                    if 'state.json' not in files:
                        continue
                    dirs[:] = []
                    run = path.resolve()
                    ident = 'ext-' + hashlib.sha256(os.fsencode(run)).hexdigest()[:20]
                    external[ident] = {'id': ident, 'run_dir': str(run), 'managed': False}
            self._external = external
            self._catalog_at = time.monotonic()
        managed_paths = {item['run_dir'] for item in tasks.values()}
        tasks.update({key: item for key, item in self._external.items() if item['run_dir'] not in managed_paths})
        return tasks

    def _get(self, ident: str) -> dict:
        meta = self._catalog().get(ident)
        if not meta:
            raise TaskError('任务不存在或运行目录已不可用', 404)
        return meta

    def _save(self, meta: dict) -> None:
        write_json(self.tasks_dir / (meta['id'] + '.json'), meta, durable=True)

    def _launcher_alive(self, meta: dict) -> bool:
        identity = meta.get('launcher') or {}
        try:
            return bool(identity and process_identity(identity['pid']) == identity)
        except (OSError, ValueError, KeyError):
            return False

    def _view(self, meta: dict, processes: dict) -> dict:
        run = Path(meta['run_dir'])
        state = read_json(run / 'state.json')
        status = meta.get('status', 'unknown')
        health = {'health': 'not_started', 'phase': status, 'rounds': 0, 'usage': {}}
        live = self._launcher_alive(meta)
        try:
            snapshot = read_run_snapshot(run)
            health = run_health(run, snapshot=snapshot)
            # A report is authoritative only once the supervisor and guardian have released their locks.
            state = {**state, **snapshot['state']}
            if health['delivery_confirmed']:
                status = 'done'
            elif health['lock_held'] or health['guardian_lock_held']:
                status = 'paused' if health['phase'] == 'paused' else 'running'
            elif live:
                status = 'starting'
            elif snapshot['finished']:
                status = str(health['status'])
            else:
                status = 'interrupted'
        except (OSError, ValueError, TypeError):
            if live:
                status = 'starting'
            elif meta.get('started_at'):
                status = 'failed'
        heartbeat = read_json(run / 'heartbeat')
        guardian = read_json(run / 'guardian.json')
        roots = []
        if live:
            roots.append(meta['launcher']['pid'])
        if health.get('lock_held') or health.get('guardian_lock_held'):
            roots.extend(pid for pid in (heartbeat.get('pid'), heartbeat.get('supervisor_pid'),
                                         heartbeat.get('child_pid'), guardian.get('guardian_pid')) if type(pid) is int)
        resources = process_tree(processes, roots)
        provider = state.get('provider_config') or {}
        requirement = meta.get('requirement') or tail(run / 'requirement.md', 32000).strip()
        title = meta.get('title') or next((line.strip('# ').strip() for line in requirement.splitlines() if line.strip('# ').strip()), run.name)
        modules = state.get('modules') or []
        accepted = sum(item.get('status') == 'accepted' for item in modules)
        control = read_json(run / 'control.json').get('action', '')
        active = status in {'running', 'paused'}
        actions = []
        if meta.get('managed') and status == 'queued':
            actions = ['start', 'cancel']
        elif active and control != 'stop':
            actions = ['continue' if status == 'paused' or control == 'pause' else 'pause', 'stop']
        elif meta.get('managed') and status in RESUMABLE and not live and (run / 'state.json').is_file():
            actions = ['resume']
        current_provider = (provider.get('reviewer' if health.get('phase') == 'reviewing' else 'worker') or {}).get('name')
        progress = self.activity.snapshot(run, state, health, status, current_provider or meta.get('provider', ''))
        return safe({'id': meta['id'], 'title': title[:160], 'requirement': requirement,
                     'cwd': meta.get('cwd') or state.get('cwd', ''), 'run_dir': str(run),
                     'managed': bool(meta.get('managed')), 'status': status, 'health': health,
                     'provider': meta.get('provider') or (provider.get('worker') or {}).get('name', '—'),
                     'reviewer': meta.get('reviewer') or (provider.get('reviewer') or {}).get('name', '—'),
                     'created_at': meta.get('created_at') or state.get('updated_at', ''),
                     'updated_at': state.get('updated_at') or meta.get('created_at', ''),
                     'started_at': meta.get('started_at'), 'resources': resources,
                     'modules': modules, 'accepted_modules': accepted, 'total_modules': len(modules),
                     'summary': state.get('summary') or (state.get('last_worker') or {}).get('summary', ''),
                     'last_worker': state.get('last_worker'), 'last_review': state.get('last_review'),
                     'audit_issues': state.get('audit_issues', {}), 'control': control,
                     'activity': progress,
                     'actions': actions, 'checks': meta.get('checks', state.get('checks', []))})

    def overview(self) -> dict:
        with self._lock:
            sample = self.metrics.sample()
            tasks = [self._view(meta, sample['processes']) for meta in self._catalog().values()]
            tasks.sort(key=lambda item: item['created_at'], reverse=True)
            return {'tasks': tasks, 'resources': {key: value for key, value in sample.items() if key != 'processes'},
                    'at': timestamp()}

    def detail(self, ident: str) -> dict:
        with self._lock:
            return self._view(self._get(ident), self.metrics.sample()['processes'])

    def create(self, data: dict) -> dict:
        def text(key, default='', maximum=32000):
            value = data.get(key, default)
            if not isinstance(value, str) or '\x00' in value or len(value) > maximum:
                raise TaskError(f'{key} 格式或长度不正确')
            return value.strip()
        title, requirement = text('title', maximum=160), text('requirement')
        if not title or not requirement:
            raise TaskError('请填写任务名称和开发需求')
        cwd = Path(text('cwd', str(self.cwd))).expanduser().resolve()
        if not cwd.is_dir() or not os.access(cwd, os.W_OK):
            raise TaskError('项目目录不存在或不可写')
        provider, reviewer = text('provider', 'codex'), text('reviewer', 'claude')
        if provider not in PROVIDERS or reviewer not in PROVIDERS:
            raise TaskError('请选择支持的执行器和审查器')
        checks = data.get('checks', [])
        if not isinstance(checks, list) or len(checks) > 20 or any(not isinstance(c, str) or not c.strip() or len(c) > 2000 or '\x00' in c for c in checks):
            raise TaskError('验收命令格式不正确，每行填写一条命令')
        numbers = {}
        for key, default, maximum in [('max_runtime_minutes', 120, 43200), ('max_agent_calls', 100, 100000), ('max_rounds', 0, 100000)]:
            value = data.get(key, default)
            if type(value) is not int or value < 0 or value > maximum:
                raise TaskError(f'{key} 必须是 0 到 {maximum} 的整数')
            numbers[key] = value
        auto_start = data.get('auto_start', False)
        if type(auto_start) is not bool:
            raise TaskError('auto_start 必须是布尔值')
        with self._lock:
            ident = uuid.uuid4().hex
            meta = {'id': ident, 'managed': True, 'title': title, 'requirement': requirement,
                    'cwd': str(cwd), 'provider': provider, 'reviewer': reviewer,
                    'model': text('model', maximum=200), 'reviewer_model': text('reviewer_model', maximum=200),
                    'paseo_backend': text('paseo_backend', 'claude', 200),
                    'checks': [c.strip() for c in checks], **numbers,
                    'run_dir': str(self.runs_dir / ident), 'created_at': timestamp(), 'status': 'queued'}
            if auto_start:
                self._check_start(meta)
            self._save(meta)
            if auto_start:
                self._launch(meta)
            return self.detail(ident)

    def _check_start(self, meta: dict) -> None:
        available = discover_providers()
        for role in ('provider', 'reviewer'):
            if not available.get(meta[role]):
                raise TaskError(f'未找到 {meta[role]} 可执行文件，请先安装并完成登录', 409)
        if not Path(meta['cwd']).is_dir():
            raise TaskError('项目目录已不存在', 409)
        for other in self._catalog().values():
            if other['id'] == meta['id']:
                continue
            view = self._view(other, {})
            if view['cwd'] == meta['cwd'] and view['status'] in {'running', 'starting', 'paused'}:
                raise TaskError('该项目已有任务执行中，请先停止或等待完成', 409)

    def _launch(self, meta: dict, resume: bool = False) -> None:
        run = Path(meta['run_dir'])
        run.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Requirement is supplied by stdin, so even text beginning with '-' stays literal.
        argv = [sys.executable, '-m', 'auto_moniter', 'run', '--cwd', meta['cwd'],
                '--run-dir', str(run), '--provider', meta['provider'], '--reviewer', meta['reviewer'],
                '--max-runtime', str(meta['max_runtime_minutes'] * 60),
                '--max-agent-calls', str(meta['max_agent_calls']), '--max-rounds', str(meta['max_rounds']), '--guard']
        for key, flag in [('model', '--model'), ('reviewer_model', '--reviewer-model'), ('paseo_backend', '--paseo-backend')]:
            if meta.get(key):
                argv.append(flag + '=' + meta[key])
        for check in meta['checks']:
            argv.append('--check=' + check)
        if resume:
            argv.append('--resume')
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = package_root + os.pathsep + env.get('PYTHONPATH', '')
        env['PYTHONUNBUFFERED'] = '1'
        try:
            with (run / 'console.log').open('ab') as output:
                child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                         cwd=package_root, env=env, start_new_session=True)
            # communicate() runs off the request thread and reaps the detached CLI on exit.
            meta.update(status='starting', started_at=timestamp(), launcher=process_identity(child.pid))
            self._children[meta['id']] = child
            self._save(meta)
            threading.Thread(target=self._wait, args=(meta['id'], child, meta['requirement']), daemon=True).start()
        except OSError as exc:
            meta.update(status='failed', started_at=timestamp(), error=str(exc))
            self._save(meta)
            raise TaskError(f'启动失败：{exc}', 500) from exc

    def _wait(self, ident: str, child: subprocess.Popen, requirement: str) -> None:
        child.communicate(requirement.encode('utf-8'))
        with self._lock:
            if self._children.get(ident) is child:
                meta = read_json(self.tasks_dir / (ident + '.json'))
                if meta:
                    meta['exit_code'] = child.returncode
                    self._save(meta)
                self._children.pop(ident, None)

    def action(self, ident: str, action: str) -> dict:
        with self._lock:
            meta = self._get(ident)
            view = self._view(meta, {})
            if action not in view['actions']:
                raise TaskError('当前状态不支持此操作，请刷新后重试', 409)
            run = Path(meta['run_dir'])
            if action in {'start', 'resume'}:
                self._check_start(meta)
                if action == 'resume':
                    (run / 'control.json').unlink(missing_ok=True)
                self._launch(meta, resume=action == 'resume')
            elif action == 'cancel':
                meta['status'] = 'cancelled'
                self._save(meta)
            elif action == 'continue':
                (run / 'control.json').unlink(missing_ok=True)
            else:
                write_json(run / 'control.json', {'action': action}, durable=True)
            return self.detail(ident)

    def logs(self, ident: str, name: str = '') -> dict:
        with self._lock:
            run = Path(self._get(ident)['run_dir'])
            files = []
            for path in run.glob('*'):
                if path.is_symlink() or not path.is_file():
                    continue
                if path.name not in {'console.log', 'monitor.log', 'events.jsonl', 'events.previous.jsonl'} and not re.fullmatch(r'\d+-(worker|reviewer)(-text)?\.(log|md)', path.name):
                    continue
                try:
                    files.append((path.stat().st_mtime, path.name))
                except FileNotFoundError:
                    continue  # The supervisor can rotate/prune logs during enumeration.
            names = [name for _, name in sorted(files, reverse=True)[:100]]
            selected = name or next((n for n in names if '-text.md' in n), names[0] if names else '')
            if selected and selected not in names:
                raise TaskError('日志文件不存在', 404)
            return {'files': names, 'selected': selected, 'content': tail(run / selected) if selected else '',
                    'limit_bytes': 65536}
