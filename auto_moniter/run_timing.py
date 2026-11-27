"""Account for active runtime across pause boundaries and supervisor restarts."""

from datetime import datetime
import json
import time

from auto_moniter.run_storage import maintain_run_storage
from auto_moniter.storage import write_json


def restore(usage, heartbeat_path):
    elapsed = float(usage.get('elapsed_s') or 0)
    if usage.get('paused') is True:
        return elapsed
    try:
        heartbeat = json.loads(heartbeat_path.read_text(encoding='utf-8'))
        if heartbeat.get('note') != 'paused at a turn boundary':
            last_tick = datetime.fromisoformat(heartbeat['at'])
            recorded = datetime.fromisoformat(usage['recorded_at'])
            elapsed += max(0, (last_tick - recorded).total_seconds())
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return elapsed


def _save_pause_usage(monitor):
    path = monitor.run_dir / 'state.json'
    if path.is_file():
        state = json.loads(path.read_text(encoding='utf-8'))
        state['usage'] = monitor._usage()
        write_json(path, state, durable=True)


def pause_at_boundary(monitor):
    monitor._paused_at = time.monotonic()
    try:
        _save_pause_usage(monitor)
        monitor._event('paused')
        while not monitor._stop:
            monitor._check_storage_quota()
            maintain_run_storage(monitor)
            monitor._beat('paused at a turn boundary')
            monitor._stop_event.wait(0.2)
            value = monitor._read_control()
            if value is None:
                continue
            action = value.get('action')
            if action == 'stop':
                monitor._process_control(active=True)
            elif action != 'pause':
                break
    finally:
        monitor._started += time.monotonic() - monitor._paused_at
        del monitor._paused_at
        _save_pause_usage(monitor)
        monitor._event('pause_ended', stopped=monitor._stop)
