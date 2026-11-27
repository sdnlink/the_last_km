"""Project-wide conclusion history assembled from saved results and milestone events."""

import json
import re
from collections import defaultdict
from pathlib import Path

from auto_moniter.events import CONCLUSION_EVENTS
from console.activity import json_lines
from console.diagnostics import present_summary

EVENT_STAGES = {
    'round_concluded': ('working', 'worker', ''),
    'requirements_reviewed': ('refining_requirements', 'reviewer', '需求梳理已结束。'),
    'requirement_coverage_reviewed': ('refining_requirements', 'reviewer', '已复核需求覆盖范围。'),
    'acceptance_checks_concluded': ('validating', 'watcher', ''),
    'approved_local_commit': ('publishing', 'watcher', '已保存审核通过的提交。'),
    'run_finished': ('closing_agents', 'watcher', '已保存本次运行结论。'),
    'referee_decision': ('refereeing', 'referee', '已安排恢复路径。'),
    'referee_rejected': ('refereeing', 'referee', '恢复决策未通过执行校验。'),
}


def _events(run: Path):
    for name in ('events.previous.jsonl', 'events.jsonl'):
        yield from (row for row in json_lines(run / name) if row.get('event') in CONCLUSION_EVENTS)
    path = run / 'milestones.jsonl'
    try:
        if not path.is_symlink():
            with path.open('rb') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict) and row.get('event') in CONCLUSION_EVENTS:
                        yield row
    except OSError:
        pass


def _coverage_summary(run, receipt):
    if not re.fullmatch(r'\d+-requirement-coverage\.json', receipt or ''):
        return ''
    path = run / receipt
    try:
        if not path.is_symlink() and path.stat().st_size <= 512 * 1024:
            result = json.loads(path.read_bytes())
            return result.get('summary', '') if isinstance(result, dict) else ''
    except (OSError, ValueError):
        pass
    return ''


def stage_history(run, state, conclusions, acceptance):
    stages = defaultdict(list)

    def add(stage, role, summary, *, round=None, cycle=None, status='', at='', commit='', checks=None, task_id=''):
        stages[stage].append({'role': role, 'summary': present_summary(summary), 'round': round, 'cycle': cycle,
                              'status': status, 'at': at, 'commit': commit, 'checks': checks, 'task_id': task_id})

    events = sorted({(row.get('at'), row['event']): row for row in _events(run)}.values(),
                    key=lambda row: row.get('at') or '')
    recorded_checks = {(row.get('round'), row.get('passed'), row.get('total'), row.get('status'))
                       for row in events if row['event'] == 'acceptance_checks_concluded'}
    saved_commits = {row.get('commit') for row in events if row['event'] == 'approved_local_commit'}
    saved_cycles = {str(row['cycle']) for row in events if row['event'] == 'requirements_reviewed'
                    and row.get('cycle')}
    briefs = [cycle.get('requirements_review') for cycle in (state.get('requirement_cycles') or [])]
    briefs.append(state.get('requirements_review'))
    for brief in briefs:
        if brief and brief.get('summary') and str(brief.get('cycle')) not in saved_cycles:
            add('refining_requirements', 'reviewer', brief['summary'], cycle=brief.get('cycle'),
                status=brief.get('status', ''))

    records = state.get('rounds')
    if not isinstance(records, list):
        records = state.get('records') or []
    check_rounds = set()
    recorded_rounds = {(item['role'], str(item.get('index'))) for item in conclusions}

    def check_result(index, receipt, task_id=''):
        checks = receipt.get('checks') or []
        if not checks:
            return
        passed = sum(check.get('status') == 'passed' for check in checks)
        check_rounds.add(str(index))
        status = 'passed' if receipt.get('passed', receipt.get('complete')) else 'failed'
        if (str(index), str(passed), str(len(checks)), status) in recorded_checks:
            return
        add('validating', 'watcher', '', round=index, task_id=task_id,
            status=status,
            checks={'passed': passed, 'total': len(checks), 'source_changed': bool(receipt.get('source_changed'))})

    for item in conclusions:
        if item.get('summary'):
            stage = 'working' if item['role'] == 'worker' else 'reviewing'
            add(stage, item['role'], item['summary'], round=item.get('index'),
                status=item.get('status'), task_id=item.get('task_id'))
        checkpoint = item.get('git_checkpoint') or {}
        business = checkpoint.get('business_commit') or {}
        commit = business.get('commit')
        if commit and commit not in saved_commits:
            saved_commits.add(commit)
            add('publishing', 'watcher', '已保存审核通过的业务提交。', round=item.get('index'),
                commit=commit, task_id=item.get('task_id'))
    last_business = (state.get('git_history') or {}).get('last_business_commit') or {}
    if last_business.get('commit') and last_business['commit'] not in saved_commits:
        add('publishing', 'watcher', '已保存审核通过的业务提交。', round=last_business.get('round'),
            commit=last_business['commit'])
    for item in records:
        receipt = (item.get('watcher_result') or {}).get('candidate_checks')
        if receipt:
            check_result(item.get('index'), receipt, item.get('task_id'))
    if acceptance.get('complete') and str(acceptance.get('round')) not in check_rounds and not any(
            row['event'] == 'acceptance_checks_concluded' and row.get('round') == str(acceptance.get('round'))
            for row in events):
        check_result(acceptance.get('round'), acceptance)

    for row in events:
        name = row['event']
        stage, role, fallback = EVENT_STAGES[name]
        if name == 'requirements_reviewed' and not row.get('summary'):
            # Older event lines have status only. The saved brief above carries the conclusion.
            continue
        if name == 'round_concluded':
            role = row.get('role')
            if role not in {'worker', 'reviewer'} or (role, str(row.get('round'))) in recorded_rounds:
                continue
            stage = 'working' if role == 'worker' else 'reviewing'
            if role == 'reviewer' and row.get('watcher_status') == 'rejected':
                row = {**row, 'summary': 'watcher 未接受本轮审计更新，请核对系统处理结果。'}
        checks = None
        if name == 'acceptance_checks_concluded':
            checks = {'passed': row.get('passed'), 'total': row.get('total'),
                      'source_changed': row.get('source_changed') == 'True'}
        summary = row.get('summary') or row.get('reason')
        if not summary and name == 'requirement_coverage_reviewed':
            summary = _coverage_summary(run, row.get('receipt'))
        add(stage, role, summary or fallback, round=row.get('round'),
            cycle=row.get('cycle'), status=row.get('status') or ('rejected' if name == 'referee_rejected' else ''),
            at=row.get('at'), commit=row.get('commit'), checks=checks, task_id=row.get('task_id'))
    if not stages['closing_agents'] and state.get('summary') and state.get('stop_reason'):
        add('closing_agents', 'watcher', state['summary'], status=state.get('status'))
    decision = (state.get('referee') or {}).get('decision') or {}
    if not stages['refereeing'] and decision.get('summary'):
        add('refereeing', 'referee', decision['summary'])
    # Retained and pruned rounds may come from different sources after a resume.
    for stage in ('working', 'reviewing', 'validating', 'publishing'):
        stages[stage].sort(key=lambda item: int(item['round']) if str(item['round']).isdecimal() else -1)
    return dict(stages)
