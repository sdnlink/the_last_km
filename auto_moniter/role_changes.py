"""Explicit task-local role reassignment, preserving acceptance and isolation."""
import json


def normalize_provider_config(config):
    """Fill isolation defaults the same way resume validation does for legacy checkpoints."""
    if not isinstance(config, dict):
        return config
    value = dict(config)
    value.setdefault('review_isolation', 'provider')
    value.setdefault('worker_isolation', 'provider')
    value.setdefault('protect_paths', [])
    return value


def authorized_change(run, saved, requested):
    path = run / 'role-change.json'
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get('id'), str):
        return None
    saved = normalize_provider_config(saved)
    requested = normalize_provider_config(requested)
    origin = normalize_provider_config(value.get('from'))
    target = normalize_provider_config(value.get('to'))
    if origin != saved or target != requested:
        return None
    if {k: v for k, v in saved.items() if k not in ('worker', 'reviewer')} != {
            k: v for k, v in requested.items() if k not in ('worker', 'reviewer')}:
        return None
    if any((c.get(role) or {}).get('name') == 'paseo'
           for c in (saved, requested) for role in ('worker', 'reviewer')):
        return None
    return value
