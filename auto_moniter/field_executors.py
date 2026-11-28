"""Resolve operator-approved field executors and serialize shared resources."""

import os
from pathlib import Path
import re
import time

from auto_moniter import field_handoff
from auto_moniter.directions import Directions
from auto_moniter.health import _lock_held, delivery_recovery_warning
from auto_moniter.storage import write_json

CONFIG = 'field-executors.json'
ENV = 'AUTO_MONITOR_FIELD_EXECUTOR_DIRECTORY'
LIVE = {'starting', 'running', 'paused'}
FAILED = {'failed', 'stopped', 'interrupted', 'action_required', 'recovery_failed',
          'stalled', 'budget_exceeded', 'max_rounds', 'blocked', 'cancelled'}


def _entries(directory):
    value = Directions(directory).read(CONFIG, 128 * 1024)
    entries = value.get('executors', [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError('执行器目录最多登记 100 项。')
    for row in entries:
        if (not isinstance(row, dict) or set(row) != {'task_id', 'workspace', 'kind', 'capability', 'resource'}
                or not isinstance(row.get('task_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', row['task_id'])
                or not isinstance(row.get('workspace'), str) or not Path(row['workspace']).is_absolute()
                or not isinstance(row.get('kind'), str)
                or row.get('kind') not in field_handoff.FIELDS
                or not all(field_handoff.valid_capability(row.get(key)) for key in ('capability', 'resource'))):
            raise ValueError('执行器登记须指定工作区、任务编号、回执类型、能力和独占资源。')
    return entries


def capability_prompt(cwd):
    directory = os.environ.get(ENV)
    if not directory:
        return ''
    choices = sorted({(row['kind'], row['capability']) for row in _entries(Path(directory))
                      if Path(row['workspace']).resolve() == cwd})
    if not choices:
        return ''
    return ('\n本工作区已登记的现场执行能力，可在相符的交接项填写 capability：'
            + '、'.join(f'{kind}/{capability}' for kind, capability in choices) + '。')


def resource_busy(store, resource, *, except_id):
    for child in store._catalog().values():
        if not child.get('managed') or child['id'] == except_id:
            continue
        path = Path(child['run_dir'])
        request = Directions(path).read(field_handoff.EXECUTOR_REQUEST)
        if (request.get('item') or {}).get('executor_resource') != resource:
            continue
        if store._view(child, {}, include_details=False)['status'] in LIVE or not _released(path):
            return True
    return False


def _released(run):
    if (delivery_recovery_warning(run)
            or any(_lock_held(run / name, missing=False) is not False for name in ('guardian.lock', 'supervisor.lock'))):
        return False
    state = Directions(run).read('state.json', 32 * 1024 * 1024)
    if state.get('status') in {'running', 'recovery_failed'}:
        providers = state.get('provider_config') or {}
        return not any((providers.get(role) or {}).get('name') == 'paseo'
                       and any(state.get(key) for key in keys) for role, keys in (
                           ('worker', ('session_id',)), ('reviewer', ('reviewer_session_id', 'active_reviewer_session_id'))))
    return True


def request_current(store, task_id, request):
    parent = store._get(request.get('parent_task', ''))
    run = Path(parent['run_dir'])
    for filename, handoff in field_handoff.saved_handoffs(run):
        if request.get('binding') != handoff.get('binding') or request.get('revision') != handoff.get('revision'):
            continue
        if filename != field_handoff.FILE and not field_handoff.deferred_state(
                run, Directions(run).read('state.json', 32 * 1024 * 1024), handoff):
            continue
        if any(item['id'] == (request.get('item') or {}).get('id') and item.get('executor_task') == task_id
               and not item.get('receipt') for item in handoff.get('items', [])):
            return True
    return False


def _replacement_failure(store, parent, value, item):
    ident = item.get('executor_task')
    failure = value.get('failed_cases', {}).get(item['id'], {})
    if not ident or failure.get('executor_task') != ident:
        return None
    try:
        child = store._validate_handoff_task(parent, ident, deployment=item['kind'] == 'deployment')
    except ValueError as exc:
        if hasattr(exc, 'status'):
            return None
        raise
    run = Path(child['run_dir'])
    status = store._view(child, {}, include_details=False)['status']
    launch_failed = value.get('dispatch', {}).get(ident, {}).get('status') == 'failed'
    if (status not in FAILED | {'done'} and not (status == 'queued' and launch_failed)) or not _released(run):
        return None
    request = Directions(run).read(field_handoff.EXECUTOR_REQUEST)
    if (request.get('parent_task') == parent['id'] and request.get('revision') == value.get('revision')
            and request.get('binding') == value.get('binding') and (request.get('item') or {}).get('id') == item['id']):
        return {**failure, 'status': status, 'run_dir': str(run), 'binding': request['binding'],
                'revision': request['revision'], 'resource': item.get('executor_resource', '')}
    return None


def dispatch_failure(exc, previous):
    from auto_moniter.redaction import redact

    if not (isinstance(exc, OSError) or getattr(exc, 'retryable', False) or getattr(exc, 'status', None) == 503):
        return {'status': 'failed', 'error': redact(str(exc))}
    attempts = previous.get('attempts', 0) + 1
    return {'status': 'waiting', 'error': redact(str(exc)), 'attempts': attempts,
            'retry_at': time.time() + min(15 * 2 ** min(attempts - 1, 5), 300)}


def collect_failure(value, ident, view):
    dispatch = value.get('dispatch', {}).get(ident, {})
    if view['status'] == 'queued' and dispatch.get('status') in {'failed', 'waiting'}:
        return dispatch.get('error', ''), dispatch['status'] == 'waiting'
    if view['status'] in LIVE | {'queued'}:
        return '等待关联执行任务完成验收。', True
    return view.get('summary') or '关联执行任务未通过验收，请查看该任务的停止原因。', False


def bind(store, parent, run, value, *, filename=field_handoff.FILE):
    requested = []
    for item in value['items']:
        if (not item.get('capability') or any(item.get(key) for key in ('receipt', 'claim', 'case_id'))
                or field_handoff.dependency_error(value, item)):
            continue
        failed = _replacement_failure(store, parent, value, item)
        if failed or not item.get('executor_task'):
            requested.append((item, failed))
    if not requested:
        return {}
    entries = _entries(store.data_dir)
    occupied = {item.get('executor_task') for item in value['items']} | set(value.get('independent_tasks', []))
    waiting = {}
    for item, failed in requested:
        tried = {attempt['executor_task'] for attempt in item.get('executor_failures', [])}
        matching = [row for row in entries if row['kind'] == item['kind']
                    and row['capability'] == item['capability'] and row['task_id'] not in occupied | tried
                    and Path(row['workspace']).resolve() == Path(parent['cwd']).resolve()]
        for entry in matching:
            try:
                child = store._validate_handoff_task(parent, entry['task_id'], deployment=item['kind'] == 'deployment')
            except ValueError as exc:
                if hasattr(exc, 'status'):
                    continue
                raise
            child_run = Path(child['run_dir'])
            if (store._view(child, {}, include_details=False)['status'] != 'queued'
                    or Directions(child_run).read(field_handoff.EXECUTOR_REQUEST)):
                continue
            if resource_busy(store, entry['resource'], except_id=child['id']):
                waiting[item['id']] = '等待执行器的独占资源释放。'
                continue
            if failed:
                item.setdefault('executor_failures', []).append(failed)
                value.get('failed_cases', {}).pop(item['id'], None)
                value.get('executor_status', {}).pop(item['id'], None)
                if item['id'] in (value.get('fallback') or {}).get('failures', {}):
                    value.pop('fallback', None)
            item.update(executor_task=child['id'], executor_resource=entry['resource'])
            occupied.add(child['id'])
            waiting.pop(item['id'], None)
            write_json(run / filename, value, durable=True)
            break
    return waiting
