"""Linux host and process-tree measurements without third-party dependencies."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path


class Metrics:
    def __init__(self, disk_path: Path):
        self.disk_path = disk_path
        self._lock = threading.Lock()
        self._at = 0.0
        self._cpu = None
        self._processes = {}
        self._cached = None

    def sample(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._cached and now - self._at < 2:
                return self._cached
            ticks = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
            total, idle = sum(ticks), ticks[3] + ticks[4]
            cpu = None
            if self._cpu and total > self._cpu[0]:
                cpu = round(100 * (1 - (idle - self._cpu[1]) / (total - self._cpu[0])), 1)
            memory = {}
            for line in Path('/proc/meminfo').read_text().splitlines():
                key, value = line.split(':', 1)
                memory[key] = int(value.strip().split()[0]) * 1024
            disk = shutil.disk_usage(self.disk_path)
            processes = {}
            clock_ticks = os.sysconf('SC_CLK_TCK')
            page_size = os.sysconf('SC_PAGE_SIZE')
            for path in Path('/proc').iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    fields = (path / 'stat').read_text().rpartition(') ')[2].split()
                    if fields[0] == 'Z':
                        continue
                    pid = int(path.name)
                    started = int(fields[19])
                    used = (int(fields[11]) + int(fields[12])) / clock_ticks
                    previous = self._processes.get(pid)
                    percent = None
                    if previous and previous['started'] == started and now > self._at:
                        percent = max(0, (used - previous['seconds']) / (now - self._at) * 100)
                    processes[pid] = {'pid': pid, 'ppid': int(fields[1]), 'started': started,
                                      'seconds': used, 'cpu_percent': percent,
                                      'rss_bytes': max(0, int(fields[21])) * page_size}
                except (OSError, ValueError, IndexError):
                    continue  # Processes can exit at any point during sampling.
            self._cpu = (total, idle)
            self._processes = processes
            self._at = now
            self._cached = {
                'cpu_percent': cpu, 'cpu_count': os.cpu_count(), 'load_average': list(os.getloadavg()),
                'memory_total': memory['MemTotal'],
                'memory_used': memory['MemTotal'] - memory.get('MemAvailable', memory.get('MemFree', 0)),
                'disk_total': disk.total, 'disk_used': disk.used, 'disk_free': disk.free,
                'disk_path': str(self.disk_path), 'processes': processes,
            }
            return self._cached


def process_tree(processes: dict, roots: list[int]) -> dict:
    included = set(roots) & processes.keys()
    children = {}
    for pid, item in processes.items():
        children.setdefault(item['ppid'], []).append(pid)
    pending = list(included)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in included:
                included.add(child)
                pending.append(child)
    entries = [processes[pid] for pid in included]
    samples = [item['cpu_percent'] for item in entries if item['cpu_percent'] is not None]
    return {'process_count': len(entries), 'rss_bytes': sum(item['rss_bytes'] for item in entries),
            'cpu_percent': round(sum(samples), 1) if samples else None}
