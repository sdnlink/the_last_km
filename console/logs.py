"""Page retained log files with a sparse newline index and bounded responses."""

from array import array
from collections import OrderedDict
from pathlib import Path
import os
import re

from auto_moniter.redaction import redact


class LogReader:
    stride = 128
    limit_bytes = 65536

    def __init__(self):
        self.indexes = OrderedDict()

    def forget(self, run: Path) -> None:
        for path in list(self.indexes):
            if Path(path).parent == run:
                self.indexes.pop(path, None)

    def read(self, path: Path | None, page: int, page_size: int) -> dict:
        if path is None:
            return self._empty(page_size)
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            previous = self.indexes.pop(str(path), None)
            if (previous and previous['identity'] == identity and stat.st_size >= previous['size']
                    and (stat.st_size != previous['size'] or stat.st_mtime_ns == previous['mtime'])):
                index = previous
            else:
                index = {'identity': identity, 'size': 0, 'breaks': 0,
                         'anchors': array('Q', [0]), 'newline': False}
            stream.seek(index['size'])
            offset = index['size']
            while offset < stat.st_size:
                block = stream.read(min(65536, stat.st_size - offset))
                if not block:
                    break
                for match in re.finditer(b'\n', block):
                    index['breaks'] += 1
                    if index['breaks'] % self.stride == 0:
                        index['anchors'].append(offset + match.end())
                index['newline'] = block.endswith(b'\n')
                offset += len(block)
            index.update(size=offset, mtime=stat.st_mtime_ns)
            self.indexes[str(path)] = index
            while len(self.indexes) > 8:
                self.indexes.popitem(last=False)
            total = index['breaks'] + int(bool(offset) and not index['newline'])
            pages = max(1, (total + page_size - 1) // page_size)
            page = pages if page == 0 else max(1, min(page, pages))
            start = (page - 1) * page_size
            anchor = start // self.stride
            stream.seek(index['anchors'][min(anchor, len(index['anchors']) - 1)])
            for _ in range(start - anchor * self.stride):
                self._line(stream, 0, offset)
            lines = []
            # A single enormous line must not crowd out all other requested lines.
            line_limit = max(128, self.limit_bytes // page_size - 64)
            for number in range(start, min(total, start + page_size)):
                raw, truncated = self._line(stream, line_limit, offset)
                text = redact(raw.decode('utf-8', errors='replace').rstrip('\r\n'))
                text = text.encode('utf-8')[:line_limit].decode('utf-8', errors='ignore')
                if truncated:
                    text += ' …（本行过长，已截断）'
                lines.append({'number': number + 1, 'text': text, 'truncated': truncated})
            content = '\n'.join(line['text'] for line in lines)
            if lines and raw.endswith(b'\n'):
                content += '\n'
            return {'content': content, 'lines': lines,
                    'page': page, 'page_size': page_size, 'pages': pages, 'total_lines': total,
                    'start_line': start + 1 if total else 0, 'end_line': min(total, start + page_size),
                    'limit_bytes': self.limit_bytes}

    @staticmethod
    def _line(stream, limit: int, end: int) -> tuple[bytes, bool]:
        raw = stream.readline(min(limit + 1, max(0, end - stream.tell())))
        truncated = len(raw) > limit
        while raw and not raw.endswith(b'\n') and stream.tell() < end:
            block = stream.readline(min(65536, end - stream.tell()))
            if not block or block.endswith(b'\n'):
                break
        return raw[:limit], truncated

    def _empty(self, page_size: int) -> dict:
        return {'content': '', 'lines': [], 'page': 1, 'page_size': page_size, 'pages': 1,
                'total_lines': 0, 'start_line': 0, 'end_line': 0, 'limit_bytes': self.limit_bytes}
