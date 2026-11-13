"""User-authored follow-up requirements; the supervisor checkpoint owns progress."""

import json
import re
import uuid

from auto_moniter.directions import Directions, now
from auto_moniter.storage import write_json


class RequirementQueue:
    MAX_ITEMS = 31

    def __init__(self, run):
        self.run = run
        self.directions = Directions(run)

    def items(self):
        value = self.directions.read('requirement-queue.json')
        items = value.get('items', [])
        if (not isinstance(items, list) or len(items) > self.MAX_ITEMS or any(
                not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', item['id'])
                or not isinstance(item.get('text'), str) or not item['text'].strip()
                or len(item['text']) > 4000 or '\0' in item['text']
                or not isinstance(item.get('at'), str) for item in items)
                or len({item['id'] for item in items}) != len(items)):
            raise ValueError('排队需求文件格式无效')
        return items

    def state(self):
        return self.directions.read('state.json', 32 * 1024 * 1024)

    def snapshot(self, state=None):
        state = self.state() if state is None else state
        items = self.items()
        by_id = {item['id']: item for item in items}
        active = state.get('active_queued_requirement') or None
        completed = {}
        cycles = state.get('requirement_cycles', [])
        if not isinstance(cycles, list):
            raise ValueError('需求轮次检查点格式无效')
        for cycle in cycles:
            if not isinstance(cycle, dict) or not isinstance(cycle.get('handoff'), dict):
                raise ValueError('需求轮次交付记录无效')
            handoff = cycle['handoff']
            previous = handoff.get('queued_requirement')
            if previous:
                if (not isinstance(previous, dict) or not isinstance(previous.get('id'), str)
                        or by_id.get(previous['id']) != previous or previous['id'] in completed):
                    raise ValueError('已验收排队需求与记录不一致')
                completed[previous['id']] = handoff
        if active:
            if (not isinstance(active, dict) or not isinstance(active.get('id'), str)
                    or by_id.get(active['id']) != active
                    or active['id'] in completed):
                raise ValueError('当前排队需求与检查点不一致')
            if state.get('status') == 'done':
                completed[active['id']] = {'summary': '已通过本轮最终验收'}
                active = None
        return {'active': active,
                'waiting': [{**item, 'position': index + 1} for index, item in enumerate(
                    item for item in items if item['id'] not in completed
                    and (not active or item['id'] != active['id']))],
                'completed': [{**item, 'delivery': completed[item['id']]} for item in items if item['id'] in completed]}

    def _editable(self, state):
        if state.get('status') in {'done', 'failed', 'stalled', 'max_rounds', 'invalid', 'cancelled'}:
            raise ValueError('任务已结束，请创建后续任务')
        if state.get('workflow', 'review-first') != 'review-first':
            raise ValueError('排队需求需要先审核需求的工作流程')

    def append(self, text, position='last'):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\0' in text:
            raise ValueError('请填写 1 到 4000 字的排队需求')
        with self.directions.lock():
            state = self.state()
            self._editable(state)
            items = self.items()
            waiting = self.snapshot(state)['waiting']
            ids = [item['id'] for item in waiting]
            if not isinstance(position, str) or position not in ['first', 'last', *ids]:
                raise ValueError('所选需求已离开等待队列，请刷新后重新选择插队位置')
            if len(items) >= self.MAX_ITEMS:
                raise ValueError('本任务排队需求已达到 31 条上限，请创建后续任务')
            item = {'id': uuid.uuid4().hex, 'text': text.strip(), 'at': now()}
            index = 0 if position == 'first' else len(ids) if position == 'last' else ids.index(position)
            pending = [entry for entry in items if entry['id'] in ids]
            pending.insert(index, item)
            self._save([entry for entry in items if entry['id'] not in ids] + pending)
            return item

    def remove(self, ident):
        with self.directions.lock():
            state = self.state()
            self._editable(state)
            if not isinstance(ident, str) or ident not in {item['id'] for item in self.snapshot(state)['waiting']}:
                raise ValueError('需求已开始处理或已验收，不能从等待队列移除')
            self._save([item for item in self.items() if item['id'] != ident])

    def _save(self, items):
        value = {'version': 1, 'items': items}
        if len(json.dumps(value, ensure_ascii=False).encode()) > 192 * 1024:
            raise ValueError('排队需求已超过容量')
        write_json(self.run / 'requirement-queue.json', value, durable=True)


def prompt_block(item):
    if not item:
        return ''
    return ('\n\nCurrent user-queued requirement (explicitly authorized after the preceding requirement passed acceptance):\n'
            + json.dumps(item, ensure_ascii=False)
            + '\nImplement and review only this selected follow-up requirement. It is an explicit user request, '
              'not an instruction to invent another capability inside the original scope. Preserve accepted work, '
              'fixed checks, exclusions and safety constraints. Do not reopen completed requirements or infer '
              'authorization to implement future queued requirements. Reviewer must refine this request and '
              'approve its implementation plan before worker execution.\n')
