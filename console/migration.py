"""Build a new task from an inactive task's remaining work."""

import json
import math

from auto_moniter.function_plan import restore, waiting
from auto_moniter.function_tasks import FunctionTasks, targets
from auto_moniter.migration import read
from auto_moniter.requirement_queue import RequirementQueue
from console.settings import DEFAULTS


STATUSES = {'stopped', 'blocked', 'action_required', 'budget_exceeded', 'failed', 'stalled', 'max_rounds'}


def eligible(meta, state, status):
    return (status in STATUSES and meta.get('managed') and not meta.get('parallel_project')
            and meta.get('verification_mode') != 'planning'
            and state.get('workflow', meta.get('workflow')) == 'review-first'
            and state.get('task_type', meta.get('task_type', 'code')) == 'code'
            and bool(state.get('usage')))


def original_requirement(run, meta):
    path = run / 'requirement.md'
    if path.is_symlink():
        raise ValueError('原任务需求文件不能是符号链接')
    try:
        with path.open(encoding='utf-8') as requirement:
            return requirement.read(32001)
    except FileNotFoundError:
        return meta['requirement']


def remaining(limit, spent):
    if (type(limit) not in (int, float) or type(spent) not in (int, float)
            or not math.isfinite(limit) or not math.isfinite(spent) or min(limit, spent) < 0):
        raise ValueError('原任务的预算记录无效')
    if not limit:
        return 0
    value = limit - spent
    if value <= 0:
        raise ValueError('原任务的预算已耗尽，请先明确追加运行预算')
    return value


def build(meta, state, run, requirement):
    usage = state['usage']
    runtime = usage.get('max_runtime_s', meta.get('max_runtime_seconds', meta.get('max_runtime_minutes', 120) * 60))
    seconds = math.floor(remaining(runtime, usage.get('elapsed_s', 0)))
    if not seconds and runtime:
        raise ValueError('原任务剩余运行时间不足一秒')
    calls = remaining(usage.get('max_agent_calls', meta.get('max_agent_calls', 100)), usage.get('agent_calls', 0))
    if type(calls) is not int:
        raise ValueError('原任务的预算记录无效')
    rounds = int(remaining(meta.get('max_rounds', 0), max(0, state.get('next_round', 1) - 1)))
    quality = dict(state.get('quality_policy') or meta.get('quality_policy') or {})
    reported = usage.get('provider_reported') or {}
    for limit, spent, missing in [('max_tokens', 'tokens', 'unknown_token_turns'),
                                  ('max_cost_usd', 'cost_usd', 'unknown_cost_turns')]:
        if quality.get(limit):
            if spent not in reported or reported.get(missing) or reported.get('unreported_turns'):
                raise ValueError('原任务的模型用量记录不完整，无法计算剩余预算')
            quality[limit] = remaining(quality[limit], reported[spent])

    queue = RequirementQueue(run).snapshot(state)
    modules = state.get('modules', meta.get('modules', []))
    assignments = FunctionTasks(state.get('function_tasks'))
    plan = restore(state.get('function_plan'))
    definition = ('id', 'title', 'acceptance', 'depends_on', 'checks', 'files')
    pending = ([assignments.current] if assignments.current else []) + waiting(plan, assignments)
    previous = read(run)
    regression_files = list(dict.fromkeys([
        *previous.get('regression_files', []),
        *(name for row in assignments.accepted for name in row['task'].get('test_files', [])),
    ]))
    context = {
        'source_task_id': meta['id'],
        'source_commit': ((state.get('git_history') or {}).get('business_head') or {}).get('commit', ''),
        'active_queued_requirement': (queue['active'] or {}).get('id', ''),
        'accepted_modules': [module['id'] for module in modules if module.get('status') == 'accepted'],
        'pending_modules': [module['id'] for module in modules if module.get('status') != 'accepted'],
        'earlier_source_task_id': previous.get('source_task_id', ''),
        'regression_files': regression_files,
        'accepted_assignments': [{'id': row['task']['id'], 'targets': targets(row['task'])}
                                 for row in assignments.accepted],
        'pending_assignments': [{**{key: task[key] for key in ('id', 'module_id', 'objective', 'verification')},
                                 'targets': targets(task)}
                                for task in pending],
        'open_issues': [{'id': item['id'], 'summary': item['summary'], 'module_ids': item['module_ids']}
                        for item in (state.get('audit_issues') or {}).get('items', []) if item['status'] != 'resolved'],
    }
    if len(json.dumps(context, ensure_ascii=False).encode('utf-8')) + 1 > 2 * 1024 * 1024:
        raise ValueError('原任务剩余工作记录超过迁移容量')
    if queue['active']:
        requirement = queue['active']['text']
    title = meta['title'].removesuffix(' · 续做')
    keys = (*DEFAULTS, 'provider', 'reviewer', 'model', 'reviewer_model', 'paseo_backend',
            'reviewer_paseo_backend', 'protect_paths', 'review_every')
    payload = {key: meta[key] for key in keys if key in meta}
    referee = state.get('referee')
    if isinstance(referee, dict):
        payload.update(referee=referee.get('enabled', False), referee_model=referee.get('model', ''))
    payload.update({key: state['execution_settings'][key] for key in DEFAULTS
                    if key in (state.get('execution_settings') or {})})
    if 'protect_paths' in state:
        payload['protect_paths'] = state['protect_paths']
    checks = state.get('checks', meta.get('checks', []))
    dispatch = bool(state.get('function_dispatch', meta.get('function_dispatch')))
    verification_mode = 'executable' if dispatch or checks or any(module.get('checks') for module in modules) else 'review_only'
    payload.update(title=title[:155] + ' · 续做', cwd=meta['cwd'], requirement=requirement, workflow='review-first',
                   task_type='code', function_dispatch=dispatch,
                   full_function_plan=bool(state.get('full_function_plan', meta.get('full_function_plan'))),
                   verification_mode=verification_mode, checks=checks,
                   modules=[{key: module[key] for key in definition if key in module} for module in modules],
                   max_runtime_minutes=math.ceil(seconds / 60), max_agent_calls=calls, max_rounds=rounds)
    if quality:
        payload['quality_policy'] = quality
    waiting_items = [{key: item[key] for key in ('id', 'text', 'at')} for item in queue['waiting']]
    return payload, context, seconds, waiting_items
