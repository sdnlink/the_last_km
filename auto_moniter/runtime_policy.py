"""Bounded transport waits and durable end-of-budget handoffs."""
import json
import re
import time


class RetryWatch:
    def __init__(self, limit_s):
        self.limit_s = limit_s
        self.since = None
        self.buffers = ['', '']

    def feed(self, stream, chunk):
        self.buffers[stream] += chunk.decode('utf-8', errors='replace')
        lines = self.buffers[stream].split('\n')
        self.buffers[stream] = lines.pop()[-8192:]
        for line in lines:
            value = None
            try:
                value = json.loads(line)
            except ValueError:
                pass
            kind = value.get('type', '') if isinstance(value, dict) else ''
            if not isinstance(kind, str):
                continue
            error_item = isinstance(value, dict) and isinstance(value.get('item'), dict) and value['item'].get('type') == 'error'
            retry = bool(re.match(r'^\s*(?:Reconnecting|Retrying|Retry attempt|stream error)', line, re.I)
                         or (kind == 'error' or error_item) and re.search(r'connection|upstream|50[234]|retry|reconnecting', line, re.I))
            if retry:
                if self.since is None:
                    self.since = time.monotonic()
            elif stream == 0 and line.strip() and not error_item and kind not in {'turn.started', 'thread.started', 'error'}:
                self.since = None

    def exhausted(self):
        return bool(self.limit_s and self.since is not None and time.monotonic()-self.since >= self.limit_s)


def reserve_seconds(max_runtime):
    return min(120, max_runtime * .05) if max_runtime >= 300 else 0
