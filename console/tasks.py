"""Persist task definitions and delegate execution to the existing supervisor CLI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.health import confirms_follow_up_wait, delivery_recovery_warning, read_run_snapshot, run_health
from auto_moniter.control import ControlError, read_control
from auto_moniter.paths import state_root
from auto_moniter.processes import process_identity
from auto_moniter.providers import discover_providers
from auto_moniter.providers import DEFAULT_PASEO_BACKEND, ProviderError
from auto_moniter.locking import workspaces_overlap
from auto_moniter.workspace import LockError
from auto_moniter.check_history import configurations
from console.settings import settings, DEFAULTS
from console.deletion import run_key, inactive_run, purge_run_logs, validate_log_location
from console.acceptance import scan as scan_acceptance, select as select_acceptance, preflight as acceptance_preflight
from console.acceptance import validate_commands
from auto_moniter.redaction import redact, safe
from auto_moniter.storage import write_json
from auto_moniter.directions import Directions
from auto_moniter import browser_execution, candidate_checks, field_handoff, referee
from auto_moniter.requirement_queue import RequirementQueue
from console.metrics import Metrics, process_tree
from console.activity import ActivityReader
from console.recovery import resume_arguments
from console.workflow import work_details
from console.git_records import read_git_record, GitRecordError
from console.conclusions import present_conclusion
from console.logs import LogReader, PUBLIC_LOG_NAME
from console.task_timing import estimate as estimate_task_time
from console.agents import AgentManager
from auto_moniter.agent_settings import AGENTS, read_settings
from console.diagnostics import diagnose, console_result, present_summary
from console.launch import abort_start
from console import debug_preview, migration


PROVIDERS = tuple(AGENTS)
RESUMABLE = {'stopped', 'blocked', 'budget_exceeded', 'max_rounds', 'recovery_failed', 'action_required', 'interrupted'}
logger = logging.getLogger(__name__)


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


def _check_references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'check_ids' and isinstance(item, list):
                for identifier in item:
                    if isinstance(identifier, str) and re.fullmatch(r'check:[1-9][0-9]{0,3}', identifier):
                        yield int(identifier.split(':')[1]) - 1
            elif isinstance(item, (dict, list)):
                yield from _check_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _check_references(item)


def _removable_checks(state):
    checks = state.get('checks') or []
    original = configurations(checks, state.get('check_changes', []))[0]
    references = _check_references([state.get(key) for key in (
        'function_tasks', 'function_plan', 'quality_policy', 'requirements_review')])
    # Positional check IDs in saved assignments must keep referring to the same commands.
    start = max(len(original), max(references, default=-1) + 1)
    for item in state.get('check_source_aliases', []):
        match = re.fullmatch(r'check:([1-9][0-9]{0,3})=[A-Za-z0-9_-]+', item)
        if match:
            start = max(start, int(match[1]))
    return {hashlib.sha256(command.encode()).hexdigest(): index
            for index, command in enumerate(checks) if index >= start}


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
        catalog = discover(self.cwd, discover_providers(), live=True)
        with self._lock:
            self.agent_catalog = catalog
        return self.config()

    def agent_settings(self, action, data):
        try:
            if action=='save':
                result=self.agents.save(data)
                with self._lock:
                    self.agent_catalog=None
                return result
            if action=='install':return self.agents.install(data.get('id'))
            if action=='probe':return self.agents.probe(data.get('id'))
            raise ValueError('Agent 操作不支持')
        except (ValueError,OSError) as exc:
            raise TaskError(str(exc),400) from exc

    def config(self) -> dict:
        available = self.agents.providers(discover_providers())
        with self._lock:
            catalog_data = self.agent_catalog
        if catalog_data is None:
            from console.agent_catalog import discover
            catalog_data = discover(self.cwd, available)
            with self._lock:
                self.agent_catalog = catalog_data
        profiles={row['id']:row for row in self.agents.snapshot()['agents']}
        catalog=dict(catalog_data)
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
        with self._lock:
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
            cached_external = dict(self._external)
        managed_paths = {item['run_dir'] for item in tasks.values()}
        tasks.update({key: item for key, item in cached_external.items() if item['run_dir'] not in managed_paths})
        removed = {ident for ident, item in tasks.items()
                   if (self.deleted_dir / (run_key(item['run_dir']) + '.json')).exists()}
        for item in tasks.values():
            source = tasks.get(item.get('migrated_from'))
            if not source or item.get('status') == 'cancelled':
                continue
            if item['id'] not in removed:
                source['_migrated_to'] = item['id']
            elif item.get('started_at'):
                source['_migration_consumed'] = True
        return {ident: item for ident, item in tasks.items() if ident not in removed}

    def _get(self, ident: str) -> dict:
        meta = self._catalog().get(ident)
        if not meta:
            raise TaskError('任务不存在或运行目录已不可用', 404)
        return meta

    def _save(self, meta: dict) -> None:
        write_json(self.tasks_dir / (meta['id'] + '.json'),
                   {key: value for key, value in meta.items() if key not in {'_migrated_to', '_migration_consumed'}},
                   durable=True)
        if meta.get('parallel_project'):
            write_json(Path(meta['run_dir']) / 'project-lifecycle.json', {'status':meta['status']}, durable=True)

    def _launcher_alive(self, meta: dict) -> bool:
        child = self._children.get(meta['id'])
        if child is not None and child.poll() is None:
            return True
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
        state = {}
        status = meta.get('status', 'unknown')
        health = {'health': 'not_started', 'phase': status, 'rounds': 0, 'usage': {}}
        live = self._launcher_alive(meta)
        try:
            snapshot = read_run_snapshot(run)
            health = run_health(run, snapshot=snapshot)
            # A report is authoritative only once the supervisor and guardian have released their locks.
            if snapshot['finished']:
                state = {**read_json(run / 'state.json'), **snapshot['state']}
            else:
                state = dict(snapshot['state'])
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
            state = read_json(run / 'state.json')
            if state and not live:
                raise
            if live:
                status = 'starting'
            elif meta.get('started_at'):
                status = 'failed'
        heartbeat = read_json(run / 'heartbeat')
        guardian = health.get('guardian') or read_json(run / 'guardian.json')
        roots = []
        if live:
            child = self._children.get(meta['id'])
            roots.append(child.pid if child is not None else meta['launcher']['pid'])
        if health.get('lock_held') or health.get('guardian_lock_held'):
            roots.extend(pid for pid in (heartbeat.get('pid'), heartbeat.get('supervisor_pid'),
                                         heartbeat.get('child_pid'), guardian.get('guardian_pid')) if type(pid) is int)
        resources = process_tree(processes, roots)
        provider = state.get('provider_config') or {}
        requirement = meta.get('requirement') or tail(run / 'requirement.md', 32000).strip()
        title = meta.get('title') or next((line.strip('# ').strip() for line in requirement.splitlines() if line.strip('# ').strip()), run.name)
        modules = state.get('modules') or []
        accepted = sum(item.get('status') == 'accepted' for item in modules)
        control_error = ''
        try:
            control = read_control(run / 'control.json').get('action', '')
        except ControlError as exc:
            control, control_error = 'invalid', str(exc)
        if status == 'paused' and control not in {'pause', 'invalid'}:
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
        migrated_to = meta.get('_migrated_to', '')
        migrated = migrated_to or meta.get('_migration_consumed')
        if migrated:
            actions = [action for action in actions if action not in {'resume', 'checks', 'roles'}]
            resume_unavailable = ('剩余工作已迁移，请在续做任务中继续处理。' if migrated_to else
                                  '续做任务已归档，原任务的剩余预算已随任务转移。')
        if ('delete' in actions and migration.eligible(meta, state, status)
                and not meta.get('_migration_consumed')
                and not (run / 'publication.json').exists() and not (run / 'work-checkpoint.json').exists()):
            actions.append('migrate')
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
        if control_error:
            control_message = (control_message + ' ' if control_message else '') + (
                '控制请求暂不可用：' + control_error + '；请重新发送暂停、恢复或停止指令。')
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
            requirement_revision = Directions(run).revision(requirement, directions) if include_details else {}
            direction_error = ''
        except (ValueError, OSError) as exc:
            directions, direction_error = [], str(exc)
            requirement_revision = {}
        directable = {'queued', 'starting', 'running', 'paused', *RESUMABLE} - {'max_rounds'}
        can_direct = not migrated and not direction_error and status in directable
        can_queue_requirements = (state.get('workflow', meta.get('workflow')) == 'review-first'
                                  and not migrated
                                  and status in directable)
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
        removable_checks = {}
        if include_details and 'checks' in actions:
            try:
                removable_checks = _removable_checks(state)
            except ValueError:
                pass
        return safe({'id': meta['id'], 'title': title[:160], 'requirement': requirement,
                     'cwd': meta.get('cwd') or state.get('cwd', ''), 'run_dir': str(run),
                     'project_cwd':meta.get('project_cwd',''), 'parallel_project':meta.get('parallel_project',False),
                     'task_scope':meta.get('task_scope',[]), 'task_dependencies':meta.get('task_dependencies',[]),
                     'assignment_workspaces': state.get('assignment_workspaces', []) if include_details else [],
                     'assignment_deliveries': state.get('assignment_deliveries', []) if include_details else [],
                     'task_branch':meta.get('task_branch',''),
                     'managed': bool(meta.get('managed')), 'status': status, 'health': health,
                     'time_estimate': estimate_task_time(state, status, health['usage'].get('elapsed_s'))
                                      if include_details else {},
                     'migrated_from': meta.get('migrated_from', ''),
                     'migrated_to': migrated_to,
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
                     'summary': present_summary(state.get('summary') or (state.get('last_worker') or {}).get('summary', '')),
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
                     'task_type': state.get('task_type', meta.get('task_type', 'code')),
                     'function_dispatch': state.get('function_dispatch', meta.get('function_dispatch', False)),
                     'referee': state.get('referee', {'enabled': meta.get('referee', False),
                                                    'model': meta.get('referee_model', ''), 'decision': None}),
                     'function_tasks': state.get('function_tasks', {}) if include_details else {},
                     'delivery_diagnosis': delivery_diagnosis if include_details else {},
                     'workflow':state.get('workflow',meta.get('workflow','legacy')),
                     'requirements_review':state.get('requirements_review',{}) if include_details else {},
                     'requirement_revision': requirement_revision,
                     'quality_policy': state.get('quality_policy', meta.get('quality_policy', {})) if include_details else {},
                     'quality_state': state.get('quality_state', {}) if include_details else {},
                     'requirement_coverage': read_json(run / 'requirement-coverage.json') if include_details else {},
                     'field_handoff': self._field_handoff_view(run, state) if include_details else {},
                     'local_preview': self._local_preview_view(meta) if include_details else {},
                     'activity': progress,
                     'work': work,
                     'control_message': control_message, 'resume_unavailable': resume_unavailable,
                     'launch_error': meta.get('error', '') if not live else '',
                     'directions': directions, 'can_direct': can_direct,
                     'can_queue_requirements': can_queue_requirements,
                     'direction_delivery': delivery, 'direction_error': direction_error,
                     'supervision_preferences': meta.get('supervision_preferences'),
                     'actions': actions, 'checks': state.get('checks', meta.get('checks', [])),
                     'removable_checks': removable_checks})

    @staticmethod
    def summary(view: dict) -> dict:
        """Allowlisted task metadata; streams and task instructions stay in detail APIs."""
        keys = ('id', 'title', 'cwd', 'project_cwd', 'parallel_project', 'task_branch', 'status', 'provider', 'reviewer', 'created_at', 'updated_at',
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

    def task_snapshot(self, ident: str) -> dict:
        with self._lock:
            task = self.summary(self._view(self._get(ident), {}, include_details=False))
            return {'tasks': [task], 'at': timestamp()}

    def supervision_target(self, ident: str) -> dict:
        with self._lock:
            task = self._view(self._get(ident), {}, include_details=False)
            return {key: task.get(key) for key in ('id', 'cwd', 'project_cwd', 'supervision_preferences')}

    def dashboard(self) -> dict:
        with self._lock:
            data = self.task_summaries()
            tasks = data['tasks']
            counts = {'total': len(tasks), 'running': sum(t['status'] in {'running', 'starting'} for t in tasks),
                      'paused': sum(t['status'] == 'paused' for t in tasks),
                      'done': sum(t['status'] == 'done' for t in tasks),
                      'waiting': sum(t['status'] not in {'running', 'starting', 'done', 'cancelled', 'stopped'}
                                     for t in tasks)}
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

    def add_direction(self, ident: str, data: dict, *, activate: bool = True) -> dict:
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
            directions = Directions(Path(meta['run_dir']))
            text = data.get('text', '')
            if not isinstance(text, str) or len(text) > 4000 or '\0' in text:
                raise TaskError('请填写 1 到 4000 字的补充要求')
            try:
                if text.strip():
                    directions.append(text)
                elif view['status'] == 'queued' or not directions.messages():
                    raise ValueError('请填写补充要求；已有要求可留空继续发送')
            except ValueError as exc:
                raise TaskError(str(exc)) from exc
            # Saving succeeds independently of resuming. A launch/validation
            # failure must not make the person re-enter an accepted amendment.
            submission = {'status': 'saved', 'message': '补充要求已保存，任务启动后发送给 AI。'}
            if pause:
                write_json(Path(meta['run_dir']) / 'control.json', {'action': 'pause'}, durable=True)
                submission.update(status='pause_requested', message='补充要求已保存，当前轮次结束后暂停。')
            elif activate:
                try:
                    if 'continue' in view['actions']:
                        self.action(ident, 'continue')
                        submission.update(status='continuing', message='补充要求已保存，已请求继续执行。')
                    elif view['status'] in RESUMABLE:
                        if 'resume' not in view['actions']:
                            raise TaskError(view.get('resume_unavailable') or '当前检查点暂不能恢复，请核对任务状态', 409)
                        self.action(ident, 'resume')
                        submission.update(status='resuming', message='补充要求已保存，已启动恢复执行。')
                    elif view['direction_delivery'] == 'restart_required':
                        submission.update(message='补充要求已保存；当前监督器版本需停止并恢复后才能发送。')
                    elif view['status'] in {'running', 'starting'}:
                        submission.update(status='pending', message='补充要求已保存，将在下一轮 AI 调用时发送。')
                except (TaskError, OSError, ValueError) as exc:
                    submission.update(status='resume_blocked', error=redact(str(exc)),
                                      message='补充要求已保存，但自动恢复受阻；无需重复填写。')
            return {**self.detail(ident), 'direction_submission': submission}

    def queued_requirements(self, ident: str, data: dict | None = None) -> dict:
        with self._lock:
            meta = self._get(ident)
            view = self._view(meta, {}, include_details=False)
            queue = RequirementQueue(Path(meta['run_dir']))
            if data is not None:
                if not view.get('can_queue_requirements'):
                    raise TaskError('当前任务不能追加排队需求；需要尚未结束的 review-first 任务', 409)
                try:
                    if data.get('action', 'append') == 'append':
                        position = data.get('position', 'last')
                        if position == 'current':
                            self.add_direction(ident, {'text': data.get('text')}, activate=False)
                        else:
                            queue.append(data.get('text'), position)
                    elif data.get('action') == 'remove':
                        queue.remove(data.get('id'))
                    else:
                        raise ValueError('不支持的需求队列操作')
                except ValueError as exc:
                    raise TaskError(str(exc), 409) from exc
            try:
                state = queue.state()
                snapshot = queue.snapshot(state)
            except ValueError as exc:
                raise TaskError(str(exc), 409) from exc
            try:
                directions = Directions(Path(meta['run_dir'])).messages()
            except (OSError, ValueError) as exc:
                raise TaskError(str(exc), 409) from exc
            brief = state.get('requirements_review') or {}
            reviewed = list(state.get('dispatch_directions') or [])
            if not state.get('function_dispatch') and brief.get('status') == 'ready':
                revision = state.get('requirement_revision') or {}
                for item in revision.get('amendments', []):
                    reviewed.append(item['direction_id'])
                    if item['sha256'] == brief.get('reviewed_direction_sha256'):
                        break
                else:
                    reviewed = []
            return {**snapshot, 'id': ident, 'title': view['title'], 'status': view['status'],
                    'amendments': safe([{**item, 'planned': item['id'] in reviewed} for item in directions]),
                    'check_supplements': safe([{**change['target'], 'at': change['at'],
                                                'count': len(change['added'])}
                                               for change in state.get('check_changes', [])
                                               if change.get('target') and change.get('added')]),
                    'original_requirement': view['requirement'],
                    'can_append': view.get('can_queue_requirements', False),
                    'delivery': 'restart_required' if state and state.get('requirement_queue_version') != 1 else 'after_acceptance'}

    def detail(self, ident: str) -> dict:
        with self._lock:
            return self._view(self._get(ident), self.metrics.sample()['processes'])

    @staticmethod
    def _field_handoff_view(run, state):
        try:
            value = field_handoff.read(run)
            return field_handoff.public(value) if value and field_handoff.binding(state) == value['binding'] else {}
        except (OSError, ValueError, KeyError):
            return {}

    def browser_captcha_image(self, ident, nonce):
        from base64 import b64decode

        with self._lock:
            run = Path(self._get(ident)['run_dir'])
            state = Directions(run).read('state.json', 32 * 1024 * 1024)
            handoff = self._field_handoff_view(run, state)
            challenge = handoff.get('browser_inputs', {}).get(nonce, {})
            if (state.get('status') != 'action_required' or challenge.get('kind') != 'image'
                    or challenge.get('expires', 0) <= time.time()):
                raise TaskError('验证码已失效，请等待新的验证码。', 404)
            image = b64decode(challenge.get('image', ''), validate=True)
            if not image.startswith(b'\x89PNG\r\n\x1a\n'):
                raise TaskError('验证码图片格式无效。', 404)
            return image

    def _local_preview_view(self, meta) -> dict:
        if not meta.get('managed') or not debug_preview.available(Path(meta['cwd'])):
            return {'status': 'unavailable'}
        try:
            return debug_preview.status(self.data_dir / 'debug-previews' / run_key(Path(meta['run_dir'])))
        except (OSError, ValueError, KeyError):
            return {'status': 'failed', 'message': '暂时无法读取本地调试状态。'}

    def local_preview(self, ident: str, data: dict | None = None) -> dict:
        with self._lock:
            meta = self._get(ident)
            if data is None:
                return self._local_preview_view(meta)
            workspace = Path(meta['cwd'])
            if not meta.get('managed') or not debug_preview.available(workspace):
                return {'status': 'unavailable'}
            directory = self.data_dir / 'debug-previews' / run_key(Path(meta['run_dir']))
            try:
                if data.get('action') == 'start':
                    return debug_preview.start(directory, workspace, data)
                if data.get('action') == 'stop':
                    return debug_preview.stop(directory)
                raise ValueError('请选择启动或关闭本地调试。')
            except (ValueError, RuntimeError) as exc:
                raise TaskError(str(exc), 409) from exc

    def field_handoff(self, ident: str, data: dict) -> dict:
        from auto_moniter.workspace import workspace_fingerprint

        with self._lock:
            meta = self._get(ident)
            run = Path(meta['run_dir'])
            view = self._view(meta, {}, include_details=False)
            if view['status'] != 'action_required':
                raise TaskError('只有等待处理的任务可以登记或提交现场回执。', 409)
            state = Directions(run).read('state.json', 32 * 1024 * 1024)
            checkpoint = field_handoff.binding(state)
            worker = next(row for row in reversed(state['rounds'])
                          if row.get('role') == 'worker' and row.get('index') == checkpoint['round'])
            receipt = worker['watcher_result']['candidate_checks']
            if not candidate_checks.valid(receipt, run, receipt.get('context')):
                raise TaskError('原轮次的检查回执已变化，请重新核对候选。', 409)
            if (workspace_fingerprint(Path(state['cwd']), ignore_dir=run) != checkpoint['tree']
                    or [row['id'] for row in Directions(run).messages()] != checkpoint['directions']):
                raise TaskError('候选或用户要求已变化，此交接需要重新复核。', 409)
            if 'items' in data:
                independent = data.get('independent_tasks', [])
                if (not isinstance(independent, list) or len(independent) > 12
                        or not all(isinstance(child, str) for child in independent)):
                    raise TaskError('请提供已登记的独立任务编号。')
                items = data['items']
                executors = [(item['executor_task'], item.get('kind') == 'deployment') for item in items
                             if isinstance(item, dict) and item.get('executor_task')] if isinstance(items, list) else []
                targets = [*independent, *(key for key, _ in executors)]
                if (any(not isinstance(key, str) for key in targets) or len(set(targets)) != len(targets)):
                    raise TaskError('执行任务不得重复关联。')
                for child_id, for_deployment in [(key, False) for key in independent] + executors:
                    self._validate_handoff_task(meta, child_id, deployment=for_deployment)
                value = field_handoff.configure(run, state, items,
                    replace_revision=data.get('replace_revision'), independent=independent)
                return self._dispatch_handoff_tasks(meta, value)
            try:
                if data.get('action') == 'claim':
                    return field_handoff.claim(run, state, data)
                if data.get('action') == 'release':
                    return field_handoff.release(run, state, data)
                if data.get('action') in ('browser_captcha', 'retry_browser_captcha'):
                    value, item = field_handoff._select(run, state, data)
                    cases = browser_execution.cases(self.data_dir / browser_execution.CONFIG, Path(meta['cwd']))
                    case_id = item.get('case_id')
                    config = browser_execution.for_handoff(cases.get(case_id), value, case_id)
                    if (not config or not config.get('interactive_captcha') or item.get('receipt')
                            or item.get('claim') or item.get('executor_task')
                            or not browser_execution.matches_item(config, value, item)):
                        raise ValueError('浏览器会话已失效，请刷新任务。')
                    if data['action'] == 'retry_browser_captcha':
                        if read_control(run / 'control.json').get('action') in ('stop', 'pause'):
                            raise ValueError('任务已暂停或停止，请先恢复任务。')
                        field_handoff._require_dependencies(value, item)
                        result = browser_execution.poll(run, value, item, config, retry_captcha=True)
                        if result['status'] == 'failed':
                            raise ValueError(result['reason'])
                        return {'status': result['status']}
                    browser_execution.submit_captcha(run, value, item, config, data.get('nonce'), data.get('code'))
                    return {'submitted': True}
                if data.get('action') == 'bind_browser_case':
                    case_id = data.get('case_id')
                    cases = browser_execution.cases(self.data_dir / browser_execution.CONFIG, Path(meta['cwd']))
                    if not isinstance(case_id, str) or case_id not in cases:
                        raise ValueError('当前工作区尚未登记此浏览器用例。')
                    handoff = field_handoff.read(run)
                    if not browser_execution.matches_item(cases[case_id], handoff, data):
                        raise ValueError('浏览器用例与当前派单、候选或交接待办不一致。')
                    return field_handoff.public(field_handoff.bind_browser_case(run, state, data, cases[case_id]))
                current_handoff = field_handoff.read(run)
                item = next((row for row in current_handoff.get('items', []) if row['id'] == data.get('id')), {})
                if item.get('executor_task'):
                    raise TaskError('此交接由关联执行任务自动回传，以该任务的正式验收结果为准。', 409)
                value = field_handoff.submit(run, state, data)
            except ValueError as exc:
                raise TaskError(str(exc), 409) from exc
            return self._resume_field_handoff(ident, run, value)

    def _validate_handoff_task(self, parent, ident, *, deployment):
        child = self._get(ident)
        if (parent['id'] == ident or not child.get('managed')
                or workspaces_overlap(Path(parent['cwd']), Path(child['cwd']))
                or child.get('task_type', 'code') not in ({'deployment'} if deployment else {'code', 'documentation'})
                or parent['id'] in child.get('task_dependencies', [])):
            raise TaskError('关联任务须已登记、使用独立工作区，且执行类型与交接一致。', 409)
        return child

    def _dispatch_handoff_tasks(self, meta, value):
        if field_handoff.ready(value):
            return field_handoff.public(value)
        run = Path(meta['run_dir'])
        targets = dict.fromkeys(value.get('independent_tasks', []))
        deployment_pending = any(item['kind'] == 'deployment' and not item.get('receipt') for item in value['items'])
        targets.update((item['executor_task'], item) for item in value['items']
                       if item.get('executor_task') and not item.get('receipt')
                       and not (item['kind'] == 'browser' and deployment_pending))
        dispatch = dict(value.get('dispatch') or {})
        for ident, item in targets.items():
            if dispatch.get(ident, {}).get('status') == 'dispatched':
                continue
            try:
                child = self._validate_handoff_task(meta, ident, deployment=bool(item and item['kind'] == 'deployment'))
                status = self._view(child, {}, include_details=False)['status']
                if status == 'queued':
                    if item:
                        field_handoff.prepare_executor(Path(child['run_dir']), meta['id'], value, item)
                    self.action(ident, 'start')
                elif status not in {'starting', 'running', 'paused', 'done'}:
                    raise TaskError('关联执行任务未就绪，请核对其状态。', 409)
                dispatch[ident] = {'status': 'dispatched'}
            except (TaskError, OSError, ValueError) as exc:
                dispatch[ident] = {'status': 'waiting', 'error': redact(str(exc))}
        if dispatch != value.get('dispatch', {}):
            value = {**value, 'dispatch': dispatch}
            write_json(run / field_handoff.FILE, value, durable=True)
        return field_handoff.public(value)

    def _executor_field_result(self, child, item):
        from auto_moniter import deployment, requirement_context
        from auto_moniter.workspace import evidence_paths, workspace_fingerprint

        run, cwd = Path(child['run_dir']), Path(child['cwd']).resolve()
        state = Directions(run).read('state.json', 32 * 1024 * 1024)
        if state.get('status') != 'done' or state.get('tree') != workspace_fingerprint(cwd, ignore_dir=run):
            raise ValueError('执行任务的工作区已变化，不能复用原结果。')
        if item['kind'] == 'deployment':
            name = field_handoff.EXECUTOR_RESULT
            accepted = next((row for row in reversed(state.get('rounds') or [])
                             if row.get('role') == 'reviewer' and row.get('delivery', {}).get('status') == 'accepted'), {})
            fingerprint = deployment.fingerprint(cwd, run, child['requirement'])
            approved = accepted.get('watcher_result', {}).get('candidate_approval', {}).get('context', {})
            if name not in fingerprint or approved.get('deployment_evidence') != fingerprint:
                raise ValueError('部署结果未纳入执行任务的正式验收。')
            report = deployment.validate(cwd, run, child['requirement'])
            if name not in report['stages'][-1]['evidence']:
                raise ValueError('部署执行器的回执须由现场验证阶段引用。')
            path = deployment.evidence_path(cwd, run).with_name(name)
            value = requirement_context.read(path, limit=128 * 1024)
            if not isinstance(value, dict) or not isinstance(value.get('receipt'), dict):
                raise ValueError('部署执行器未提供有效的结构化回执。')
            target = report['target']
            if (value['receipt'].get('version') != target['version']
                    or item.get('expected_target', target['host']) != target['host']):
                raise ValueError('部署回执与已验收的目标版本不一致。')
            return value
        # Browser and observation executors publish their result as part of the reviewed workspace.
        path = cwd / field_handoff.EXECUTOR_RESULT
        if path.is_symlink() or not evidence_paths(cwd, [path.name], ignore_dir=run, include_git_ignored=False):
            raise ValueError('执行回执须为工作区内经过验收的普通文件。')
        return requirement_context.read(path, limit=128 * 1024)

    def _collect_field_results(self, meta, run, state, value):
        errors = {}
        failed_cases = {}
        browser_inputs = {}
        browser_items = [item for item in value['items'] if item['kind'] == 'browser']
        cases = None
        for item in value['items']:
            if item.get('receipt'):
                continue
            ident = item.get('executor_task')
            if item['kind'] == 'browser' and any(row['kind'] == 'deployment' and not row.get('receipt')
                                                for row in value['items']):
                errors[item['id']] = '等待同一候选的部署回执。'
                continue
            try:
                if not ident:
                    if item['kind'] != 'browser':
                        errors[item['id']] = '尚未关联获准的执行任务。'
                        continue
                    if item.get('claim'):
                        errors[item['id']] = '等待认领人提交现场回执。'
                        continue
                    if cases is None:
                        cases = browser_execution.cases(self.data_dir / browser_execution.CONFIG, Path(meta['cwd']))
                    case_id = item.get('case_id')
                    binding = value['binding']
                    if not case_id:
                        matching = [key for key, config in cases.items()
                                    if config.get('task_id') and (config.get('handoff_ids') or len(browser_items) == 1)
                                    and browser_execution.matches_item(config, value, item)]
                        if len(matching) == 1:
                            case_id = matching[0]
                    if not case_id:
                        errors[item['id']] = ('等待可见浏览器操作并提交回执。' if item.get('allow_manual')
                                              else '尚未关联获准的执行任务。')
                        if not item.get('allow_manual'):
                            failed_cases[item['id']] = {'case_id':'', 'reason':errors[item['id']], 'role':'reviewer'}
                        continue
                    config = browser_execution.for_handoff(cases.get(case_id), value, case_id)
                    if not config:
                        result = {'status':'failed', 'reason':'当前工作区未配置所请求的浏览器用例。'}
                    elif not browser_execution.matches_item(config, value, item):
                        result = {'status':'failed', 'reason':'浏览器用例与当前派单、候选或交接待办不一致。'}
                    elif (item.get('url', config['url']) != config['url']
                          or item.get('expected_version', config['expected_version']) != config['expected_version']):
                        result = {'status':'failed', 'reason':'浏览器用例的现场入口或版本与原待办不一致。'}
                    else:
                        if config.get('task_id') and (not item.get('case_id') or not item.get('browser_proof')):
                            value = field_handoff.bind_browser_case(run, state, {
                                'id': item['id'], 'case_id': case_id, 'binding': binding,
                                'revision': value['revision'],
                            }, config)
                            item = next(row for row in value['items'] if row['id'] == item['id'])
                        result = browser_execution.poll(run, value, item, config)
                    if result['status'] == 'ready':
                        submission = result['submission']
                        if config.get('handoff_ids'):
                            submission = {**submission, 'id': item['id']}
                        value = field_handoff.submit(run, state, submission, executor='browser:'+case_id)
                    else:
                        errors[item['id']] = result.get('reason', '浏览器验证已启动，等待执行结果。')
                        if result['status'] == 'needs_input':
                            challenge = result['challenge']
                            browser_inputs.setdefault(challenge['nonce'], {**challenge, 'id': item['id']})
                            errors[item['id']] = '浏览器正在等待验证码。'
                        if result['status'] == 'failed':
                            failed_cases[item['id']] = {'case_id':case_id, 'reason':errors[item['id']],
                                                       'role':(config or {}).get('fallback', 'reviewer')}
                            if config and config.get('interactive_captcha') and browser_execution.captcha_retryable(result):
                                failed_cases[item['id']]['retry_captcha'] = True
                    continue
                child = self._validate_handoff_task(meta, ident, deployment=item['kind'] == 'deployment')
                child_view = self._view(child, {}, include_details=False)
                if child_view['status'] != 'done':
                    errors[item['id']] = '等待关联执行任务完成验收。' if child_view['status'] in {
                        'queued', 'starting', 'running', 'paused'} else '关联执行任务未通过验收，请查看该任务的停止原因。'
                    continue
                result = self._executor_field_result(child, item)
                if not isinstance(result, dict) or result.get('id') != item['id']:
                    raise ValueError('执行器回执未绑定当前交接项。')
                value = field_handoff.submit(run, state, result, executor=ident)
            except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
                errors[item['id']] = redact(str(exc))
                if not ident and item['kind'] == 'browser' and not item.get('allow_manual'):
                    failed_cases[item['id']] = {'case_id':item.get('case_id', ''),
                                               'reason':errors[item['id']], 'role':'reviewer'}
        if (errors != value.get('executor_status', {}) or failed_cases != value.get('failed_cases', {})
                or browser_inputs != value.get('browser_inputs', {})):
            value = {**value, 'executor_status': errors, 'failed_cases': failed_cases,
                     'browser_inputs': browser_inputs}
            write_json(run / field_handoff.FILE, value, durable=True)
        return value

    def _route_browser_failure(self, meta, run, value, *, referee_enabled=False):
        missing = [item for item in value['items'] if not item.get('receipt')]
        failures = {item['id']: value['failed_cases'][item['id']] for item in missing
                    if item['id'] in (value.get('failed_cases') or {})
                    and not value['failed_cases'][item['id']].get('retry_captcha')}
        if not failures:
            return
        key = candidate_checks.digest([value['binding'], sorted(
            (f['case_id'], f['role'], f['reason']) for f in failures.values())])
        configured_role = 'referee' if any(f['role'] == 'referee' for f in failures.values()) else 'reviewer'
        role = 'referee' if referee_enabled else configured_role
        if role != configured_role:
            # Let the Referee handle an incident already sent to the Reviewer
            # under the previous routing rule, once per checkpoint.
            key = candidate_checks.digest([key, role])
        history = value.get('fallback_history', [])
        previous = value.get('fallback') or {}
        pending = previous.get('key') == key and previous.get('status') == 'pending'
        if not pending and key in history:
            return
        waiting = {item['id']: value.get('executor_status', {}).get(item['id'], '等待回执。')
                   for item in missing if item['id'] not in failures}
        updated = {**value, 'fallback': {'revision':value['revision'], 'role':role, 'failures':failures,
                                         'waiting': waiting,
                                         'key':key, 'status':'pending'},
                   'fallback_history': history if pending else [*history, key]}
        write_json(run / field_handoff.FILE, updated, durable=True)
        try:
            self.action(meta['id'], 'resume')
        except (TaskError, OSError, ValueError) as exc:
            write_json(run / field_handoff.FILE, {**value, 'fallback_history': updated['fallback_history'],
                         'executor_status': {**value.get('executor_status', {}),
                             'fallback': redact(str(exc))}}, durable=True)
        else:
            current = field_handoff.read(run)
            if current.get('revision') == value['revision'] and current.get('fallback') == updated['fallback']:
                current['fallback']['status'] = 'launched'
                write_json(run / field_handoff.FILE, current, durable=True)

    def _resume_field_handoff(self, ident, run, value):
        if not field_handoff.ready(value) or value.get('resume', {}).get('status') != 'pending':
            return field_handoff.public(value)
        try:
            self.action(ident, 'resume')
        except (TaskError, OSError, ValueError) as exc:
            failure = redact(str(exc))
            updated = field_handoff.update_resume(run, value, 'pending',
                                                 agent_calls=value['resume']['agent_calls'], error=failure)
            return {**field_handoff.public(updated), 'resume_error': failure}
        # The child may already have advanced or reopened the handoff.
        current = field_handoff.read(run)
        if current.get('revision') == value['revision'] and current.get('resume', {}).get('status') == 'pending':
            current = field_handoff.update_resume(run, current, 'launched',
                                                 agent_calls=current['resume']['agent_calls'])
        return {**field_handoff.public(current), 'resuming': True}

    @staticmethod
    def _can_resume_field_archive(state, value, receipt):
        return (state.get('full_function_plan') and state.get('referee', {}).get('enabled')
                and field_handoff.archive_item(value) and receipt.get('passed') and receipt.get('checks'))

    def _resume_field_archive(self, meta, run, value, *, retry_failed):
        for entry in referee.Journal(run, archive=True).read()['decisions']:
            if (entry.get('field_binding', entry.get('archive', {}).get('binding')) == value['binding']
                    and (entry['status'] != 'captured' or entry['archive']['revision'] != value['revision'])):
                return
        key = candidate_checks.digest(value['binding'])
        previous = value.get('archive_resume') or {}
        if previous.get('key') == key and (previous.get('status') == 'launched'
                                            or previous.get('error') and not retry_failed):
            return
        pending = {'key': key, 'status': 'pending'}
        write_json(run / field_handoff.FILE, {**value, 'archive_resume': pending}, durable=True)
        try:
            self.action(meta['id'], 'resume')
        except (TaskError, OSError, ValueError) as exc:
            finished = {**pending, 'error': redact(str(exc))}
        else:
            finished = {**pending, 'status': 'launched'}
        current = field_handoff.read(run)
        if current.get('revision') == value['revision'] and current.get('archive_resume') == pending:
            write_json(run / field_handoff.FILE, {**current, 'archive_resume': finished}, durable=True)

    def recover_field_handoffs(self, *, retry_failed=True):
        """Collect receipts and resume eligible checkpoints even without an open task page."""
        from auto_moniter.workspace import workspace_fingerprint

        with self._lock:
            for meta in self._catalog().values():
                if not meta.get('managed'):
                    continue
                run = Path(meta['run_dir'])
                try:
                    value = field_handoff.read(run)
                    if not value:
                        continue
                    view = self._view(meta, {}, include_details=False)
                    if view['status'] in {'running', 'starting', 'paused'}:
                        continue
                    if view['status'] not in {'action_required', 'interrupted'}:
                        continue
                    if read_control(run / 'control.json').get('action') in ('stop', 'pause'):
                        continue
                    state = Directions(run).read('state.json', 32 * 1024 * 1024)
                    checkpoint = value['binding']
                    worker = next((row for row in reversed(state.get('rounds') or [])
                                   if row.get('role') == 'worker' and row.get('index') == checkpoint['round']), {})
                    receipt = worker.get('watcher_result', {}).get('candidate_checks') or {}
                    if (field_handoff.binding(state) != checkpoint
                            or [row['id'] for row in Directions(run).messages()] != checkpoint['directions']
                            or workspace_fingerprint(Path(state['cwd']), ignore_dir=run) != checkpoint['tree']
                            or not candidate_checks.valid(receipt, run, receipt.get('context'))):
                        if field_handoff.ready(value):
                            field_handoff.update_resume(run, value, 'stale', error='候选、验收命令或用户要求已变化。')
                        continue
                    if 'resume' in view['actions'] and self._can_resume_field_archive(state, value, receipt):
                        self._resume_field_archive(meta, run, value, retry_failed=retry_failed)
                        continue
                    if not field_handoff.ready(value):
                        value = self._collect_field_results(meta, run, state, value)
                        self._dispatch_handoff_tasks(meta, value)
                        if not field_handoff.ready(value) and 'resume' in view['actions']:
                            self._route_browser_failure(meta, run, value,
                                referee_enabled=state.get('referee', {}).get('enabled') is True)
                    if field_handoff.ready(value) and 'resume' in view['actions']:
                        request = value.get('resume') or {}
                        if request.get('error') and not retry_failed:
                            continue
                        if (request.get('status') == 'launched' and view['status'] == 'interrupted'
                                and state.get('status') == 'running'
                                and request.get('agent_calls') == (state.get('usage') or {}).get('agent_calls', 0)):
                            value = field_handoff.update_resume(run, value, 'pending',
                                                               agent_calls=request['agent_calls'])
                        self._resume_field_handoff(meta['id'], run, value)
                except (OSError, ValueError, KeyError, TypeError, ControlError) as exc:
                    logger.warning('任务 %s 的现场续审检查失败：%s', meta['id'], redact(str(exc)))

    def dispatch_plan(self, ident: str, page: int = 1, page_size: int = 10, status: str = 'all') -> dict:
        from console.dispatch import page as dispatch_page
        with self._lock:
            meta = self._get(ident)
            try:
                state = RequirementQueue(Path(meta['run_dir'])).state()
                return {**dispatch_page(state, page, page_size, status), 'id':ident, 'at':timestamp()}
            except (ValueError, KeyError, TypeError) as exc:
                raise TaskError(str(exc), 400 if isinstance(exc, ValueError) else 409) from exc

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

    def create(self, data: dict, *, supervision_preferences=None, migration_snapshot=None) -> dict:
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
        from auto_moniter.task_types import TASK_TYPES, NON_FUNCTION_TASK_TYPES
        plan_type = data['acceptance_plan'].get('task_type', 'code') if isinstance(data.get('acceptance_plan'), dict) else 'code'
        task_type = data.get('task_type', plan_type)
        if not isinstance(task_type, str) or task_type not in TASK_TYPES:
            raise TaskError('请选择有效任务类型')
        deployment = task_type == 'deployment'
        non_function = task_type in NON_FUNCTION_TASK_TYPES
        if deployment and workflow != 'review-first':
            raise TaskError('部署任务需要先审核需求并进行独立验收')
        parallel = data.get('parallel_project',False)
        if type(parallel) is not bool:
            raise TaskError('parallel_project 必须是布尔值')
        if parallel and workflow != 'review-first':
            raise TaskError('同工程并行任务需要先审核需求的流程')
        if deployment and parallel:
            raise TaskError('部署任务不支持同工程并行开发')
        function_dispatch = data.get('function_dispatch',
            workflow == 'review-first' and data.get('verification_mode') not in ('review_only', 'planning'))
        if type(function_dispatch) is not bool or function_dispatch and workflow != 'review-first':
            raise TaskError('逐函数下发需要启用先审核需求的流程')
        if non_function:
            function_dispatch = False
        full_function_plan = data.get('full_function_plan', function_dispatch)
        if non_function:
            full_function_plan = False
        if type(full_function_plan) is not bool or full_function_plan and not function_dispatch:
            raise TaskError('完整函数计划需要开启逐函数派单')
        use_referee = data.get('referee', False)
        referee_model = data.get('referee_model', '')
        if (type(use_referee) is not bool or not isinstance(referee_model, str)
                or len(referee_model) > 2000 or '\0' in referee_model):
            raise TaskError('裁判设置须为开关和有效模型名称')
        if provider not in PROVIDERS or reviewer not in PROVIDERS:
            raise TaskError('请选择支持的执行器和审查器')
        guided = None
        if data.get('acceptance_plan') is not None:
            try:
                guided = select_acceptance(cwd, data['acceptance_plan'])
            except (ValueError,OSError) as exc:
                raise TaskError(str(exc)) from exc
            if guided['task_type'] != task_type:
                raise TaskError('任务类型与验收方案不一致，请重新扫描')
        checks = data.get('checks', [])
        if not isinstance(checks, list) or len(checks) > 20 or any(not isinstance(c, str) or not c.strip() or len(c) > 2000 or '\x00' in c for c in checks):
            raise TaskError('验收命令格式不正确，每行填写一条命令')
        try:
            validate_commands([*checks, *_module_check_commands(data.get('modules'))])
        except ValueError as exc:
            raise TaskError(str(exc)) from exc
        # Distinguish explicit mode from legacy clients that omit verification_mode.
        mode_explicit = 'verification_mode' in data
        verification_mode = data.get('verification_mode',
            'executable' if function_dispatch or non_function else 'review_only')
        if verification_mode not in ('executable', 'review_only', 'planning'):
            raise TaskError('验收模式无效')
        if deployment and verification_mode != 'executable':
            raise TaskError('部署任务需要执行部署并验证远程结果')
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
        if (mode_explicit and verification_mode == 'executable' and not function_dispatch
                and not non_function
                and not checks and not module_check_commands and not (guided and guided['selected'])):
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
            from auto_moniter.quality import validate_check_inputs
            try:
                validate_check_inputs(execution.get('quality_policy', {}), checks)
            except ValueError as exc:
                raise TaskError(str(exc)) from exc
            if scopes:
                if not function_dispatch:
                    raise TaskError('集成任务需要开启逐函数派单与执行验收')
                from auto_moniter.function_gate import validate_approvals
                try:
                    validate_approvals(scopes, cwd, {m['id']: m for m in execution['modules']}, checks)
                except (ValueError, OSError) as exc:
                    raise TaskError(str(exc)) from exc
            meta = {'id': ident, 'managed': True, 'title': title, 'requirement': requirement,
                    'cwd': str(cwd), 'provider': provider, 'reviewer': reviewer, 'workflow':workflow, 'task_type':task_type,
                    'function_dispatch': function_dispatch,
                    'full_function_plan': full_function_plan,
                    'referee': use_referee, 'referee_model': referee_model,
                    **models, **backends,
                    'checks': [c.strip() for c in checks], 'verification_mode': verification_mode, **numbers, **execution,
                    'run_dir': str(self.runs_dir / ident), 'created_at': timestamp(), 'status': 'queued'}
            if supervision_preferences is not None:
                meta['supervision_preferences'] = supervision_preferences
            if parallel:
                if verification_mode == 'planning':
                    raise TaskError('并行开发请使用实现与验收模式；只制定验收方案不创建并行工作副本')
                try:
                    from auto_moniter.project_workspace import create as create_workspace
                    def validate_workspace(workspace):
                        nonlocal guided
                        meta.update(project_cwd=str(cwd),cwd=workspace['cwd'],parallel_project=True,
                                    task_scope=workspace['scope'],task_dependencies=workspace['dependencies'],task_branch=workspace['branch'])
                        if guided:
                            guided = select_acceptance(meta['cwd'],guided)
                            meta['acceptance_plan'] = guided
                        if auto_start:
                            self._check_start(meta)
                    workspace = create_workspace(cwd,run,ident,title,data.get('task_scope',[]),
                                                 data.get('task_dependencies',[]),[*checks,*module_check_commands],
                                                 validate=validate_workspace)
                except (OSError,ValueError,LockError,subprocess.SubprocessError) as exc:
                    raise TaskError('无法创建并行任务：'+str(exc),409) from exc
            from console.acceptance import coverage_warning
            meta['acceptance_warning'] = coverage_warning([*checks, *module_check_commands])
            if deployment:
                meta['acceptance_warning'] = '部署成功须有实际远程构建、产物校验和目标服务运行证据；本地测试通过不代表部署成功。'
            elif task_type == 'documentation':
                meta['acceptance_warning'] = '文档交付由独立 reviewer 核对内容、引用和覆盖范围；不代表产品运行验证或合规认证通过。'
            if guided:
                meta['acceptance_plan'] = guided
                meta['acceptance'] = ('验收方案须说明如何验证以下目标，不要求在本任务实现或证明原功能已通过：\n' if verification_mode=='planning' else '') + guided['criteria']
            if migration_snapshot is not None:
                context, seconds, directions, queue, acceptance = migration_snapshot
                meta.update(migrated_from=context['source_task_id'], max_runtime_seconds=seconds, acceptance=acceptance)
                run.mkdir(mode=0o700)
                write_json(run / 'migration-context.json', context, durable=True)
                if directions:
                    write_json(run / 'directions.json', {'version': 1, 'messages': directions}, durable=True)
                if queue:
                    write_json(run / 'requirement-queue.json', {'version': 1, 'items': queue}, durable=True)
            if auto_start and not parallel:
                self._check_start(meta)
            self._save(meta)
            if auto_start:
                self._launch(meta)
            return self.detail(ident)

    def _check_start(self, meta: dict) -> None:
        if meta['id'] in self._children or self._launcher_alive(meta):
            raise TaskError('任务进程仍在运行或等待回收，请稍后重试', 409)
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
        if state.get('status') == 'done' and delivery_recovery_warning(run):
            raise TaskError('仍有未完成的进程或提交恢复记录，尚不能确认交付；请先核对恢复记录并保留现场', 409)
        if state.get('status') not in {'running', *RESUMABLE} - {'interrupted'}:
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
            arguments = resume_arguments(manifest, run, state, **options)
            if state.get('function_dispatch') and state.get('function_gate') and meta.get('full_function_plan', True):
                arguments.append('--full-function-plan')
            return arguments
        except (ValueError, ProviderError, TypeError, KeyError, OSError) as exc:
            raise TaskError('无法恢复：' + str(exc), 409) from exc

    def _new_args(self, meta: dict) -> list[str]:
        argv = ['run', '--cwd', meta['cwd'], '--run-dir', meta['run_dir'],
                '--provider', meta['provider'], '--reviewer', meta['reviewer'],
                '--git-history', 'off' if meta.get('task_type') == 'deployment' else 'required',
                '--review-every', str(meta.get('review_every', 5)),
                '--max-runtime', str(meta.get('max_runtime_seconds', meta['max_runtime_minutes'] * 60)),
                '--max-agent-calls', str(meta['max_agent_calls']), '--max-rounds', str(meta['max_rounds']), '--guard']
        argv.append('--task-type=' + meta.get('task_type', 'code'))
        for key, flag in [('model', '--model'), ('reviewer_model', '--reviewer-model'), ('paseo_backend', '--paseo-backend'), ('reviewer_paseo_backend', '--reviewer-paseo-backend')]:
            if meta.get(key):
                argv.append(flag + '=' + meta[key])
        for check in meta['checks']:
            argv.append('--check=' + check)
        argv.append('--workflow='+meta.get('workflow','legacy'))
        argv.append('--function-dispatch' if meta.get('function_dispatch') else '--no-function-dispatch')
        if meta.get('full_function_plan'):
            argv.append('--full-function-plan')
        argv.append('--referee' if meta.get('referee', False) else '--no-referee')
        if meta.get('referee_model'):
            argv.append('--referee-model=' + meta['referee_model'])
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
        saved_round = meta.pop('review_saved_round', None)
        if resume and saved_round is not None:
            arguments.append(f'--review-saved-round={saved_round}')
        argv = [sys.executable, '-m', 'auto_moniter', *arguments]
        env = os.environ.copy()
        env['AUTO_MONITER_AGENT_SETTINGS']=str(self.agents.path)
        env[browser_execution.ENV] = str(self.data_dir / browser_execution.CONFIG)
        package_root = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = package_root + os.pathsep + env.get('PYTHONPATH', '')
        env['PYTHONUNBUFFERED'] = '1'
        previous_control = read_json(run / 'control.json') if resume else {}
        child = None
        try:
            if resume:
                (run / 'control.json').unlink(missing_ok=True)
            else:
                if meta.get('acceptance_plan'):
                    write_json(run/'acceptance-plan.json', meta['acceptance_plan'], durable=True)
                if meta.get('modules'):
                    write_json(run / 'modules.json', meta['modules'], durable=True)
                if meta.get('quality_policy'):
                    write_json(run / 'quality-policy.json', meta['quality_policy'], durable=True)
                write_json(run / 'launch.json', {'argv': arguments}, durable=True)
            with (run / 'console.log').open('ab') as output:
                child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                         cwd=package_root, env=env, start_new_session=True)
                # Ownership must survive identity lookup or bookkeeping failures.
                self._children[meta['id']] = child
            # communicate() runs off the request thread and reaps the detached CLI on exit.
            meta.update(status='starting', started_at=timestamp(), launcher=process_identity(child.pid))
            meta.pop('error', None)
            meta.pop('exit_summary', None)
            self._save(meta)
            threading.Thread(target=self._wait, args=(meta['id'], child, meta['requirement']), daemon=True).start()
        except Exception as exc:
            if child is None and not isinstance(exc, OSError):
                raise
            errors = []
            if child is not None:
                try:
                    abort_start(child)
                except Exception as cleanup_error:
                    errors.append('进程回收失败：' + str(cleanup_error))
                else:
                    if self._children.get(meta['id']) is child:
                        self._children.pop(meta['id'], None)
                    meta.pop('launcher', None)
            if previous_control:
                try:
                    write_json(run / 'control.json', previous_control, durable=True)
                except OSError as control_error:
                    errors.append('原控制请求未能恢复：' + str(control_error))
            message = str(exc) + (('；' + '；'.join(errors)) if errors else '')
            meta.update(status='failed', started_at=timestamp(), error=message)
            try:
                self._save(meta)
            except OSError as save_error:
                message += '；失败记录未能保存：' + str(save_error)
            raise TaskError(f'启动失败：{message}', 500) from exc

    def _wait(self, ident: str, child: subprocess.Popen, requirement: str) -> None:
        error = ''
        cleanup_confirmed = True
        try:
            child.communicate(requirement.encode('utf-8'))
        except Exception as exc:
            error = '任务进程通信或等待失败：' + str(exc)
            try:
                abort_start(child)
            except Exception as cleanup_error:
                cleanup_confirmed = False
                error += '；进程回收失败：' + str(cleanup_error)
        if error:
            logger.warning('任务 %s：%s', ident, redact(error))
        with self._lock:
            if self._children.get(ident) is child:
                reaped = cleanup_confirmed and child.returncode is not None
                try:
                    meta = read_json(self.tasks_dir / (ident + '.json'))
                    if meta:
                        meta['exit_code'] = child.returncode
                        if error:
                            meta.update(status='failed', error=error)
                        if reaped:
                            meta.pop('launcher', None)
                        result = console_result(Path(meta['run_dir']))
                        if result:
                            meta['exit_summary'] = result['summary']
                        self._save(meta)
                except OSError as save_error:
                    logger.warning('任务 %s 的进程退出记录保存失败：%s', ident, redact(str(save_error)))
                finally:
                    if reaped:
                        self._children.pop(ident, None)

    def action(self, ident: str, action: str, data: dict | None = None) -> dict:
        with self._lock:
            meta = self._get(ident)
            view = self._view(meta, {})
            if action not in view['actions']:
                raise TaskError('当前状态不支持此操作，请刷新后重试', 409)
            run = Path(meta['run_dir'])
            if action == 'migrate':
                if meta.get('_migrated_to'):
                    return self.detail(meta['_migrated_to'])
                try:
                    with inactive_run(run), Directions(run).lock():
                        directions = Directions(run)
                        state = directions.read('state.json', 32 * 1024 * 1024)
                        status = state.get('status')
                        if status == 'running' and confirms_follow_up_wait(run, state, read_json(run / 'report.json')):
                            status = 'action_required'
                        if not migration.eligible(meta, state, status):
                            raise ValueError('原任务的状态已变化，请刷新后重试')
                        requirement = migration.original_requirement(run, meta)
                        payload, context, seconds, queue = migration.build(meta, state, run, requirement)
                        acceptance = state.get('acceptance', meta.get('acceptance', ''))
                        return self.create(payload, migration_snapshot=(
                            context, seconds, directions.messages(), queue, acceptance))
                except TaskError:
                    raise
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise TaskError(redact(str(exc)), 409) from exc
            if action == 'checks':
                additions = (data or {}).get('additional_checks', [])
                removals = (data or {}).get('remove_checks', [])
                if (not isinstance(additions, list) or len(additions) > 20
                        or any(not isinstance(c, str) or not c.strip() or len(c) > 2000 or '\0' in c for c in additions)):
                    raise TaskError('请填写要追加的验收命令，每条最多 2000 字')
                if (not isinstance(removals, list) or len(removals) > 20
                        or any(not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token) for token in removals)
                        or not (additions or removals)):
                    raise TaskError('请选择要撤回的验收命令或填写追加命令')
                try:
                    validate_commands(additions)
                except ValueError as exc:
                    raise TaskError(str(exc)) from exc
                try:
                    with inactive_run(run):
                        state = read_json(run / 'state.json')
                        arguments = self._resume_args(meta, state)
                        old = list(state.get('checks') or [])
                        removable = _removable_checks(state)
                        if any(token not in removable for token in removals):
                            raise ValueError('只可撤回未被派单引用且不影响已引用编号的追加检查，请刷新后重试')
                        indices = sorted({removable[token] for token in removals})
                        removed = [{'index': index, 'command': old[index]} for index in indices]
                        combined = [command for index, command in enumerate(old) if index not in indices]
                        retained = len(combined)
                        for command in additions:
                            if command.strip() not in combined:
                                combined.append(command.strip())
                        if len(combined) > 20:
                            raise ValueError('最多保留 20 条验收命令')
                        if combined == old:
                            return self.detail(ident)
                        original = (run / 'state.json').read_bytes()
                        change = {'at': timestamp(), 'added': combined[retained:]}
                        if change['added'] and state.get('tree'):
                            current = (state.get('function_tasks') or {}).get('current') or {}
                            change['target'] = {'task_id': current.get('id', ''),
                                'criterion_ids': current.get('criterion_ids', []),
                                'tree': state['tree'], 'cycle': len(state.get('requirement_cycles', [])) + 1}
                        if removed:
                            change['removed'] = removed
                        changes = [*state.get('check_changes', []), change]
                        configurations(combined, changes)
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
                    if change['added']:
                        record(run, 'acceptance_checks_added', added=change['added'],
                               **(change.get('target') or {}))
                    if removed:
                        record(run, 'acceptance_checks_removed', removed=[item['command'] for item in removed])
                except (OSError, ValueError) as exc:
                    raise TaskError('保存验收检查失败：' + str(exc), 409) from exc
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
                        debug_preview.remove(self.data_dir / 'debug-previews' / run_key(run))
                        cleanup = purge_run_logs(directory)
                        marker = self.deleted_dir / (run_key(run) + '.json')
                        write_json(marker, {'id': ident, 'run_dir': str(run.resolve()),
                                          'deleted_at': timestamp(), 'logs_cleaned': True, **cleanup}, durable=True)
                        lifecycle = run / 'project-lifecycle.json'
                        previous_lifecycle = read_json(lifecycle) if meta.get('parallel_project') else None
                        try:
                            if previous_lifecycle is not None:
                                write_json(lifecycle,{'status':'deleted'},durable=True)
                            # A started follow-up has already taken ownership of the source budget.
                            if not (meta.get('migrated_from') and meta.get('started_at')):
                                (self.tasks_dir / (ident + '.json')).unlink(missing_ok=True)
                        except OSError:
                            if previous_lifecycle is not None:
                                write_json(lifecycle,previous_lifecycle,durable=True)
                            marker.unlink(missing_ok=True)
                            raise
                except (ValueError, RuntimeError) as exc:
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
                    saved_state = read_json(run / 'state.json')
                    handoff = self._field_handoff_view(run, saved_state)
                    archive_resume = handoff.get('archive_resume') or {}
                    automatic_archive = (archive_resume.get('status') == 'pending'
                                         and archive_resume.get('key') == candidate_checks.digest(handoff.get('binding'))
                                         and saved_state.get('full_function_plan')
                                         and saved_state.get('referee', {}).get('enabled')
                                         and field_handoff.archive_item(handoff))
                    if (handoff and not field_handoff.ready(handoff) and not handoff.get('fallback')
                            and not automatic_archive
                            and [row['id'] for row in Directions(run).messages()] == handoff['binding']['directions']):
                        raise TaskError('现场回执尚未齐备，请在任务详情中补交。', 409)
                    usage = saved_state.get('usage', {})
                    additions = data or {}
                    if 'review_saved_round' in additions:
                        saved_round = additions['review_saved_round']
                        if type(saved_round) is not int or saved_round < 1:
                            raise TaskError('补审轮次须为正整数。')
                        meta = {**meta, 'review_saved_round': saved_round}
                    if 'referee' in additions:
                        if type(additions['referee']) is not bool:
                            raise TaskError('裁判开关必须为布尔值')
                        arguments.append('--referee' if additions['referee'] else '--no-referee')
                    if 'referee_model' in additions:
                        value = additions['referee_model']
                        if not isinstance(value, str) or len(value) > 2000 or '\0' in value:
                            raise TaskError('裁判模型名称无效')
                        arguments.append('--referee-model=' + value)
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
                    additional_rounds = additions.get('additional_worker_rounds', 0)
                    if type(additional_rounds) is not int or not 0 <= additional_rounds <= 100000:
                        raise TaskError('追加 Worker 执行轮次必须为 0 至 100000 的整数')
                    next_round = saved_state.get('next_round') or view['health'].get('rounds', 0) + 1
                    if additional_rounds and (parsed.max_rounds or view['status'] == 'max_rounds'):
                        total_rounds = max(parsed.max_rounds, next_round - 1) + additional_rounds
                        arguments.append('--max-rounds=' + str(total_rounds))
                    elif view['status'] == 'max_rounds' and parsed.max_rounds < next_round:
                        raise TaskError('已达到 Worker 执行轮次上限，请明确追加轮次后恢复', 409)
                    from auto_moniter.project_workspace import assignment_workspaces
                    from auto_moniter.assignment_delivery import links
                    from auto_moniter.check_environment import source_aliases
                    for key, flag, validate in (
                            ('assignment_workspaces', '--assignment-workspace=', assignment_workspaces),
                            ('assignment_deliveries', '--assignment-delivery=', links),
                            ('check_source_aliases', '--check-source-alias=',
                             lambda values: source_aliases(values, saved_state.get('checks', [])))):
                        if key not in additions:
                            continue
                        bindings = additions[key]
                        try:
                            validate(bindings)
                        except (OSError, ValueError) as exc:
                            raise TaskError(str(exc)) from exc
                        arguments = [arg for arg in arguments if not arg.startswith(flag)]
                        arguments.extend(flag + binding for binding in (bindings or ['']))
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
            if not PUBLIC_LOG_NAME.fullmatch(path.name):
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
