"""Read-only validation of evolution checkpoints; no recovery actions live here."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat

from auto_moniter.health import valid_report


_LIMIT = 32 * 1024 * 1024
_INT_LIMITS = ('max_agent_calls', 'memory_bytes', 'file_bytes', 'max_run_bytes',
               'max_history_bytes', 'max_agent_state_bytes', 'max_tokens')
_LIMITS = (*_INT_LIMITS, 'max_runtime_s', 'max_cost_usd', 'max_output_bytes')
_HASHES = ('requirement_sha256', 'acceptance_sha256', 'worker_args_sha256',
           'reviewer_args_sha256', 'policy_sha256')
_ACTIVE = {'selected', 'baseline', 'implementing', 'reviewed', 'validating', 'candidate',
           'ready', 'promoting', 'activating', 'rolling_back'}
_WRAPPERS = {'stopped', 'deferred', 'baseline_failed', 'rejected', 'candidate_failed',
             'recovery_required', 'rollback_activation_failed', 'failed'}
_COMPLETE = {'promoted', 'rolled_back', 'no_change'}
_PHASES = _ACTIVE | _WRAPPERS | _COMPLETE
_PROMOTION = {'ready', 'promoting', 'activating', 'rolling_back', 'recovery_required',
              'promoted', 'rolled_back', 'rollback_activation_failed'}


class CheckpointError(ValueError):
    """The saved evidence cannot safely describe the requested recovery context."""


def _path(value, label, *, argument=False):
    if not isinstance(value, (str, Path)) or not str(value) or '\0' in str(value):
        raise CheckpointError('Invalid checkpoint path: ' + label)
    path = Path(value)
    if '..' in path.parts or (not argument and not path.is_absolute()):
        raise CheckpointError('Checkpoint path is not absolute and canonical: ' + label)
    path = path.absolute()
    try:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise CheckpointError('Symbolic links are forbidden: ' + label)
    except OSError as exc:
        raise CheckpointError('Cannot inspect checkpoint path: ' + label) from exc
    return path


def _bytes(path, *, limit=_LIMIT):
    """Open every path component without following links, and bound the read."""
    descriptor = None
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(path.anchor, directory_flags)
        for part in path.parts[1:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CheckpointError('Checkpoint evidence is not a bounded regular file: ' + path.name)
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise CheckpointError('Checkpoint evidence is too large: ' + path.name)
        return data
    except OSError as exc:
        raise CheckpointError('Cannot read checkpoint evidence: ' + path.name) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _object(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON key')
            value[key] = item
        return value

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('Non-finite JSON number')
        return result

    def constant(_value):
        raise ValueError('Non-finite JSON constant')

    data = _bytes(path)
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_float=finite, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CheckpointError('Invalid checkpoint JSON: ' + path.name) from exc
    if not isinstance(value, dict):
        raise CheckpointError('Checkpoint evidence must be an object: ' + path.name)
    return value


def _number(value, label, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float)) or value < 0
            or (type(value) is float and not math.isfinite(value))):
        raise CheckpointError('Invalid checkpoint number: ' + label)
    return value


def _hash(value, label, *, commit=False):
    pattern = r'(?:[0-9a-f]{40}|[0-9a-f]{64})' if commit else r'[0-9a-f]{64}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise CheckpointError('Invalid checkpoint hash: ' + label)
    return value


def _identity(value):
    if not isinstance(value, dict):
        raise CheckpointError('Missing checkpoint configuration identity')
    for name in _HASHES:
        _hash(value.get(name), 'config_identity.' + name)
    for name in ('model', 'reviewer_model'):
        if not isinstance(value.get(name), str):
            raise CheckpointError('Invalid checkpoint model identity: ' + name)
    for role in ('worker', 'reviewer'):
        if role == 'reviewer' and role in value and value[role] is None:
            continue
        provider = value.get(role)
        if (not isinstance(provider, dict)
                or not all(isinstance(provider.get(key), str) for key in ('name', 'backend', 'mode'))):
            raise CheckpointError('Invalid checkpoint provider identity: ' + role)
        for name in ('binary_sha256', 'command_hash'):
            _hash(provider.get(name), role + '.' + name)
    limits = value.get('limits')
    if not isinstance(limits, dict) or not all(name in limits for name in _LIMITS):
        raise CheckpointError('Missing original checkpoint budgets')
    for name in _LIMITS:
        if name == 'max_output_bytes' and limits[name] is None:
            continue
        _number(limits[name], name, integer=name in _INT_LIMITS or name == 'max_output_bytes')
    return limits


def _usage(value, *, limits=None, required=False):
    if not isinstance(value, dict):
        raise CheckpointError('Invalid checkpoint usage object')
    if required and not all(name in value for name in ('agent_calls', 'elapsed_s')):
        raise CheckpointError('Missing known checkpoint usage')
    result = dict(value)
    for name in ('agent_calls', 'elapsed_s', *_LIMITS):
        if name not in value:
            continue
        if not (name == 'max_output_bytes' and value[name] is None):
            _number(value[name], 'usage.' + name,
                    integer=name in _INT_LIMITS or name in {'agent_calls', 'max_output_bytes'})
        if limits is not None and name in _LIMITS and value[name] != limits[name]:
            raise CheckpointError('Checkpoint budget copies disagree: ' + name)
    if 'provider_reported' in value:
        reported = value['provider_reported']
        names = {'tokens', 'cost_usd', 'reported_turns', 'unreported_turns',
                 'unknown_token_turns', 'unknown_cost_turns'}
        if not isinstance(reported, dict) or set(reported) - names:
            raise CheckpointError('Invalid checkpoint provider usage')
        for name, amount in reported.items():
            _number(amount, 'provider_reported.' + name, integer=name != 'cost_usd')
    if limits is not None:
        result.update(limits)
    return result


def _promotion_evidence(record, directory, workspace, monitor):
    """Verify persisted approval/log evidence, without executing historical commands."""
    candidate = _hash(record.get('candidate'), 'candidate', commit=True)
    if monitor is None:
        raise CheckpointError('Promotion checkpoint is missing its monitor run')
    for name in ('state.json', 'report.json'):
        evidence = _object(_path(monitor / name, 'monitor ' + name))
        if name == 'report.json' and not valid_report(evidence):
            raise CheckpointError('Invalid final promotion report structure')
        history = evidence.get('git_history')
        if (evidence.get('status') != 'done' or not isinstance(history, dict)
                or any(not isinstance(history.get(key), dict)
                       or history[key].get('commit') != candidate
                       for key in ('business_head', 'last_business_commit'))):
            raise CheckpointError('Promotion lacks final review for its exact candidate')
        if name == 'state.json' and _path(evidence.get('cwd'), 'monitor cwd') != workspace:
            raise CheckpointError('Promotion monitor belongs to another workspace')
        if 'usage' in evidence:
            _usage(evidence['usage'])
    rows = record.get('candidate_checks')
    if not isinstance(rows, list) or len(rows) not in {4, 5}:
        raise CheckpointError('Promotion checkpoint lacks complete candidate checks')
    python = None
    python_args = (['-m', 'compileall', '-q', 'auto_moniter', 'console', 'tests'],
                   ['-m', 'unittest', 'discover', '-q'], ['-m', 'auto_moniter', 'selftest'])
    for index, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or type(row.get('exit_code')) is not int or row['exit_code'] != 0
                or row.get('timed_out') is not False or row.get('source_changed') is not False
                or row.get('interruption') != '' or not isinstance(row.get('command'), str)
                or not row['command'].strip()):
            raise CheckpointError('Promotion checkpoint has an unsuccessful candidate check')
        if index <= 3:
            try:
                argv = shlex.split(row['command'])
            except ValueError as exc:
                raise CheckpointError('Invalid fixed promotion check command') from exc
            if not argv or argv[1:] != python_args[index - 1] or (python is not None and argv[0] != python):
                raise CheckpointError('Promotion checkpoint does not match the fixed checks')
            python = argv[0]
        elif row['command'] != ('git diff --check ' + record['base']['commit'] + '..HEAD' if index == 4
                                else 'node --test tests/*.mjs'):
            raise CheckpointError('Promotion checkpoint does not match the fixed checks')
        name = f'candidate-{index:02d}.log'
        if row.get('log') != name:
            raise CheckpointError('Promotion check log escapes its cycle')
        expected = _hash(row.get('sha256'), 'candidate check log')
        if hashlib.sha256(_bytes(_path(directory / name, 'candidate log'), limit=16 * 1024 * 1024)).hexdigest() != expected:
            raise CheckpointError('Promotion candidate log hash changed')


def load_checkpoint(path, expected_source, *, allowed_versions=(1, 2), purpose='resume'):
    """Return a validated context with Path locations and the untouched decoded record.

    Purposes are ``resume``, ``promotion`` and ``inspect``. General resume requires
    the complete version-2 identity/usage schema. Version-1 publication records
    are accepted only for promotion or inspection with final review and hashed
    log evidence; absent identity/budgets remain None, never current defaults.
    This validates saved data, not current Git/lock state or permission to promote.
    """
    if not isinstance(purpose, str) or purpose not in {'resume', 'promotion', 'inspect'}:
        raise CheckpointError('Unknown checkpoint recovery purpose')
    if (not isinstance(allowed_versions, (tuple, list, set, frozenset)) or not allowed_versions
            or any(type(version) is not int or version not in {1, 2} for version in allowed_versions)):
        raise CheckpointError('Unsupported allowed checkpoint versions')
    path = _path(path, 'checkpoint', argument=True)
    source = _path(expected_source, 'expected source', argument=True)
    directory = path.parent
    if (path.name != 'evolution.json' or directory.is_relative_to(source)
            or source.is_relative_to(directory) or not source.is_dir()):
        raise CheckpointError('Checkpoint cycle must be outside its source project')
    record = _object(path)
    version = record.get('version')
    if type(version) is not int or version not in allowed_versions or version not in {1, 2}:
        raise CheckpointError('Unsupported checkpoint version')
    if _path(record.get('source'), 'source') != source:
        raise CheckpointError('Checkpoint belongs to another source project')
    base = record.get('base')
    if not isinstance(base, dict):
        raise CheckpointError('Missing checkpoint base identity')
    _hash(base.get('commit'), 'base.commit', commit=True)
    ref = base.get('ref')
    if (not isinstance(ref, str) or not ref.startswith('refs/heads/') or ref.endswith(('/', '.'))
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '~^:?*[\\' for c in ref)
            or '..' in ref or '@{' in ref or any(not part or part.startswith('.') or part.endswith('.lock')
                                               for part in ref.split('/'))):
        raise CheckpointError('Invalid checkpoint source branch identity')
    phase = record.get('phase')
    if not isinstance(phase, str) or phase not in _PHASES:
        raise CheckpointError('Unknown checkpoint phase')
    if version == 2:
        if 'resume_phase' not in record or not isinstance(record.get('stop_reason'), str):
            raise CheckpointError('Missing versioned checkpoint cursor or stop reason')
        cursor = record['resume_phase']
        if (cursor is not None and (not isinstance(cursor, str) or cursor not in _ACTIVE | _COMPLETE)
                or phase not in _WRAPPERS and cursor != phase):
            raise CheckpointError('Invalid checkpoint resume phase')
    else:
        if purpose == 'resume' or phase not in _PROMOTION:
            raise CheckpointError('Legacy checkpoint supports evidenced promotion only')
        cursor = phase if phase not in _WRAPPERS else None
    locations = {}
    for key, name in (('workspace', 'workspace'), ('monitor_run', 'monitor'), ('validation_workspace', 'validation')):
        if key not in record and key != 'workspace':
            continue
        location = _path(record.get(key), key)
        if location != directory / name or (location.exists() and not location.is_dir()):
            raise CheckpointError('Checkpoint workspace boundary mismatch: ' + key)
        locations[key] = location
    for name in ('candidate', 'promoted_commit', 'previous_commit', 'rollback_commit'):
        if name in record:
            _hash(record[name], name, commit=True)
    if cursor in (_ACTIVE | _COMPLETE) - {'selected', 'baseline'} and 'monitor_run' not in locations:
        raise CheckpointError('Checkpoint phase is missing its monitor run')
    if cursor in {'validating', 'candidate', 'ready', 'promoting', 'activating', 'rolling_back', 'promoted', 'rolled_back'}:
        _hash(record.get('candidate'), 'candidate required by phase', commit=True)
    if cursor in {'rolling_back', 'rolled_back'}:
        _hash(record.get('rollback_commit'), 'rollback commit required by phase', commit=True)
    if 'promoted_commit' in record and record['promoted_commit'] != record.get('candidate'):
        raise CheckpointError('Promoted commit does not match the candidate')
    if 'previous_commit' in record and record['previous_commit'] != base['commit']:
        raise CheckpointError('Previous commit does not match the base')
    identity = record.get('config_identity')
    limits = _identity(identity) if identity is not None or version == 2 else None
    usage = _usage(record.get('usage', {}), limits=limits, required=version == 2)
    for name in ('agent_calls', 'elapsed_s'):
        if name in record:
            amount = _number(record[name], name, integer=name == 'agent_calls')
            usage[name] = max(usage.get(name, amount), amount)
        elif name not in usage:
            raise CheckpointError('Missing known checkpoint usage: ' + name)
    if version == 1 or purpose == 'promotion':
        if phase not in _PROMOTION:
            raise CheckpointError('Checkpoint is not in a promotion phase')
        _promotion_evidence(record, directory, locations['workspace'], locations.get('monitor_run'))
    return {'path': path, 'directory': directory, 'source': source, 'version': version,
            'phase': phase, 'resume_phase': cursor, 'purpose': purpose, 'record': record,
            'workspace': locations['workspace'], 'monitor_run': locations.get('monitor_run'),
            'validation_workspace': locations.get('validation_workspace'), 'config_identity': identity,
            'limits': limits, 'usage': usage, 'legacy': version == 1, 'budget_verified': limits is not None,
            'promotion_only': version == 1}
