"""Task roles and conclusions, separate from command output and monitor events."""

from __future__ import annotations

import json
import re


def conclusion_event(event: dict) -> bool:
    """Native providers sometimes publish their final JSON as a public message."""
    if event.get('kind') not in {'message', 'output'}:
        return False
    try:
        value = json.loads(event.get('text', ''))
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and 'status' in value and 'summary' in value


def review_wait(state: dict, health: dict, status: str, rounds: list, current_round: int | None) -> dict:
    schedule = health.get('review_schedule') or state.get('review_schedule') or {}
    every = schedule.get('every_worker_rounds')
    every = every if type(every) is int and every >= 0 else None
    elapsed = schedule.get('rounds_since_review')
    elapsed = elapsed if type(elapsed) is int and elapsed >= 0 else None
    last = next((item for item in reversed(rounds) if item.get('role') == 'reviewer'), None)
    last_round = (last or {}).get('index')
    failures = [item for item in rounds if item.get('role') == 'worker'
                and (last_round is None or item.get('index', 0) > last_round)
                and (item.get('exit_code') or item.get('timed_out') or item.get('interruption') or item.get('failure'))]
    due = bool(every and elapsed is not None and elapsed >= every)
    disabled = (state.get('provider_config') or {}).get('no_review') or schedule.get('enabled') is False
    phase = health.get('phase')
    if disabled:
        reason = '本任务未启用独立审核。'
    elif status == 'queued':
        reason = '任务尚未启动，启动后按审核频率调度。'
    elif status == 'paused':
        reason = '任务已暂停，恢复后继续调度审核。'
    elif status not in {'running', 'starting'}:
        reason = '任务当前未在执行；下方为最近一次审核结论。' if last else '任务当前未在执行，尚无审核结论。'
    elif phase == 'reviewing':
        reason = f'正在独立审核第 {current_round} 轮工作及项目改动。'
    elif phase == 'validating':
        reason = 'watcher 正在执行验收检查。'
    elif due:
        reason = f'已达到周期审核阈值（{elapsed}/{every} 轮），等待当前 worker 正常结束并返回有效结论后触发审核。'
    elif every == 0:
        reason = '周期审核已关闭；worker 提交完成候选或外部阻塞时触发审核。'
    elif every is not None:
        reason = f'每 {every} 轮触发周期审核；等待 worker 本轮结果，完成候选及外部阻塞会立即送审。'
    else:
        reason = '等待 worker 返回可审核的结果；该运行未保存审核频率。'
    if failures and not disabled and status in {'running', 'starting', 'paused'}:
        names = '、'.join(str(item.get('index')) for item in failures[-5:])
        reason += f' 第 {names} 轮 worker 超时、停止或执行失败，累计到轮次计数，但未继续进入审核。'
    return {'every_worker_rounds': every, 'rounds_since_review': elapsed,
            'due': due, 'last_review_round': last_round, 'failed_worker_rounds': [item.get('index') for item in failures],
            'wait_reason': reason}


def work_details(state: dict, health: dict, activity: dict, status: str, heartbeat: dict,
                 guardian: dict, worker: str, reviewer: str) -> dict:
    rounds = state.get('rounds')
    if not isinstance(rounds, list):
        rounds = state.get('records') or []
    conclusions = [{key: item.get(key) for key in ('index', 'role', 'status', 'summary', 'done', 'gaps',
                    'next_actions', 'evidence', 'blockers', 'exit_code', 'elapsed_s', 'git_checkpoint')}
                   for item in rounds[-40:] if item.get('role') in {'worker', 'reviewer'}]
    # Older/pruned runs still expose their latest business conclusions.
    for role in ('worker', 'reviewer'):
        last_key = 'last_worker' if role == 'worker' else 'last_review'
        if not any(item['role'] == role for item in conclusions) and state.get(last_key):
            conclusions.append({**state[last_key], 'role': role, 'index': None})
    active = status in {'running', 'starting'}
    phase = health.get('phase')
    round_match = re.search(r'\bround (\d+):', health.get('activity', ''))
    current_round = int(round_match[1]) if round_match else activity.get('round')
    current = 'worker' if phase == 'working' else 'reviewer' if phase == 'reviewing' else None
    events = activity.get('events', [])
    workflow = [event for event in events if not conclusion_event(event)]
    roles = {}
    for role, provider in (('worker', worker), ('reviewer', reviewer)):
        last = next((item for item in reversed(conclusions) if item['role'] == role), None)
        role_events = [e for e in workflow if e.get('role') == role and e.get('kind') != 'phase'][-16:]
        executing = active and current == role
        role_status = ('running' if executing else 'paused' if status == 'paused' else
                       'waiting' if active else 'not_started' if status == 'queued' else 'stopped')
        if status == 'done':
            role_status = 'completed'
        if role == 'reviewer' and (state.get('provider_config') or {}).get('no_review'):
            role_status = 'disabled'
        same_role = activity.get('role') == role and activity.get('round') == current_round
        focus = activity.get('focus') if executing and same_role else ''
        if executing and (not focus or conclusion_event({'kind': 'message', 'text': focus})):
            focus = next((e.get('text', '') for e in reversed(role_events)
                          if same_role and e.get('round') == activity.get('round')
                          and e.get('kind') in {'message', 'command', 'tool', 'plan'}), '')
        roles[role] = {'role': role, 'provider': provider, 'status': role_status,
                       'round': current_round if executing else (last or {}).get('index'),
                       'session_id': state.get('session_id') or '' if role == 'worker' else
                                     state.get('active_reviewer_session_id') or state.get('reviewer_session_id') or '',
                       'pid': heartbeat.get('child_pid') if executing else None,
                       'current_work': focus or ('正在实现任务' if executing and role == 'worker' else
                                                '正在核对改动与验收证据' if executing else ''),
                       'last_conclusion': last, 'events': role_events}
    roles['reviewer'].update(review_wait(state, health, status, rounds, current_round))
    roles['watcher'] = {'role': 'watcher', 'provider': 'auto-moniter',
                        'status': 'monitoring' if active or status == 'paused' else
                                  'not_started' if status == 'queued' else 'stopped',
                        'current_work': health.get('activity', ''), 'health': health.get('health'),
                        'heartbeat_age_s': health.get('heartbeat_age_s'),
                        'pid': heartbeat.get('supervisor_pid') or heartbeat.get('pid'),
                        'guardian_pid': guardian.get('guardian_pid'), 'child_pid': heartbeat.get('child_pid'),
                        'round': health.get('rounds', 0), 'usage': health.get('usage', {}),
                        'review_schedule': health.get('review_schedule', {}),
                        'recovery_count': health.get('recovery_count', 0),
                        'events': [e for e in workflow if e.get('kind') == 'phase'][-20:]}
    return {'roles': roles, 'workflow': workflow, 'conclusions': conclusions}
