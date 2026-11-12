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
from auto_moniter.providers import DEFAULT_PASEO_BACKEND, ProviderError
from auto_moniter.locking import workspaces_overlap
from console.settings import settings, DEFAULTS
from console.deletion import run_key, inactive_run, purge_run_logs, validate_log_location
from console.acceptance import scan as scan_acceptance, select as select_acceptance, preflight as acceptance_preflight
from auto_moniter.redaction import redact, safe
from auto_moniter.storage import write_json
from auto_moniter.directions import Directions
from console.metrics import Metrics, process_tree
from console.activity import ActivityReader
from console.recovery import resume_arguments
from console.workflow import work_details
from console.git_records import read_git_record, GitRecordError
from console.conclusions import present_conclusion
from console.logs import LogReader
from console.agents import AgentManager
from auto_moniter.agent_settings import AGENTS, read_settings
from console.diagnostics import diagnose, console_result


PROVIDERS = tuple(AGENTS)
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


def _module_check_commands(modules) -> list[str]:
    """Collect non-empty modules[].checks from a raw create payload (pre-ModuleLedger)."""
    if not isinstance(modules, list):
        return []
    commands: list[str] = []
    for item in modules:
        if not isinstance(item, dict):
            continue
        checks = item.get('checks') or []
        if not isinstance(checks, list):
            continue
        for command in checks:
            if isinstance(command, str) and command.strip():
                commands.append(command.strip())
    return commands


class TaskStore:
    def __init__(self, data_dir: Path, cwd: Path, runs_dirs: list[Path] | None = None):
        self.data_dir = data_dir.resolve()
        self.cwd = cwd.resolve()
        self.tasks_dir = self.data_dir / 'tasks'
        self.runs_dir = self.data_dir / 'runs'
        self.deleted_dir = self.data_dir / 'deleted'
        for path in (self.data_dir, self.tasks_dir, self.runs_dir, self.deleted_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs_dirs = [state_root(), self.cwd / '.auto-moniter', *(runs_dirs or [])]
        self.metrics = Metrics(self.data_dir)
        self.activity = ActivityReader()
        self._lock = threading.RLock()
        self._catalog_at = 0.0
        self._external = {}
        self._children = {}
        self._scan_warnings = []
        self.agent_catalog = None
        self.agents = AgentManager(self.data_dir, self.cwd)

    def refresh_agents(self):
        from console.agent_catalog import discover
        self.agent_catalog = discover(self.cwd, discover_providers(), live=True)
        return self.config()

    def agent_settings(self, action, data):
        try:
            if action=='save':
                result=self.agents.save(data);self.agent_catalog=None;return result
            if action=='install':return self.agents.install(data.get('id'))
            if action=='probe':return self.agents.probe(data.get('id'))
            raise ValueError('Agent 操作不支持')
        except (ValueError,OSError) as exc:
            raise TaskError(str(exc),400) from exc

    def config(self) -> dict:
        available = self.agents.providers(discover_providers())
        if self.agent_catalog is None:
            from console.agent_catalog import discover
            self.agent_catalog = discover(self.cwd, available)
        profiles={row['id']:row for row in self.agents.snapshot()['agents']}
        catalog=dict(self.agent_catalog)
        models=dict(catalog.get('models',{}))
        for name,row in profiles.items():
            if row['config']['model']:
                current=models.get(name,{}).get('models',[])
                models[name]={'models':[{'id':row['config']['model'],'label':row['config']['model'],'source':'console_settings'},*[item for item in current if item['id']!=row['config']['model']]],'note':'控制台配置及本机模型目录'}
        catalog['models']=models
        return {'cwd': str(self.cwd), 'data_dir': str(self.data_dir),
                'providers': [{'name': name, 'label':AGENTS[name]['label'], 'available': bool(available.get(name)),
                               'enabled':profiles[name]['enabled'], 'can_review':profiles[name]['can_review']} for name in PROVIDERS],
                'setup_required':not self.agents.initialized(),
                'agent_catalog': catalog,
                'poll_interval_ms': 3000}

    def _catalog(self) -> dict:
        tasks = {}
        for path in self.tasks_dir.glob('*.json'):
            meta = read_json(path)
            if meta.get('id') == path.stem and re.fullmatch(r'(?:[0-9a-f]{32}|ext-[0-9a-f]{20})', path.stem):
                tasks[meta['id']] = meta
        if time.monotonic() - self._catalog_at > 5:
            external = {}
            warnings = []
            for root in self.runs_dirs:
                root = root.expanduser().resolve()
                visited = 0
                for current, dirs, files in os.walk(root, followlinks=False):
                    visited += 1
                    path = Path(current)
                    depth = len(path.relative_to(root).parts)
                    if '.catalog-ignore' in files:
                        dirs[:] = []
                        continue
                    dirs[:] = [name for name in dirs if not name.startswith('.') and name not in
                               {'node_modules', 'site-packages', 'validation-env', 'venv', '__pycache__'}]
                    if visited > 2000:
                        warnings.append(f'目录 {root} 已达到扫描上限，请将 --runs-dir 指向更接近任务的位置')
                        break
                    if depth >= 5 and dirs:
                        warnings.append(f'目录 {root} 有更深层级，请使用 --runs-dir 指定深层任务目录')
                        dirs[:] = []
                    artifacts = {'requirement.md', 'supervisor.lock', 'guardian.lock', 'report.json'}
                    if 'state.json' not in files or not artifacts.intersection(files):
                        continue
                    dirs[:] = []
                    run = path.resolve()
                    ident = 'ext-' + hashlib.sha256(os.fsencode(run)).hexdigest()[:20]
                    external[ident] = {'id': ident, 'run_dir': str(run), 'managed': False}
            self._external = external
            self._scan_warnings = list(dict.fromkeys(warnings))[:10]
            self._catalog_at = time.monotonic()
        managed_paths = {item['run_dir'] for item in tasks.values()}
        tasks.update({key: item for key, item in self._external.items() if item['run_dir'] not in managed_paths})
        return {ident: item for ident, item in tasks.items()
                if not (self.deleted_dir / (run_key(item['run_dir']) + '.json')).exists()}

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

    def _view(self, meta: dict, processes: dict, *, include_details: bool = True) -> dict:
        try:
            return self._view_data(meta, processes, include_details=include_details)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # One damaged run must not hide every other task in the overview.
            return safe({'id': meta['id'], 'title': meta.get('title') or Path(meta['run_dir']).name,
                         'cwd': meta.get('cwd', ''), 'run_dir': meta['run_dir'], 'managed': bool(meta.get('managed')),
                         'status': 'unknown', 'health': {'health': 'unknown', 'phase': 'unknown', 'rounds': 0, 'usage': {}},
                         'provider': meta.get('provider', '—'), 'reviewer': meta.get('reviewer', '—'),
                         'created_at': meta.get('created_at', ''), 'updated_at': '', 'requirement': '',
                         'resources': {'rss_bytes': 0, 'process_count': 0, 'cpu_percent': None},
                         'modules': [], 'accepted_modules': 0, 'total_modules': 0, 'summary': '',
                         'activity': {'events': [], 'focus': '运行数据读取失败：' + str(exc)},
                         'directions': [], 'can_direct': False, 'actions': [], 'checks': [],
                         'audit_issues': {}, 'control': '', 'resume_unavailable': '运行数据需要修复：' + str(exc)})

    def _view_data(self, meta: dict, processes: dict, *, include_details: bool = True) -> dict:
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
            if state and not live:
                raise
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
        if status == 'paused' and control != 'pause':
            status = 'running'
            health = {**health, 'phase': 'resuming'}
        active = status in {'running', 'paused'}
        actions = []
        resume_unavailable = ''
        if meta.get('managed') and status == 'queued':
            actions = ['start', 'cancel']
        elif active and control != 'stop':
            actions = ['continue' if status == 'paused' or control == 'pause' else 'pause', 'stop']
        elif status in RESUMABLE and not live and (run / 'state.json').is_file():
            try:
                self._resume_args(meta, state)
                actions = ['resume']
            except TaskError as exc:
                resume_unavailable = str(exc)
        if (not live and status not in {'running', 'starting', 'paused', 'unknown', 'recovery_failed', 'interrupted'}
                and not health.get('lock_held') and not health.get('guardian_lock_held')
                and not (run / 'process.json').exists()):
            actions.append('delete')
        if meta.get('managed') and (status == 'queued' or 'resume' in actions):
            actions.append('roles')
        if 'resume' in actions:
            actions.append('checks')
        control_message = ''
        if meta.get('error') and not live:
            control_message = '上次启动失败：' + str(meta['error'])
        elif active:
            if control == 'stop':
                control_message = '正在停止执行并保存检查点…'
            elif control == 'pause' and status != 'paused':
                control_message = '暂停已请求，当前轮次结束后暂停；可取消暂停。'
            elif status == 'paused':
                control_message = '已暂停，点击恢复执行后继续原任务。'
            elif health.get('phase') == 'resuming':
                control_message = '恢复已请求，正在继续原任务。'
        elif status == 'starting':
            control_message = '正在启动监督器并加载任务检查点…'
        from auto_moniter.failures import classify
        failure_records=state.get('rounds') if isinstance(state.get('rounds'),list) else state.get('records',[])
        latest_failure = next((r.get('failure') or ' '.join(r.get('gaps', [])) for r in reversed(failure_records)
                               if r.get('role') == 'worker'), '')
        if not live and latest_failure and classify(latest_failure)[0] == 'authentication':
            name = meta.get('provider') or (provider.get('worker') or {}).get('name', '执行器')
            control_message = (f'{name} 认证不可用，worker 尚未开始实现。请在运行控制台的同一用户下完成登录，或为服务配置对应 API 凭据，再恢复任务。'
                               + (' Grok 可执行 grok login --device-code，或在控制台服务环境提供 XAI_API_KEY。' if name=='grok' else ''))
        if status == 'queued' and not health.get('review_schedule'):
            health = {**health, 'review_schedule': {'enabled': True, 'every_worker_rounds': meta.get('review_every', 5),
                                                   'rounds_since_review': 0}}
        current_provider = (provider.get('reviewer' if health.get('phase') in {'reviewing','refining_requirements'} else 'worker') or {}).get('name')
        progress = (self.activity.snapshot(run, state, health, status, current_provider or meta.get('provider', ''))
                    if include_details else {'events': [], 'role': 'reviewer' if health.get('phase') in {'reviewing','refining_requirements'} else 'worker',
                                             'round': health.get('rounds', 0), 'focus': ''})
        try:
            directions = Directions(run).snapshot() if include_details else []
            direction_error = ''
        except (ValueError, OSError) as exc:
            directions, direction_error = [], str(exc)
        can_direct = not direction_error and status in {'queued', 'starting', 'running', 'paused', *RESUMABLE}
        delivery = 'next_turn' if state.get('directions_version') == 1 else 'on_start'
        if active and state.get('directions_version') != 1:
            delivery = 'restart_required'
        rounds = state.get('rounds')
        if not isinstance(rounds, list):
            rounds = state.get('records') or []
        work_state = state if include_details else {**state, 'rounds': [], 'records': []}
        work = work_details(work_state, health, progress, status, heartbeat, guardian,
                            meta.get('provider') or (provider.get('worker') or {}).get('name', '—'),
                            meta.get('reviewer') or (provider.get('reviewer') or {}).get('name', '—'))
        failure = diagnose(status, state, meta, health, guardian, run, details=include_details)
        if failure:
            work['roles']['watcher']['current_work'] = failure['title'] + '：' + failure['reason']
        acceptance_results = read_json(run/'acceptance-results.json') if include_details else {}
        delivery_diagnosis = state.get('delivery_diagnosis') or {}
        if (include_details and not delivery_diagnosis and status=='budget_exceeded'
                and failure and failure['code'] in {'runtime_budget','runtime_handoff'}):
            from auto_moniter.delivery_diagnosis import diagnose as diagnose_delivery
            delivery_diagnosis = diagnose_delivery(rounds, acceptance=acceptance_results)
        return safe({'id': meta['id'], 'title': title[:160], 'requirement': requirement,
                     'cwd': meta.get('cwd') or state.get('cwd', ''), 'run_dir': str(run),
                     'managed': bool(meta.get('managed')), 'status': status, 'health': health,
                     'provider': meta.get('provider') or (provider.get('worker') or {}).get('name', '—'),
                     'reviewer': meta.get('reviewer') or (provider.get('reviewer') or {}).get('name', '—'),
                     'model': meta.get('model', ''), 'reviewer_model': meta.get('reviewer_model', ''),
                     'paseo_backend': meta.get('paseo_backend', 'claude'),
                     'reviewer_paseo_backend': meta.get('reviewer_paseo_backend', '') or meta.get('paseo_backend', 'claude'),
                     'role_changes': meta.get('role_changes', []) if include_details else [],
                     'created_at': meta.get('created_at') or state.get('updated_at', ''),
                     'updated_at': state.get('updated_at') or meta.get('created_at', ''),
                     'started_at': meta.get('started_at'), 'resources': resources,
                     'modules': modules, 'accepted_modules': accepted, 'total_modules': len(modules),
                     'summary': state.get('summary') or (state.get('last_worker') or {}).get('summary', ''),
                     'failure': failure,
                     'acceptance_warning': meta.get('acceptance_warning', ''),
                     'last_worker': state.get('last_worker'), 'last_review': state.get('last_review'),
                     'git_history': state.get('git_history', {'enabled': False}),
                     'git_checkpoints': [{**item['git_checkpoint'], 'summary': present_conclusion(item).get('summary', ''),
                                          'status': item.get('status', '')}
                                         for item in rounds if item.get('git_checkpoint')],
                     'audit_issues': state.get('audit_issues', {}), 'control': control,
                     'acceptance_plan': meta.get('acceptance_plan', {}) if include_details else {},
                     'acceptance_results': acceptance_results,
                     'verification_mode': meta.get('verification_mode', 'legacy'),
                     'function_dispatch': state.get('function_dispatch', meta.get('function_dispatch', False)),
                     'function_tasks': state.get('function_tasks', {}) if include_details else {},
                     'delivery_diagnosis': delivery_diagnosis if include_details else {},
                     'workflow':state.get('workflow',meta.get('workflow','legacy')),
                     'requirements_review':state.get('requirements_review',{}) if include_details else {},
                     'quality_policy': state.get('quality_policy', meta.get('quality_policy', {})) if include_details else {},
                     'quality_state': state.get('quality_state', {}) if include_details else {},
                     'requirement_coverage': read_json(run / 'requirement-coverage.json') if include_details else {},
                     'activity': progress,
                     'work': work,
                     'control_message': control_message, 'resume_unavailable': resume_unavailable,
                     'launch_error': meta.get('error', '') if not live else '',
                     'directions': directions, 'can_direct': can_direct,
                     'direction_delivery': delivery, 'direction_error': direction_error,
                     'actions': actions, 'checks': meta.get('checks', state.get('checks', []))})

    @staticmethod
    def summary(view: dict) -> dict:
        """Allowlisted task metadata; streams and task instructions stay in detail APIs."""
        keys = ('id', 'title', 'cwd', 'status', 'provider', 'reviewer', 'created_at', 'updated_at',
                'resources', 'accepted_modules', 'total_modules', 'actions', 'control', 'control_message',
                'can_direct', 'resume_unavailable')
        result = {key: view.get(key) for key in keys}
        result['health'] = {key: view.get('health', {}).get(key) for key in
                            ('health', 'phase', 'rounds', 'heartbeat_age_s', 'usage')}
        result['summary'] = str(view.get('summary') or (view.get('last_worker') or {}).get('summary') or '')[:500]
        result['failure'] = {key:view['failure'].get(key) for key in ('code','title','reason','phase','role','round')} if view.get('failure') else None
        result['roles'] = {role: {'status': item.get('status'), 'current_work': str(item.get('current_work') or '')[:300]}
                           for role, item in (view.get('work', {}).get('roles') or {}).items()}
        return result

    def task_summaries(self) -> dict:
        with self._lock:
            sample = self.metrics.sample()
            tasks = [self.summary(self._view(meta, sample['processes'], include_details=False))
                     for meta in self._catalog().values()]
            tasks.sort(key=lambda item: item.get('created_at') or '', reverse=True)
            return {'tasks': tasks, 'at': timestamp(), 'discovery': {'warnings': self._scan_warnings}}

    def dashboard(self) -> dict:
        with self._lock:
            data = self.task_summaries()
            tasks = data['tasks']
            counts = {'total': len(tasks), 'running': sum(t['status'] in {'running', 'starting'} for t in tasks),
                      'paused': sum(t['status'] == 'paused' for t in tasks),
                      'done': sum(t['status'] == 'done' for t in tasks)}
            sample = self.metrics.sample()
            return {**data, 'tasks': [t for t in tasks if t['status'] in {'running', 'starting', 'paused'}],
                    'counts': counts, 'resources': {k: v for k, v in sample.items() if k != 'processes'}}

    def resource_overview(self) -> dict:
        with self._lock:
            sample = self.metrics.sample()
            tasks = [self.summary(self._view(meta, sample['processes'], include_details=False))
                     for meta in self._catalog().values()]
            return {'resources': {k: v for k, v in sample.items() if k != 'processes'}, 'at': timestamp(),
                    'tasks': [{k: t[k] for k in ('id', 'title', 'status', 'resources')} for t in tasks]}

    def progress(self, ident: str) -> dict:
        view = self.detail(ident)
        keys = ('id', 'title', 'cwd', 'status', 'health', 'activity', 'work', 'actions', 'control',
                'control_message', 'can_direct', 'provider', 'reviewer', 'resume_unavailable', 'failure')
        result = {key: view.get(key) for key in keys}
        work = view.get('work') or {}
        result['work'] = {**work, 'conclusions': [next((c for c in reversed(work.get('conclusions', []))
                                  if c.get('role') == role), None) for role in ('worker', 'reviewer')]}
        result['work']['conclusions'] = [c for c in result['work']['conclusions'] if c]
        return result

    def overview(self) -> dict:
        with self._lock:
            sample = self.metrics.sample()
            tasks = [self._view(meta, sample['processes']) for meta in self._catalog().values()]
            tasks.sort(key=lambda item: item['created_at'], reverse=True)
            return {'tasks': tasks, 'resources': {key: value for key, value in sample.items() if key != 'processes'},
                    'discovery': {'roots': list(dict.fromkeys(str(p) for p in self.runs_dirs)), 'warnings': self._scan_warnings},
                    'at': timestamp()}

    def add_direction(self, ident: str, data: dict) -> dict:
        with self._lock:
            meta = self._get(ident)
            view = self._view(meta, {})
            if not view['can_direct']:
                raise TaskError('该任务已结束，请创建后续任务', 409)
            pause = data.get('pause', False)
            if type(pause) is not bool:
                raise TaskError('pause 必须是布尔值')
            if pause and 'pause' not in view['actions']:
                raise TaskError('当前任务无法请求暂停，请刷新状态后重试', 409)
            try:
                Directions(Path(meta['run_dir'])).append(data.get('text'))
            except ValueError as exc:
                raise TaskError(str(exc)) from exc
            if pause:
                write_json(Path(meta['run_dir']) / 'control.json', {'action': 'pause'}, durable=True)
            return self.detail(ident)

    def detail(self, ident: str) -> dict:
        with self._lock:
            return self._view(self._get(ident), self.metrics.sample()['processes'])

    def git_record(self, ident: str, commit: str) -> dict:
        view = self.detail(ident)
        history = view.get('git_history') or {}
        candidates = [*view.get('git_checkpoints', []), history.get('last_worker', {}), history.get('last_record', {})]
        checkpoint = next((item for item in candidates if item.get('commit') == commit), None)
        if not checkpoint:
            raise TaskError('该提交不在本任务的近期 Git 记录中', 404)
        try:
            return read_git_record(Path(view['cwd']), history, checkpoint)
        except GitRecordError as exc:
            raise TaskError(str(exc), 409) from exc

    def acceptance_options(self, data: dict, *, prepare=False) -> dict:
        try:
            cwd = data.get('cwd', str(self.cwd))
            if not isinstance(cwd, str) or not cwd or '\0' in cwd or len(cwd)>4000:
                raise ValueError('项目路径格式无效')
            if prepare:
                plan = data.get('acceptance_plan')
                if isinstance(plan,dict) and not plan.get('criteria'):
                    plan = {**plan,'criteria':'仅检查运行前提，不创建任务或执行测试'}
                return acceptance_preflight(select_acceptance(cwd, plan))
            return scan_acceptance(cwd, data.get('task_type','code'), data.get('python',''))
        except (ValueError,OSError) as exc:
            raise TaskError(str(exc)) from exc

    def create(self, data: dict) -> dict:
        def text(key, default='', maximum=32000):
            value = data.get(key, default)
            if not isinstance(value, str) or '\x00' in value or len(value) > maximum:
                raise TaskError(f'{key} 格式或长度不正确')
            return value.strip()
        title, requirement = text('title', maximum=160), text('requirement')
        if not title or not requirement:
            raise TaskError('请填写任务名称和开发需求')
        cwd_text = text('cwd', str(self.cwd), maximum=4000)
        if not cwd_text:
            raise TaskError('请填写项目目录；留空不会自动选择项目，请使用项目的完整路径')
        try:
            cwd = Path(cwd_text).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise TaskError('项目目录无法解析，请检查路径、符号链接和访问权限') from exc
        if not cwd.is_dir() or not os.access(cwd, os.W_OK):
            raise TaskError('项目目录不存在或不可写')
        provider, reviewer = text('provider', 'codex'), text('reviewer', 'claude')
        models = {key: text(key, maximum=200) for key in ('model', 'reviewer_model')}
        profiles=read_settings(self.agents.path).get('agents',{})
        for key,name in [('model',provider),('reviewer_model',reviewer)]:
            if not models[key]:models[key]=profiles.get(name,{}).get('model','')
        backends = {'paseo_backend': text('paseo_backend', DEFAULT_PASEO_BACKEND, 200),
                    'reviewer_paseo_backend': text('reviewer_paseo_backend', '', 200)}
        workflow=text('workflow','review-first')
        if workflow not in ('review-first','legacy'):raise TaskError('工作流程无效')
        function_dispatch = data.get('function_dispatch',
            workflow == 'review-first' and data.get('verification_mode') not in ('review_only', 'planning'))
        if type(function_dispatch) is not bool or function_dispatch and workflow != 'review-first':
            raise TaskError('逐函数下发需要启用先审核需求的流程')
        if provider not in PROVIDERS or reviewer not in PROVIDERS:
            raise TaskError('请选择支持的执行器和审查器')
        guided = None
        if data.get('acceptance_plan') is not None:
            try:
                guided = select_acceptance(cwd, data['acceptance_plan'])
            except (ValueError,OSError) as exc:
                raise TaskError(str(exc)) from exc
        checks = data.get('checks', [])
        if not isinstance(checks, list) or len(checks) > 20 or any(not isinstance(c, str) or not c.strip() or len(c) > 2000 or '\x00' in c for c in checks):
            raise TaskError('验收命令格式不正确，每行填写一条命令')
        # Distinguish explicit mode from legacy clients that omit verification_mode.
        mode_explicit = 'verification_mode' in data
        verification_mode = data.get('verification_mode',
            'executable' if function_dispatch else 'review_only')
        if verification_mode not in ('executable', 'review_only', 'planning'):
            raise TaskError('验收模式无效')
        if function_dispatch and mode_explicit and verification_mode != 'executable':
            raise TaskError('逐函数派单需要执行验收模式及已批准的检查')
        # Module-level checks also schedule sh -lc execution via ModuleLedger; inspect raw
        # payload before settings()/save so contradictory modes never leave a half task.
        module_check_commands = _module_check_commands(data.get('modules', []))
        if verification_mode == 'planning':
            checks = []
            if guided:
                guided = {**guided,'selected_ids':[],'selected':[]}
            if module_check_commands:
                raise TaskError(
                    '制定验收方案模式不能附带模块执行检查；请清除 modules[].checks，'
                    '或改用执行验收模式（不会静默删除固定模块命令）'
                )
            requirement = ('本任务仅制定可执行的验收方案，不实施原目标、不安装依赖、不运行部署。'
                           '阅读项目已有验证入口，列出需求到检查项、所需环境、固定样本、成功/失败断言和仍缺的测试资产；'
                           '将方案写入项目文档并交独立 reviewer 审核。完成本任务不代表原目标通过验收。\n\n原目标：\n' + requirement)
        elif verification_mode == 'review_only' and mode_explicit:
            # Explicit semantic-review-only must not schedule execution via top-level checks,
            # acceptance selected_ids, or modules[].checks. Reject before save/launch.
            selected_ids = list((guided or {}).get('selected_ids') or [])
            if selected_ids or checks or module_check_commands:
                raise TaskError(
                    '仅独立语义审核不能附带执行检查；请切换到执行验收模式，或清除已选检查项、'
                    '高级命令与 modules[].checks 后再提交（不会静默删除固定模块命令）'
                )
            if guided:
                guided = {**guided, 'selected_ids': [], 'selected': []}
            checks = []
        if mode_explicit and verification_mode == 'executable' and not checks and not module_check_commands and not (guided and guided['selected']):
            raise TaskError('独立命令验收模式至少需要一条验收命令；仅文档审核请明确选择仅审核模式')
        numbers = {}
        for key, default, maximum in [('max_runtime_minutes', 120, 43200), ('max_agent_calls', 100, 100000),
                                      ('max_rounds', 0, 100000), ('review_every', 5, 100000)]:
            value = data.get(key, default)
            if type(value) is not int or value < 0 or value > maximum:
                raise TaskError(f'{key} 必须是 0 到 {maximum} 的整数')
            numbers[key] = value
        auto_start = data.get('auto_start', False)
        if type(auto_start) is not bool:
            raise TaskError('auto_start 必须是布尔值')
        try:
            # Validate the same normalized role/backend values that are saved and launched.
            execution = settings({**data, 'provider': provider, 'reviewer': reviewer, **backends}, cwd)
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            raise TaskError(str(exc)) from exc
        if execution['worker_isolation'] == 'strict' and self.runs_dir.is_relative_to(cwd):
            raise TaskError('strict worker 的运行目录必须位于目标项目外；请使用外部 --data-dir', 409)
        with self._lock:
            ident = uuid.uuid4().hex
            run = self.runs_dir / ident
            if guided:
                import shlex
                selected_commands = [shlex.join([token.replace('{run_dir}', str(run)) for token in shlex.split(c['command'])]) for c in guided['selected']]
                for item, command in zip(guided['selected'], selected_commands):
                    item['command'] = command
                checks = list(dict.fromkeys([*selected_commands, *checks]))
                if verification_mode == 'planning':
                    checks = []
                if len(checks)>20:
                    raise TaskError('最多选择或填写 20 条验收检查')
            scopes = execution.get('quality_policy', {}).get('integration_scopes', [])
            if scopes:
                if not function_dispatch:
                    raise TaskError('集成任务需要开启逐函数派单与执行验收')
                from auto_moniter.function_gate import validate_approvals
                try:
                    validate_approvals(scopes, cwd, {m['id']: m for m in execution['modules']}, checks)
                except (ValueError, OSError) as exc:
                    raise TaskError(str(exc)) from exc
            meta = {'id': ident, 'managed': True, 'title': title, 'requirement': requirement,
                    'cwd': str(cwd), 'provider': provider, 'reviewer': reviewer, 'workflow':workflow,
                    'function_dispatch': function_dispatch,
                    **models, **backends,
                    'checks': [c.strip() for c in checks], 'verification_mode': verification_mode, **numbers, **execution,
                    'run_dir': str(self.runs_dir / ident), 'created_at': timestamp(), 'status': 'queued'}
            from console.acceptance import coverage_warning
            meta['acceptance_warning'] = coverage_warning([*checks, *module_check_commands])
            if guided:
                meta['acceptance_plan'] = guided
                meta['acceptance'] = ('验收方案须说明如何验证以下目标，不要求在本任务实现或证明原功能已通过：\n' if verification_mode=='planning' else '') + guided['criteria']
                if 'ndr-documentation' in guided['selected_ids'] and run.is_relative_to(cwd):
                    raise TaskError('文档只读验收的基线须在目标项目外，请使用外部控制台 --data-dir',409)
            if auto_start:
                self._check_start(meta)
            self._save(meta)
            if auto_start:
                self._launch(meta)
            return self.detail(ident)

    def _check_start(self, meta: dict) -> None:
        available = self.agents.providers(discover_providers())
        for role in ('provider', 'reviewer'):
            if meta.get(role) and meta[role] != '—' and not available.get(meta[role]):
                raise TaskError(f'未找到 {meta[role]} 可执行文件，请先安装并完成登录', 409)
        if not Path(meta['cwd']).is_dir():
            raise TaskError('项目目录已不存在', 409)
        if meta.get('acceptance_plan'):
            plan = meta['acceptance_plan']
            if not (Path(meta['run_dir'])/'state.json').exists():
                try:
                    select_acceptance(meta['cwd'], plan)
                except (OSError,ValueError) as exc:
                    raise TaskError(str(exc),409) from exc
            result = acceptance_preflight(plan) if meta.get('verification_mode') != 'planning' else {'ready':True,'checks':[],'executed':False}
            meta['acceptance_preflight'] = result
            if not result['ready']:
                raise TaskError('验收环境不可用（未执行业务检查）：'+ '; '.join(error for item in result['checks'] for error in item['errors']),409)
        if not (Path(meta['run_dir']) / 'state.json').exists():
            from auto_moniter.isolation import ReviewIsolation, WorkerIsolation
            from auto_moniter.providers import resolve_provider
            try:
                resolve_provider(meta['provider'],settings_path=self.agents.path)
                reviewer = resolve_provider(meta['reviewer'], role='reviewer',
                                             settings_path=self.agents.path,
                                             paseo_backend=meta.get('reviewer_paseo_backend') or meta.get('paseo_backend', 'claude'))
                reviewer.validate_review()
                if meta.get('review_isolation') == 'strict':
                    ReviewIsolation(Path(meta['cwd']), Path(meta['run_dir'])).verify(meta['reviewer'])
                if meta.get('worker_isolation') == 'strict':
                    if Path(meta['run_dir']).is_relative_to(Path(meta['cwd'])):
                        raise ProviderError('strict worker 的运行目录必须位于目标项目外；请使用外部 --data-dir')
            except ProviderError as exc:
                raise TaskError('启动预检失败：' + str(exc), 409) from exc
        for other in self._catalog().values():
            if other['id'] == meta['id']:
                continue
            view = self._view(other, {})
            if view['status'] in {'running', 'starting', 'paused'} and workspaces_overlap(Path(view['cwd']), Path(meta['cwd'])):
                raise TaskError('该项目已有任务执行中，请先停止或等待完成', 409)

    def _resume_args(self, meta: dict, state: dict | None = None) -> list[str]:
        run = Path(meta['run_dir'])
        state = state if state is not None else read_json(run / 'state.json')
        if state.get('status') not in {'running', 'stopped', 'blocked', 'budget_exceeded', 'recovery_failed', 'action_required'}:
            raise TaskError('检查点状态不支持恢复，请新建任务', 409)
        if not (run / 'requirement.md').is_file():
            raise TaskError('缺少原始需求检查点，无法恢复', 409)
        manifest = {'argv': meta['resume_argv']} if meta.get('resume_argv') else read_json(run / 'launch.json')
        if not manifest:
            if meta.get('managed'):
                fallback = meta
                if 'review_every' not in meta:
                    saved_every = (state.get('review_schedule') or {}).get('every_worker_rounds', 5)
                    if type(saved_every) is not int or saved_every < 0:
                        raise TaskError('保存的审核频率无效，请修复检查点后恢复', 409)
                    fallback = {**meta, 'review_every': saved_every}
                manifest = {'argv': self._new_args(fallback)}
            else:
                raise TaskError('缺少原始启动配置 launch.json，请使用原 CLI 配置恢复', 409)
        try:
            options={'settings_path':self.agents.path} if self.agents.path.exists() else {}
            return resume_arguments(manifest, run, state, **options)
        except (ValueError, ProviderError, TypeError, KeyError, OSError) as exc:
            raise TaskError('无法恢复：' + str(exc), 409) from exc

    def _new_args(self, meta: dict) -> list[str]:
        argv = ['run', '--cwd', meta['cwd'], '--run-dir', meta['run_dir'],
                '--provider', meta['provider'], '--reviewer', meta['reviewer'],
                '--git-history', 'required',
                '--review-every', str(meta.get('review_every', 5)),
                '--max-runtime', str(meta['max_runtime_minutes'] * 60),
                '--max-agent-calls', str(meta['max_agent_calls']), '--max-rounds', str(meta['max_rounds']), '--guard']
        for key, flag in [('model', '--model'), ('reviewer_model', '--reviewer-model'), ('paseo_backend', '--paseo-backend'), ('reviewer_paseo_backend', '--reviewer-paseo-backend')]:
            if meta.get(key):
                argv.append(flag + '=' + meta[key])
        for check in meta['checks']:
            argv.append('--check=' + check)
        argv.append('--workflow='+meta.get('workflow','legacy'))
        argv.append('--function-dispatch' if meta.get('function_dispatch') else '--no-function-dispatch')
        if meta.get('acceptance'):
            argv.append('--accept=' + meta['acceptance'])
        # Legacy metadata keeps the old provider boundary; new tasks store an
        # explicit strict default in create(). Never silently change a resume.
        for key in DEFAULTS:
            if key in meta:
                argv.append('--' + key.replace('_', '-') + '=' + str(meta[key]))
        for name in meta.get('protect_paths', []):
            argv.append('--protect=' + name)
        if meta.get('modules'):
            argv += ['--modules', str(Path(meta['run_dir']) / 'modules.json')]
        if meta.get('quality_policy'):
            argv += ['--quality-policy', str(Path(meta['run_dir']) / 'quality-policy.json')]
        return argv

    def _launch(self, meta: dict, resume: bool = False) -> None:
        run = Path(meta['run_dir'])
        run.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Requirement is supplied by stdin, so even text beginning with '-' stays literal.
        arguments = self._resume_args(meta) if resume else self._new_args(meta)
        argv = [sys.executable, '-m', 'auto_moniter', *arguments]
        env = os.environ.copy()
        env['AUTO_MONITER_AGENT_SETTINGS']=str(self.agents.path)
        package_root = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = package_root + os.pathsep + env.get('PYTHONPATH', '')
        env['PYTHONUNBUFFERED'] = '1'
        previous_control = read_json(run / 'control.json') if resume else {}
        try:
            if resume:
                (run / 'control.json').unlink(missing_ok=True)
            else:
                if meta.get('acceptance_plan'):
                    write_json(run/'acceptance-plan.json', meta['acceptance_plan'], durable=True)
                    if 'ndr-documentation' in meta['acceptance_plan']['selected_ids']:
                        if run.is_relative_to(Path(meta['cwd'])):
                            raise TaskError('文档只读验收的基线须在目标项目外，请使用外部控制台 --data-dir',409)
                        import gzip
                        from auto_moniter.workspace import file_snapshot
                        baseline = file_snapshot(Path(meta['cwd']))
                        (run/'documentation-baseline.json.gz').write_bytes(gzip.compress(json.dumps(baseline).encode()))
                if meta.get('modules'):
                    write_json(run / 'modules.json', meta['modules'], durable=True)
                if meta.get('quality_policy'):
                    write_json(run / 'quality-policy.json', meta['quality_policy'], durable=True)
                write_json(run / 'launch.json', {'argv': arguments}, durable=True)
            with (run / 'console.log').open('ab') as output:
                child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                         cwd=package_root, env=env, start_new_session=True)
            # communicate() runs off the request thread and reaps the detached CLI on exit.
            meta.update(status='starting', started_at=timestamp(), launcher=process_identity(child.pid))
            meta.pop('error', None)
            meta.pop('exit_summary', None)
            self._children[meta['id']] = child
            self._save(meta)
            threading.Thread(target=self._wait, args=(meta['id'], child, meta['requirement']), daemon=True).start()
        except OSError as exc:
            if previous_control:
                write_json(run / 'control.json', previous_control, durable=True)
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
                    result = console_result(Path(meta['run_dir']))
                    if result:
                        meta['exit_summary'] = result['summary']
                    self._save(meta)
                self._children.pop(ident, None)

    def action(self, ident: str, action: str, data: dict | None = None) -> dict:
        with self._lock:
            meta = self._get(ident)
            view = self._view(meta, {})
            if action not in view['actions']:
                raise TaskError('当前状态不支持此操作，请刷新后重试', 409)
            run = Path(meta['run_dir'])
            if action == 'checks':
                additions = (data or {}).get('additional_checks')
                if (not isinstance(additions, list) or not additions or len(additions) > 20
                        or any(not isinstance(c, str) or not c.strip() or len(c) > 2000 or '\0' in c for c in additions)):
                    raise TaskError('请填写要追加的验收命令，每条最多 2000 字')
                try:
                    with inactive_run(run):
                        state = read_json(run / 'state.json')
                        arguments = self._resume_args(meta, state)
                        old = list(state.get('checks') or [])
                        combined = list(old)
                        for command in additions:
                            if command.strip() not in combined:
                                combined.append(command.strip())
                        if len(combined) > 20:
                            raise ValueError('最多保留 20 条验收命令')
                        if combined == old:
                            return self.detail(ident)
                        original = (run / 'state.json').read_bytes()
                        changes = state.get('check_changes', [])
                        if not isinstance(changes, list) or len(changes) >= 20:
                            raise ValueError('验收检查追加记录无效或已达到上限')
                        changes = list(changes)
                        changes.append({'at': timestamp(), 'added': combined[len(old):]})
                        state.update(checks=combined, check_changes=changes)
                        try:
                            write_json(run / 'state.json', state, durable=True)
                            self._save({**meta, 'checks': combined, 'resume_argv': arguments})
                        except OSError:
                            temporary = run / 'state.json.rollback'
                            temporary.write_bytes(original)
                            temporary.replace(run / 'state.json')
                            raise
                    from auto_moniter.events import record
                    record(run, 'acceptance_checks_added', added=combined[len(old):])
                except (OSError, ValueError) as exc:
                    raise TaskError('追加验收检查失败：' + str(exc), 409) from exc
                return self.detail(ident)
            if action == 'roles':
                from console.roles import prepare
                from auto_moniter.role_changes import normalize_provider_config
                role_change_path = run / 'role-change.json'
                # None = not written yet; False = wrote over empty; bytes = prior authorization backup.
                role_change_backup = None
                try:
                    with inactive_run(run):
                        state = read_json(run / 'state.json')
                        arguments = self._resume_args(meta, state) if state else self._new_args(meta)
                        arguments, values, resolved = prepare(arguments, data or {}, Path(meta['cwd']),settings_path=self.agents.path)
                        policy = state.get('quality_policy', meta.get('quality_policy', {}))
                        if policy.get('review_tools') == 'files_only' and values['reviewer'] != 'claude':
                            raise ValueError('当前只读文件审核策略要求 Claude reviewer')
                        available = self.agents.providers(discover_providers())
                        for role in ('provider', 'reviewer'):
                            if not available.get(values[role]):
                                raise ValueError('未找到执行器：' + values[role])
                        origin = normalize_provider_config(state.get('provider_config')) if state else None
                        assignment = {'id': uuid.uuid4().hex, 'at': timestamp(),
                                      'from': origin, 'to': resolved,
                                      'models': {'worker': values['model'], 'reviewer': values['reviewer_model']}}
                        if state:
                            # Keep the prior authorization if metadata save fails after overwrite.
                            if role_change_path.is_file() and not role_change_path.is_symlink():
                                role_change_backup = role_change_path.read_bytes()
                            else:
                                role_change_backup = False
                            write_json(role_change_path, assignment, durable=True)
                            meta['resume_argv'] = arguments
                        meta.update(values)
                        meta['role_changes'] = (meta.get('role_changes', []) + [assignment])[-20:]
                        self._save(meta)
                except (ValueError, OSError, ProviderError) as exc:
                    if role_change_backup is not None:
                        try:
                            if role_change_backup is False:
                                role_change_path.unlink(missing_ok=True)
                            else:
                                role_change_path.write_bytes(role_change_backup)
                        except OSError:
                            pass
                    raise TaskError(str(exc), 409) from exc
                return self.detail(ident)
            if action == 'delete':
                if not data or data.get('confirm') != ident:
                    raise TaskError('请确认删除此任务', 400)
                if self._launcher_alive(meta):
                    raise TaskError('任务仍在启动或执行，请先停止', 409)
                child = self._children.get(ident)
                if child is not None and child.poll() is None:
                    raise TaskError('任务进程尚未退出，请稍后重试', 409)
                try:
                    validate_log_location(run, Path(view['cwd']))
                    with inactive_run(run) as directory:
                        cleanup = purge_run_logs(directory)
                        marker = self.deleted_dir / (run_key(run) + '.json')
                        write_json(marker, {'id': ident, 'run_dir': str(run.resolve()),
                                          'deleted_at': timestamp(), 'logs_cleaned': True, **cleanup}, durable=True)
                        try:
                            (self.tasks_dir / (ident + '.json')).unlink(missing_ok=True)
                        except OSError:
                            marker.unlink(missing_ok=True)
                            raise
                except ValueError as exc:
                    raise TaskError(str(exc), 409) from exc
                except OSError as exc:
                    raise TaskError(f'任务日志清理或删除失败，请重试：{exc}', 500) from exc
                if hasattr(self, '_log_reader'):
                    self._log_reader.forget(run)
                self._external.pop(ident, None)
                self._catalog_at = 0
                return {'id': ident, 'deleted': True, 'logs_cleaned': True, **cleanup}
            if action in {'start', 'resume'}:
                if action == 'resume' and not meta.get('managed'):
                    # Keep the discovered task ID and its original run directory.
                    state = read_json(run / 'state.json')
                    arguments = self._resume_args(meta, state)
                    meta = {**meta, 'managed': True, 'imported': True, 'title': view['title'],
                            'requirement': view['requirement'], 'cwd': view['cwd'],
                            'provider': view['provider'], 'reviewer': view['reviewer'],
                            'created_at': view['created_at'], 'resume_argv': arguments}
                self._check_start(meta)
                if action == 'resume':
                    arguments = self._resume_args(meta)
                    usage = read_json(run / 'state.json').get('usage', {})
                    additions = data or {}
                    for key, flag, used, scale in [
                        ('additional_runtime_minutes', '--max-runtime', float(usage.get('elapsed_s', 0)), 60),
                        ('additional_agent_calls', '--max-agent-calls', int(usage.get('agent_calls', 0)), 1)]:
                        value = additions.get(key, 0)
                        if type(value) is not int or not 0 <= value <= 100000:
                            raise TaskError('追加预算必须为 0 至 100000 的整数')
                        if value:
                            from auto_moniter.cli import build_parser, _parse_duration
                            parsed = build_parser().parse_args(arguments)
                            current = _parse_duration(parsed.max_runtime) if scale == 60 else parsed.max_agent_calls
                            total = max(current, int(used) + 1) + value * scale
                            arguments.append(flag + '=' + str(total))
                    from auto_moniter.cli import build_parser, _parse_duration
                    parsed = build_parser().parse_args(arguments)
                    if (_parse_duration(parsed.max_runtime) and float(usage.get('elapsed_s', 0)) >= _parse_duration(parsed.max_runtime)
                            or parsed.max_agent_calls and int(usage.get('agent_calls', 0)) >= parsed.max_agent_calls):
                        raise TaskError('累计预算已耗尽，请在恢复窗口明确追加预算', 409)
                    meta = {**meta, 'resume_argv': arguments}
                    # Only save the new launch configuration once Popen succeeds.
                self._launch(meta, resume=action == 'resume')
            elif action == 'cancel':
                meta['status'] = 'cancelled'
                self._save(meta)
            elif action == 'continue':
                (run / 'control.json').unlink(missing_ok=True)
            else:
                reason = (data or {}).get('reason', '')
                if not isinstance(reason, str) or len(reason) > 500 or '\0' in reason:
                    raise TaskError('停止或暂停原因必须为 500 字以内的文字')
                write_json(run / 'control.json', {'action': action, **({'reason': reason.strip()} if reason.strip() else {})}, durable=True)
            return self.detail(ident)

    def _log_selection(self, ident: str, name: str):
        run = Path(self._get(ident)['run_dir'])
        files = []
        for path in run.glob('*'):
            if path.is_symlink() or not path.is_file():
                continue
            if path.name not in {'console.log', 'monitor.log', 'events.jsonl', 'events.previous.jsonl', 'requirements-workspace-change.json'} and not re.fullmatch(r'\d+-(?:(worker|reviewer(?:-repair-\d+)?)(-text)?\.(log|md)|check-\d+\.log)', path.name):
                continue
            try:
                files.append((path.stat().st_mtime, path.name))
            except FileNotFoundError:
                continue  # The supervisor can rotate/prune logs during enumeration.
        names = [name for _, name in sorted(files, reverse=True)]
        selected = name or next((n for n in names if '-text.md' in n), names[0] if names else '')
        if selected and selected not in names:
            raise TaskError('日志文件不存在', 404)
        return run, names, selected

    def log_text(self, ident: str, name: str) -> str:
        """Read an explicitly selected public artifact without preview truncation."""
        if not name:
            raise TaskError('请选择日志文件', 400)
        with self._lock:
            run, _, selected = self._log_selection(ident, name)
            try:
                with os.fdopen(os.open(run/selected, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
            except FileNotFoundError:
                raise TaskError('日志文件不存在', 404) from None
            if len(raw) > 8 * 1024 * 1024:
                raise TaskError('完整文本超过 8 MiB，请在运行记录目录读取原文件；未返回截断内容。', 413)
            return redact(raw.decode('utf-8', errors='replace'))

    def logs(self, ident: str, name: str = '', page: int = 1, page_size: int = 10) -> dict:
        if type(page) is not int or not 0 <= page <= 1000000000 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise TaskError('日志页码须为非负整数，每页数量须为 1 至 100', 400)
        with self._lock:
            run, names, selected = self._log_selection(ident, name)
            if not hasattr(self, '_log_reader'):
                self._log_reader = LogReader()
            try:
                data = self._log_reader.read(run / selected if selected else None, page, page_size)
            except FileNotFoundError:
                raise TaskError('日志文件不存在', 404) from None
            return {'files': names, 'selected': selected, **data}
