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
