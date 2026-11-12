"""Bounded public agent activity, written while a subprocess is still running."""

from __future__ import annotations

import codecs
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.redaction import redact


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_preview(text: str) -> dict:
    text = redact(text)
    return {'text': text[:4000], **({'truncated': True, 'text_length': len(text)} if len(text) > 4000 else {})}


def public_events(value: dict, at: str = '') -> list[dict]:
    """Normalize public messages and tool operations; ignore private model reasoning."""
    try:
        return _public_events(value, at)
    except (AttributeError, TypeError, ValueError):
        return []  # Invalid observation records cannot interrupt supervised work.


def _public_events(value: dict, at: str = '') -> list[dict]:
    at = at or now()
    kind = value.get('type', '')
    if not isinstance(kind, str):
        return []
    if kind in {'tool_call', 'tool_call_update'} and isinstance(value.get('toolCallId'), str):
        if kind == 'tool_call':
            return _public_events({'type':'function_call', 'call_id':value['toolCallId'],
                                   'name':value.get('toolName', value.get('title', 'tool')),
                                   'arguments':value.get('rawInput', {})}, at)
        if value.get('status') not in {'completed', 'failed'}:
            return []
        text = '\n'.join(c.get('content', {}).get('text', '') for c in value.get('content', [])
                         if isinstance(c, dict) and isinstance(c.get('content'), dict)
                         and c['content'].get('type') == 'text')
        return _public_events({'type':'tool_result', 'call_id':value['toolCallId'], 'content':text}, at)
    if kind == 'end' and isinstance(value.get('structuredOutput'), dict):
        summary = value['structuredOutput'].get('summary')
        return [{'at': at, 'kind': 'message', **text_preview(summary)}] if isinstance(summary, str) and summary.strip() else []
    if kind in {'assistant','user'} and isinstance(value.get('message'),dict):
        events=[]
        for block in value['message'].get('content',[]):
            if not isinstance(block,dict):
                continue
            if kind=='assistant' and block.get('type')=='text' and isinstance(block.get('text'),str):
                events.append({'at':at or now(),'kind':'message',**text_preview(block['text'])})
            elif block.get('type')=='tool_use':
                events.extend(_public_events({'type':'function_call','name':block.get('name'),
                                             'arguments':block.get('input',{}),'call_id':block.get('id')},at))
            elif block.get('type')=='tool_result':
                events.extend(_public_events({'type':'tool_result','content':block.get('content'),
                                             'call_id':block.get('tool_use_id')},at))
        return events
    if kind == 'response_item':
        item = value.get('payload') or {}
    elif kind == 'event_msg':
        payload = value.get('payload') or {}
        if payload.get('type') == 'agent_message' and isinstance(payload.get('message'), str):
            return [{'at': at, 'kind': 'message', **text_preview(payload['message'])}]
        return []
    else:
        item = value.get('item', value)
    if not isinstance(item, dict):
        return []
    item_type = item.get('type', '')
    if item.get('channel') == 'analysis' or item.get('phase') in {'analysis', 'reasoning'}:
        return []
    entry = {'at': at, 'kind': 'message', 'text': ''}
    if item.get('call_id') or item.get('id'):
        entry['id'] = str(item.get('call_id') or item['id'])[:200]
    if item_type == 'message' and item.get('role') == 'assistant':
        entry['text'] = '\n'.join(block.get('text', '') for block in (item.get('content') or [])
                                 if isinstance(block, dict) and block.get('type') == 'output_text')
    elif item_type == 'agent_message' and isinstance(item.get('text'), str):
        entry['text'] = item['text']
    elif item_type == 'command_execution':
        entry.update(kind='command', text=str(item.get('command') or ''),
                     status='running' if kind == 'item.started' else 'completed',
                     output=str(item.get('aggregated_output') or '')[-2500:], exit_code=item.get('exit_code'))
    elif item_type == 'file_change':
        paths = [str(change.get('path', '')) for change in (item.get('changes') or []) if isinstance(change, dict)]
        entry.update(kind='file', text='更新文件：' + '、'.join(paths))
    elif item_type in {'function_call', 'custom_tool_call', 'mcp_tool_call'}:
        name = str(item.get('name') or item.get('tool') or 'tool')
        arguments = item.get('arguments', item.get('input', ''))
        if isinstance(arguments, dict):
            args = arguments
            arguments = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments = str(arguments)
            try:
                args = json.loads(arguments)
            except ValueError:
                args = {}
        entry.update(kind='tool', tool=name, text=name, status='running')
        if name.endswith('update_plan') and isinstance(args, dict) and isinstance(args.get('plan'), list):
            plan = [{'step': str(step.get('step', ''))[:300], 'status': str(step.get('status', 'pending'))[:30]}
                    for step in args['plan'][:30] if isinstance(step, dict)]
            entry.update(kind='plan', text=str(args.get('explanation') or '更新执行计划'), plan=plan)
        elif isinstance(args, dict) and isinstance(args.get('cmd') or args.get('command'), str):
            entry.update(kind='command', text=args.get('cmd') or args['command'])
        elif name in {'exec', 'apply_patch'}:
            paths = re.findall(r'\*\*\* (?:Update|Add|Delete) File: ([^\n\\]+)', arguments)
            commands = re.findall(r'(?:"cmd"|\bcmd)\s*:\s*("(?:\\.|[^"\\])*")', arguments)
            if paths:
                entry.update(kind='file', text='修改文件：' + '、'.join(paths[:8]))
            elif commands:
                entry.update(kind='command', text='\n'.join(json.loads(command) for command in commands[:3]))
            else:
                entry['text'] = {'exec': '执行工具操作', 'apply_patch': '修改项目文件'}[name]
        elif 'wait' in name:
            entry['text'] = '等待工具或协作任务完成'
        elif name in {'send_message', 'send_message_to_thread', 'spawn_agent'}:
            entry['text'] = '协调子任务：' + str(args.get('target') or args.get('task_name') or '') if isinstance(args, dict) else '协调子任务'
    elif item_type in {'function_call_output', 'custom_tool_call_output', 'tool_result'}:
        output = item.get('output', item.get('content', ''))
        if isinstance(output, list):
            output = '\n'.join(block.get('text', '') for block in output if isinstance(block, dict)
                               and block.get('type') in {'input_text', 'output_text', 'text'})
        if isinstance(output, str):
            entry.update(kind='result', text=output[-2500:])
    elif item_type == 'error' or kind in {'error', 'turn.failed'}:
        entry.update(kind='error', text=str(item.get('message') or value.get('error') or '执行出错'))
    elif isinstance(value.get('summary'), str) and value.get('status') in {'done', 'continue', 'blocked'}:
        entry['text'] = value['summary']
    elif kind == 'result' and isinstance(value.get('result'), str):
        entry['text'] = value['result']
    else:
        return []
    if not entry['text'].strip():
        return []
    entry.update(text_preview(entry['text']))
    if 'output' in entry:
        entry['output'] = redact(entry['output'])
    return [entry]


class LiveOutput:
    """Keep at most two 256 KiB files, independently of the round log window."""
    def __init__(self, run_dir: Path, role: str, index: int, provider: str, *, announce: bool = True):
        self.path = run_dir / 'live.jsonl'
        self.role, self.index = role, index
        self.decoders = [codecs.getincrementaldecoder('utf-8')(errors='replace') for _ in range(2)]
        self.pending = ['', '']
        self.discarding = [False, False]
        self.text_fragments = ''
        if announce:
            self.emit({'kind': 'phase', 'text': f'{provider} 开始本轮执行', 'provider': provider})

    def emit(self, entry: dict) -> None:
        entry = {'at': now(), **entry, 'role': self.role, 'round': self.index}
        try:
            line = json.dumps(entry, ensure_ascii=False) + '\n'
            if self.path.exists() and self.path.stat().st_size + len(line.encode()) > 256 * 1024:
                self.path.replace(self.path.with_name('live.previous.jsonl'))
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line)
        except OSError:
            pass  # Observability must not interrupt the supervised work.

    def feed(self, stream: int, chunk: bytes) -> None:
        fresh = self.decoders[stream].decode(chunk)
        if self.discarding[stream]:
            _, separator, fresh = fresh.partition('\n')
            if not separator:
                return
            self.discarding[stream] = False
        self.pending[stream] += fresh
        while '\n' in self.pending[stream]:
            line, self.pending[stream] = self.pending[stream].split('\n', 1)
            if len(line) <= 256 * 1024:
                self.line(line, stream)
        if len(self.pending[stream]) > 256 * 1024:
            self.pending[stream] = ''
            self.discarding[stream] = True

    def line(self, line: str, stream: int) -> None:
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except ValueError:
            if not line.lstrip().startswith(('{', '[')):
                self.emit({'kind': 'output' if stream == 0 else 'error', **text_preview(line)})
            return
        if isinstance(value, dict):
            if (value.get('type') == 'text' and isinstance(value.get('data'), str)
                    and value.get('role', 'assistant') == 'assistant' and value.get('channel') != 'analysis'):
                self.text_fragments += value['data']
                if '\n' in self.text_fragments or len(self.text_fragments) >= 240:
                    self._flush_text()
                return
            self._flush_text()
            for entry in public_events(value):
                self.emit(entry)

    def _flush_text(self):
        if self.text_fragments.strip():
            self.emit({'kind':'message', **text_preview(self.text_fragments)})
        self.text_fragments = ''

    def close(self) -> None:
        for stream in range(2):
            self.pending[stream] += self.decoders[stream].decode(b'', final=True)
            if not self.discarding[stream]:
                self.line(self.pending[stream], stream)
            self.pending[stream] = ''
        self._flush_text()
