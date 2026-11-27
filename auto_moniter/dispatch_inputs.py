"""Local readiness inputs and exact checkpoint binding for deferred assignments."""

import json
import os
from pathlib import Path
import time

from auto_moniter import candidate_checks, readiness_proof
from auto_moniter.quality import relative_path
from auto_moniter.workspace import workspace_fingerprint

WAIT_FILE = 'dispatch-input-wait.json'
DELIVERY_WAIT_FILE = 'delivery-input-wait.json'


def validate_readiness(spec):
    if set(spec) != {'path', 'environment', 'executor', 'max_age_s'}:
        raise ValueError('环境准备回执须配置路径、环境、执行方和有效期。')
    path = spec['path']
    relative_path(path)
    if len(path) > 2000 or Path(path).parts[:1] != ('.auto-moniter',) or len(Path(path).parts) < 2:
        raise ValueError('环境准备回执须保存在工作区的 .auto-moniter 目录内。')
    if (not all(isinstance(spec[key], str) and 0 < len(spec[key].strip()) <= 100
                and not any(ord(char) < 32 for char in spec[key]) for key in ('environment', 'executor'))
            or type(spec['max_age_s']) is not int or not 1 <= spec['max_age_s'] <= 86400):
        raise ValueError('环境和执行方须使用有效标识，有效期须为 1 至 86400 秒。')


def _receipt_ready(root, path, spec, candidate):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            return False
        payload = json.loads(data)
        observed = payload.get('observed_at')
        return (type(observed) is int and 0 <= time.time() - observed <= spec['max_age_s']
                and payload.get('ready') is True and payload.get('candidate_tree') == candidate
                and all(payload.get(key) == spec[key] for key in ('environment', 'executor'))
                and readiness_proof.verified(root, spec, payload))
    except (OSError, ValueError, AttributeError):
        return False


def missing(root, inputs, *, run=None):
    missing_paths = []
    candidate = None
    for entry in inputs:
        structured = isinstance(entry, dict)
        if structured:
            validate_readiness(entry)
        name = entry['path'] if structured else entry
        relative_path(name)
        try:
            path = (root / name).resolve()
            available = path.is_relative_to(root) and path.exists()
        except (OSError, RuntimeError):
            available = False
        if available and structured:
            if candidate is None:
                candidate = workspace_fingerprint(root, ignore_dir=run)
            available = path.is_file() and _receipt_ready(root, path, entry, candidate)
        if not available:
            missing_paths.append(entry)
    return missing_paths


def binding(task, root, checks, policy, directions):
    return candidate_checks.digest([task, str(root), checks, policy, directions])


def ready(wait, state, root: Path, directions, *, run=None):
    task = (state.get('function_tasks') or {}).get('current')
    inputs = wait.get('inputs')
    return bool(state.get('status') == 'action_required' and task
                and state.get('cwd') == str(root) and directions == state.get('dispatch_directions', [])
                and state.get('summary') == wait.get('summary')
                and wait.get('binding') == binding(task, root, state.get('checks', []),
                                                  state.get('quality_policy', {}), directions)
                and isinstance(inputs, list) and inputs and all(isinstance(name, (str, dict)) for name in inputs)
                and not missing(root, inputs, run=run))


def delivery_ready(run, state, marker):
    from auto_moniter.directions import Directions

    wait = Directions(run).read(DELIVERY_WAIT_FILE)
    task = ((marker.get('receipt') or {}).get('context') or {}).get('task')
    inputs = wait.get('inputs')
    root = Path(state['cwd']).resolve()
    return bool(task and isinstance(inputs, list) and inputs and all(isinstance(entry, dict) for entry in inputs)
                and wait.get('binding') == binding(task, root, state.get('checks', []),
                                                  state.get('quality_policy', {}), state.get('dispatch_directions', []))
                and not missing(root, inputs, run=run))
