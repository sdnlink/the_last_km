"""Durable pairing of a pending source ref and its task metadata."""
import json

from auto_moniter.git_history import GitHistoryError
from auto_moniter.storage import write_json


LIMIT = 4 * 1024 * 1024


def read(path):
    if path.is_symlink() or path.stat().st_size > LIMIT:
        raise GitHistoryError('Invalid task work checkpoint')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise GitHistoryError('Invalid task work checkpoint')
    return value


def persist(history, previous):
    journal = history.run_dir / 'work-checkpoint.json'
    if journal.exists() or journal.is_symlink():
        raise GitHistoryError('Pending task work checkpoint requires recovery')
    before = read(history.state_path) if history.state_path.exists() else None
    value = {'version': 1, 'project': str(history.cwd), 'ref': history.work_ref,
             'before': previous, 'after': history.work_head, 'before_state': before,
             'after_state': history.export()}
    write_json(journal, value, durable=True)
    history.git('update-ref', history.work_ref, history.work_head,
                previous or '0' * len(history.work_head))
    history._save()
    journal.unlink()


def recover(history):
    journal = history.run_dir / 'work-checkpoint.json'
    if not journal.exists() and not journal.is_symlink():
        return
    value = read(journal)
    state, previous = value.get('after_state'), value.get('before_state')
    ref = 'refs/auto-moniter-work/' + history.key
    if (value.get('version') != 1 or value.get('project') != str(history.cwd) or value.get('ref') != ref
            or not isinstance(state, dict) or not (previous is None or isinstance(previous, dict))
            or state.get('project') != str(history.cwd) or state.get('work_ref') != ref
            or state.get('work_head') != value.get('after')
            or state.get('ref') not in ('refs/auto-moniter/' + history.key, 'refs/heads/auto-moniter/' + history.key)
            or history._business_identity() != state.get('business_head')):
        raise GitHistoryError('Task work checkpoint identity does not match')
    current = read(history.state_path) if history.state_path.exists() else None
    if current != previous and current != state:
        raise GitHistoryError('Task history changed during interrupted work checkpoint')
    if previous is not None and previous.get('work_head', '') != value.get('before'):
        raise GitHistoryError('Task work checkpoint predecessor does not match')
    after = value.get('after')
    if not isinstance(after, str) or not isinstance(value.get('before'), str):
        raise GitHistoryError('Invalid task work checkpoint refs')
    tree = history.git('rev-parse', after + '^{tree}').decode().strip()
    expected = state.get('candidate', {}).get('commit') or state.get('initial')
    if tree != expected:
        raise GitHistoryError('Task work checkpoint does not pin the saved candidate')
    if value['before']:
        parents = history.git('rev-list', '--parents', '-n', '1', after).decode().split()
        if parents[1:] != [value['before']]:
            raise GitHistoryError('Task work checkpoint is not a direct continuation')
    actual = history.git('rev-parse', '--verify', ref, check=False).decode().strip()
    if actual not in (value['before'], after):
        raise GitHistoryError('Task work ref changed externally; refusing checkpoint recovery')
    if actual != after:
        if current == state:
            raise GitHistoryError('Task work ref was rolled back after its checkpoint')
        history.git('update-ref', ref, after, value['before'] or '0' * len(after))
    write_json(history.state_path, state, durable=True)
    journal.unlink()
