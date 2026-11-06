"""Persistent user amendments with receipts tied to actual agent invocations."""

from __future__ import annotations

import json
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.storage import write_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Directions:
    def __init__(self, run: Path):
        self.run = run

    def read(self, name: str, max_size: int = 256 * 1024) -> dict:
        try:
            path = self.run / name
            if path.stat().st_size > max_size or path.is_symlink():
                raise ValueError('任务补充要求文件无效或超过容量')
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('任务补充要求文件格式无效')
            return value
        except FileNotFoundError:
            return {}

    def messages(self) -> list[dict]:
        items = self.read('directions.json').get('messages', [])
        if not isinstance(items, list) or len(items) > 64 or any(
                not isinstance(item, dict) or not isinstance(item.get('text'), str)
                or not isinstance(item.get('id'), str) or len(item['text']) > 4000 for item in items):
            raise ValueError('任务补充要求格式无效')
        return items

    def append(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\x00' in text:
            raise ValueError('请填写 1 到 4000 字的补充要求')
        with self.lock():
            # Serialize with final report persistence: a request racing delivery
            # is either included in another turn or explicitly rejected.
            state = self.read('state.json', 32 * 1024 * 1024)
            if state.get('status') in {'done', 'failed', 'stalled', 'max_rounds', 'invalid', 'cancelled'}:
                raise ValueError('任务已结束，请创建后续任务')
            items = self.messages()
            item = {'id': uuid.uuid4().hex, 'text': text.strip(), 'at': now()}
            value = {'version': 1, 'messages': [*items, item]}
            if len(items) >= 64 or len(json.dumps(value, ensure_ascii=False).encode()) > 128 * 1024:
                raise ValueError('本任务补充要求已达到容量，请创建后续任务')
            write_json(self.run / 'directions.json', value, durable=True)
            return item

    @contextmanager
    def lock(self):
        self.run.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.run / 'directions.lock').open('a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def receipt(self, ids: list[str], role: str, index: int, *, responded: bool = False) -> None:
        if not ids:
            return
        path = self.run / 'direction-receipts.json'
        # The supervisor is the only receipt writer; request threads write a separate file.
        value = self.read(path.name)
        role_receipts = value.setdefault(role, {})
        for ident in ids:
            previous = role_receipts.get(ident, {})
            role_receipts[ident] = {**previous, 'round': index,
                                    'status': 'responded' if responded else 'sent',
                                    'at': now()}
            if responded:
                role_receipts[ident].update(responded_at=now(), responded_round=index)
        write_json(path, value, durable=True)

    def snapshot(self) -> list[dict]:
        receipts = self.read('direction-receipts.json')
        return [{**item, 'worker': (receipts.get('worker') or {}).get(item['id']),
                 'reviewer': (receipts.get('reviewer') or {}).get(item['id'])} for item in self.messages()]
