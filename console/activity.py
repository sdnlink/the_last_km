"""Read public live activity, including native sessions from monitors already running."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_moniter.live import public_events
from auto_moniter.redaction import safe


def json_lines(path: Path, limit: int = 512 * 1024, *, head: bool = False) -> list[dict]:
    try:
        if path.is_symlink():
            return []
        with path.open('rb') as stream:
            stream.seek(0, 2)
            length = stream.tell()
            offset = 0 if head else max(0, length - limit)
            stream.seek(offset)
            raw = stream.read(limit)
        lines = raw.splitlines()
        if offset:
            lines = lines[1:]  # The first line can be a truncated JSON record.
        values = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    values.append(value)
            except ValueError:
                pass
        return values
    except OSError:
        return []


def parse_time(value: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, AttributeError):
        return None


EVENT_LABELS = {
    'git_checkpoint': '项目 Git 记录已保存',
    'agent_start': '开始执行', 'review_checkpoint': '提交独立审查', 'agent_failure': '执行异常，准备重试',
    'audit_issues_reported': '审查发现待解决问题', 'audit_issues_resolved': '审查问题已解决',
    'module_accepted': '模块通过验收', 'modules_invalidated': '模块需要重新验收',
    'check_finished': '验收检查结束', 'run_finished': '本次运行结束', 'paused': '任务已暂停',
    'pause_ended': '结束暂停', 'stagnation_recovery': '恢复停滞任务', 'review_rejected': '交付审查未通过',
    'business_head_adopted': '识别业务新提交，继续开发并重新验收',
    'function_next_assignment_rejected': '下一函数派单被拒绝',
    'requirements_assignment_repair': '派单编号格式无效，Reviewer 正在修正',
    'requirements_review_continued': '复用需求审查记录，仅复核待处理项和输入变化',
    'worker_report_repair': 'Worker 正在只读纠正交付报告，保留现有实现',
    'function_submission_recovered': '恢复已有函数交付，直接交 Reviewer 复核并派发后续任务',
    'function_gate_rejected': '函数验收门禁未通过',
    'function_assigned': '已下发函数任务',
    'function_accepted': '函数通过验收',
    'acceptance_checks_added': '已追加验收检查',
    'maintenance_started': '部署维护，保存检查点后恢复',
    'project_change_detected': '发现共享工程变化，等待当前调用结束后复核',
    'project_observation_failed': '共享工程状态暂时无法读取，需要重新核对',
    'project_replanning': 'Reviewer 正在复核工程变化并更新任务',
    'project_review_retry': '工程继续变化，保留工作并重新审查',
    'project_review_stale': '本轮审查输入已变化，结论需要重新核对',
    'project_candidate_stale': '实现期间共享输入变化，先复核再派单',
    'project_integration_review': '共享输入集成需重新审查',
}


class ActivityReader:
    def __init__(self, session_root: Path | None = None):
        self.session_root = session_root or Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
        self._locations = {}
        self._parsed = {}

    def _session(self, cwd: str, start: str, session_id: str = '') -> Path | None:
        key = (cwd, start, session_id)
        cached = self._locations.get(key)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        at = parse_time(start)
        if not at:
            return None
        candidates = []
        valid_id = bool(re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', session_id))
        # Session directories use local dates, so inspect neighboring UTC dates too.
        paths = []
        if valid_id:
            paths = list(self.session_root.glob(f'*/*/*/*{session_id}.jsonl'))[:1000]
        else:
            for offset in (-1, 0, 1):
                day = (at + timedelta(days=offset)).strftime('%Y/%m/%d')
                paths.extend(list((self.session_root / day).glob('*.jsonl'))[:1000])
        for path in paths:
            try:
                if path.is_symlink():
                    continue
                with path.open('rb') as stream:
                    meta = json.loads(stream.readline(65536)).get('payload', {})
                if meta.get('cwd') != cwd or meta.get('parent_thread_id'):
                    continue
                if isinstance(meta.get('source'), dict):
                    continue  # A child agent is not the monitor's root session.
                created = parse_time(meta.get('timestamp', ''))
                if valid_id and meta.get('id') == session_id:
                    candidates.append(path)
                elif not valid_id and created and abs((created - at).total_seconds()) <= 15:
                    candidates.append(path)
            except (OSError, ValueError, TypeError):
                continue
        found = candidates[0] if len(candidates) == 1 else None
        if len(self._locations) > 200:
            self._locations.clear()
        self._locations[key] = (time.monotonic(), found)
        return found

    def _transcript(self, path: Path, start: str) -> list[dict]:
        try:
            info = path.stat()
        except OSError:
            return []
        signature = (str(path), info.st_mtime_ns, info.st_size, start)
        if signature in self._parsed:
            return self._parsed[signature]
        # Keep early public progress messages even when verbose tool results fill the tail.
        lines = json_lines(path, 256 * 1024, head=True) + json_lines(path)
        result = []
        since = parse_time(start)
        for item in lines:
            at = item.get('timestamp', '')
            item_time = parse_time(at)
            if since and (not item_time or item_time < since):
                continue
            result.extend(public_events(item, at))
        if len(self._parsed) > 32:
            self._parsed.clear()
        self._parsed[signature] = result
        return result

    def snapshot(self, run: Path, state: dict, health: dict, status: str, provider: str) -> dict:
        events = json_lines(run / 'events.previous.jsonl') + json_lines(run / 'events.jsonl')
        starts = [e for e in events if e.get('event') == 'agent_start']
        current = starts[-1] if starts else {}
        match = re.search(r'round (\d+): (worker|reviewer)', health.get('activity', ''))
        role = match[2] if match else str(current.get('role', 'worker'))
        if health.get('phase') == 'validating':
            role = 'check'
        index = int(match[1]) if match else int(current.get('round') or state.get('next_round') or 0)
        start = next((e.get('at', '') for e in reversed(starts)
                      if e.get('role') == role and str(e.get('round')) == str(index)), '')
        activity = []
        for name in ('live.previous.jsonl', 'live.jsonl'):
            for entry in json_lines(run / name):
                if entry.get('kind') in {'message', 'command', 'file', 'tool', 'result', 'plan', 'error', 'output', 'phase', 'direction'}:
                    activity.append(entry)
        source = 'live' if activity else 'monitor'
        session = ''
        if provider == 'codex' and start and role in {'worker', 'reviewer'}:
            session_id = state.get('session_id' if role == 'worker' else 'active_reviewer_session_id') or ''
            path = self._session(str(state.get('cwd', '')), start, session_id)
            if path:
                session = path.stem.rsplit('rollout-', 1)[-1]
                native = self._transcript(path, start)
                activity.extend({**entry, 'role': role, 'round': index} for entry in native)
                if native:
                    source = 'codex_session'
        for event in events:
            name = event.get('event')
            if name not in EVENT_LABELS:
                continue
            text = EVENT_LABELS[name]
            if name == 'agent_start':
                text = f"第 {event.get('round', '?')} 轮 · {'独立审查器' if event.get('role') == 'reviewer' else '执行器'}开始工作"
            elif name == 'module_accepted':
                text += '：' + str(event.get('modules') or '')
            elif name == 'check_finished':
                text += f"（退出码 {event.get('exit', '?')}）：{event.get('command', '')}"
            elif name == 'run_finished':
                text += '：' + str(event.get('summary') or event.get('status') or '')
            elif name == 'function_next_assignment_rejected':
                text += '：' + str(event.get('rejected_task_id') or '') + '\n' + str(event.get('reason') or '')
            elif name == 'function_gate_rejected':
                text += '：' + str(event.get('task_id') or '') + '\n' + str(event.get('reasons') or '')
            elif name in {'function_assigned', 'function_accepted'}:
                text += '：' + str(event.get('task_id') or '')
            elif name == 'acceptance_checks_added':
                added = event.get('added')
                text += '：' + ('\n'.join(added) if isinstance(added, list) else str(added or ''))
            elif name == 'maintenance_started':
                text += '：' + str(event.get('reason') or '')
            elif name == 'git_checkpoint':
                text += f"：第 {event.get('round', '?')} 轮 " + ('reviewer' if event.get('role') == 'reviewer' else 'worker')
                text += '\n' + str(event.get('commit', '')) + '\n' + str(event.get('ref', ''))
            activity.append({'at': event.get('at', ''), 'kind': 'phase', 'text': text,
                             'role': event.get('role', ''), 'round': event.get('round', '')})
        activity.sort(key=lambda item: (parse_time(item.get('at', '')) or datetime.min.replace(tzinfo=timezone.utc)))
        current_entries = [e for e in activity if str(e.get('round')) == str(index) and e.get('role') == role]
        unique, seen = [], set()
        for entry in reversed(activity):
            key = (entry.get('id') or entry.get('at'), entry.get('role'), entry.get('kind'),
                   str(entry.get('text')), str(entry.get('round')))
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        activity = list(reversed(unique[:60]))
        # Completed turns retain the full result separately from live previews.
        # Old live records have no truncation flag; label their 4000-char limit
        # without claiming we know how many characters were omitted.
        for entry in activity:
            if entry.get('kind') not in {'message', 'output'}:
                continue
            text = entry.get('text')
            if isinstance(text, str) and len(text) == 4000 and not entry.get('truncated'):
                entry['preview_only'] = True
            round_id, entry_role = str(entry.get('round', '')), entry.get('role')
            if round_id.isdecimal() and len(round_id) <= 9 and entry_role in {'worker', 'reviewer'}:
                name = f'{int(round_id):03d}-{entry_role}-text.md'
                path = run / name
                if path.is_file() and not path.is_symlink():
                    entry['text_file'] = name
        messages = [e for e in current_entries if e.get('kind') in {'message', 'output'}]
        operations = [e for e in current_entries if e.get('kind') in {'command', 'file', 'tool'}]
        operation = dict(operations[-1]) if operations else None
        if operation and operation.get('id') and any(e.get('kind') == 'result' and e.get('id') == operation['id']
                                                     for e in current_entries):
            operation['status'] = 'completed'
        plans = [e['plan'] for e in current_entries if e.get('kind') == 'plan' and isinstance(e.get('plan'), list)]
        plan = plans[-1] if plans else []
        focus = next((step.get('step', '') for step in plan if step.get('status') == 'in_progress'), '')
        recent = next((e for e in reversed(current_entries) if e.get('kind') != 'phase'), None)
        last_output_at = recent.get('at', '') if recent else ''
        parsed_at = parse_time(last_output_at)
        output_age = max(0, (datetime.now(timezone.utc) - parsed_at).total_seconds()) if parsed_at else None
        return safe({'role': role, 'round': index, 'started_at': start, 'source': source,
                     'is_running': status in {'running', 'starting'}, 'session': session,
                     'focus': focus or (messages[-1]['text'] if messages else ''),
                     'operation': operation, 'plan': plan,
                     'last_output_at': last_output_at, 'output_age_s': round(output_age, 1) if output_age is not None else None,
                     'events': activity, 'has_output': recent is not None})
