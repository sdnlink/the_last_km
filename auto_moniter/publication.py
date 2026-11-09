"""Publish reviewed code and audit refs together, preserving unrelated staged work."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from auto_moniter.git_history import GitHistoryError
from auto_moniter.storage import write_json


def digest(data):
    return hashlib.sha256(data).hexdigest()


def index_path(history):
    value = Path(history.git('rev-parse', '--git-path', 'index').decode().strip())
    return value if value.is_absolute() else history.cwd / value


def entries(history, tree):
    result = {}
    for item in history.git('-C', str(history.root), 'ls-tree', '-r', '-z', tree).split(b'\0'):
        if item:
            mode, _, tail = item.split(b' ', 2)
            oid, path = tail.split(b'\t', 1)
            result[path] = mode + b' ' + oid + b'\t' + path + b'\0'
    return result


def business_tree(history, code_index):
    env = {'GIT_INDEX_FILE': str(code_index)}
    empty = history.git('hash-object', '-t', 'tree', '--stdin', data=b'').decode().strip()
    history.git('read-tree', history.business_head['commit'] or empty, env=env)
    original = entries(history, history.initial)
    candidate = entries(history, history.candidate['commit'])
    published_tree = history.last_business_commit.get('candidate_tree')
    published = entries(history, published_tree) if published_tree else {}
    scope = os.fsencode(history.cwd.relative_to(history.root).as_posix())
    runtime = b'.auto-moniter/' if scope == b'.' else scope + b'/.auto-moniter/'
    selected = {p for p in original.keys() | candidate.keys() | published.keys() if not p.startswith(runtime)}
    zero = b'0' * len(empty)
    changes = b''.join(candidate.get(p, b'0 ' + zero + b'\t' + p + b'\0') for p in sorted(selected))
    if changes:
        history.git('-C', str(history.root), 'update-index', '-z', '--index-info', data=changes, env=env)
    return history.git('write-tree', env=env).decode().strip()


def read_index(path):
    if path.is_symlink():
        raise GitHistoryError('Git index cannot be a symlink')
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(data) > 64 * 1024 * 1024:
        raise GitHistoryError('Git index exceeds publication limit')
    return data


def recover(history):
    """Reconcile an interrupted ref/index transaction before checking saved HEAD."""
    journal = history.run_dir / 'publication.json'
    if not journal.exists():
        return
    if journal.is_symlink() or journal.stat().st_size > 4 * 1024 * 1024:
        raise GitHistoryError('Invalid publication journal')
    saved = json.loads(journal.read_text())
    if saved.get('project') != str(history.cwd) or saved.get('audit_ref') != history.ref:
        raise GitHistoryError('Publication journal belongs to another task')
    business_ref = saved['before_business']['ref']
    if not business_ref.startswith('refs/heads/'):
        raise GitHistoryError('Invalid publication business ref')
    actual = history._business_identity()
    audit = history.git('rev-parse', '--verify', history.ref, check=False).decode().strip()
    if saved.get('phase') == 'preparing':
        path = index_path(history)
        lock = path.with_name(path.name + '.lock')
        if actual != saved['before_business'] or audit != saved['before_audit']:
            raise GitHistoryError('Refs changed during interrupted publication preparation')
        current = read_index(path)
        if (digest(current) if current is not None else None) != saved['old_index_hash']:
            raise GitHistoryError('Index changed during publication preparation')
        if lock.exists():
            stat = lock.lstat()
            if lock.is_symlink() or [stat.st_dev,stat.st_ino] != saved['lock_identity'] or digest(lock.read_bytes()) != saved['lock_hash']:
                raise GitHistoryError('Another Git operation owns the index lock')
            lock.unlink()
        journal.unlink()
        return
    before = (saved['before_business'], saved['before_audit'])
    after = (saved['after']['business_head'], saved['after']['head'])
    if (actual, audit) not in (before, after):
        raise GitHistoryError('Publication refs changed externally; refusing recovery')
    path = index_path(history)
    lock = path.with_name(path.name + '.lock')
    current = read_index(path)
    current_hash = digest(current) if current is not None else None
    if lock.exists() or lock.is_symlink():
        if lock.is_symlink():
            raise GitHistoryError('Unsafe publication index lock')
        stat = lock.stat()
        if [stat.st_dev, stat.st_ino] != saved['lock_identity']:
            raise GitHistoryError('Another Git operation owns the index lock')
        retained = read_index(lock)
        if digest(retained) not in (saved['lock_hash'], saved['next_index_hash']):
            replacement = read_index(history.run_dir / 'publication-index.next')
            if ((actual, audit) != after or replacement is None
                    or digest(replacement) != saved['next_index_hash'] or not replacement.startswith(retained)):
                raise GitHistoryError('Publication index lock changed')
    if (actual, audit) == before:
        if current_hash != saved['old_index_hash']:
            raise GitHistoryError('Index changed before interrupted publication; refusing recovery')
        lock.unlink(missing_ok=True)
    else:
        if current_hash not in (saved['old_index_hash'], saved['next_index_hash']):
            raise GitHistoryError('User staging changed after interrupted publication; preserve it and inspect journal')
        if current_hash != saved['next_index_hash']:
            data = read_index(history.run_dir / 'publication-index.next')
            if data is None or digest(data) != saved['next_index_hash']:
                raise GitHistoryError('Publication recovery index is corrupt')
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            if not lock.exists():
                descriptor = os.open(lock, flags, 0o600)
            else:
                descriptor = os.open(lock, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            lock.replace(path)
        else:
            lock.unlink(missing_ok=True)
        write_json(history.state_path, saved['after'], durable=True)
    journal.unlink()
    (history.run_dir / 'publication-index.next').unlink(missing_ok=True)


def publish(history, index, worker_data, reviewer_data, worker_text, reviewer_text):
    history.verify()
    previous = history.last_business_commit
    if (previous.get('round') == index and previous.get('candidate_tree') == history.candidate['commit']
            and previous.get('commit') == history.business_head['commit']):
        return history.last_worker, history.last_record
    before_business = dict(history.business_head)
    if not before_business['ref'].startswith('refs/heads/'):
        raise GitHistoryError('Business publication requires a checked-out local branch')
    path = index_path(history)
    lock = path.with_name(path.name + '.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    marker = ('auto-moniter publication ' + history.key).encode()
    os.write(descriptor, marker)
    os.fsync(descriptor)
    stat = os.fstat(descriptor)
    old_state = history.export()
    journal = history.run_dir / 'publication.json'
    prepared = False
    try:
        old_index = read_index(path)
        write_json(journal, {'phase':'preparing', 'project':str(history.cwd), 'audit_ref':history.ref,
            'before_business':before_business, 'before_audit':history.head,
            'old_index_hash':digest(old_index) if old_index is not None else None,
            'lock_hash':digest(marker), 'lock_identity':[stat.st_dev,stat.st_ino]}, durable=True)
        with tempfile.TemporaryDirectory(prefix='auto-moniter-publish-') as directory:
            directory = Path(directory)
            empty = history.git('hash-object', '-t', 'tree', '--stdin', data=b'').decode().strip()
            zero = b'0' * len(empty)
            parent = before_business['commit']
            code_tree = business_tree(history, directory / 'code-index')
            if history.candidate.get('business_tree') and code_tree != history.candidate['business_tree']:
                raise GitHistoryError('Business tree differs from the reviewed publication plan')
            parent_tree = history.git('rev-parse', parent + '^{tree}').decode().strip() if parent else empty
            changed = set(filter(None, history.git('diff', '--name-only', '-z', parent_tree, code_tree).split(b'\0')))
            synced = directory / 'sync-index'
            sync_env = {'GIT_INDEX_FILE': str(synced)}
            if old_index is not None:
                synced.write_bytes(old_index)
            else:
                history.git('read-tree', empty, env=sync_env)
            staged = set(filter(None, history.git('diff', '--cached', '--name-only', '-z', parent or empty,
                                                  env=sync_env).split(b'\0')))
            # A human's pre-existing staged changes are never silently folded into
            # or replaced by an automated publication.
            if staged & changed:
                raise GitHistoryError('Reviewed paths overlap existing staged changes; preserve staging and resolve before publication')
            code_entries = entries(history, code_tree)
            updates = b''.join(code_entries.get(p, b'0 ' + zero + b'\t' + p + b'\0') for p in sorted(changed))
            if updates:
                history.git('-C', str(history.root), 'update-index', '-z', '--index-info', data=updates, env=sync_env)
            next_index = synced.read_bytes()
            commit = parent
            if code_tree != parent_tree:
                commit = history.git('commit-tree', code_tree, *(['-p', parent] if parent else []),
                    data=(f'Implement reviewed task round {index}\n\n' + worker_data['summary'] + '\n').encode()).decode().strip()
            business = {'ref': before_business['ref'], 'commit': commit, 'parent': parent,
                        'round': index, 'created': commit != parent,
                        'candidate_tree': history.candidate['commit'],
                        'changed_files': [os.fsdecode(p) for p in sorted(changed)]}
            before_audit = history.head
            history._defer_refs = True
            history._ref_before = before_audit
            history.last_business_commit = business
            first = history.record(index, 'worker', {**worker_data, 'business_commit': business},
                                   before=history.candidate['base'], raw=worker_text)
            second = history.record(index, 'reviewer', {**reviewer_data, 'business_commit': business},
                                    before=first['commit'], raw=reviewer_text)
            first['business_commit'] = second['business_commit'] = business
            history.last_worker, history.last_record = first, second
            history.business_head = {'ref': business['ref'], 'commit': business['commit']}
            after = history.export()
            with (history.run_dir / 'publication-index.next').open('wb') as stream:
                stream.write(next_index)
                stream.flush()
                os.fsync(stream.fileno())
            write_json(journal, {'project': str(history.cwd), 'audit_ref': history.ref,
                'before_business': before_business, 'before_audit': before_audit, 'after': after,
                'old_index_hash': digest(old_index) if old_index is not None else None,
                'next_index_hash': digest(next_index), 'lock_hash': digest(marker),
                'lock_identity': [stat.st_dev, stat.st_ino]}, durable=True)
            prepared = True
            # A single --stdin batch is one ref transaction, including on Git
            # 2.20 which predates explicit start/prepare/commit commands.
            transaction = (f'update {business["ref"]} {commit} {parent or "0" * len(empty)}\n'
                           + f'update {history.ref} {history.head} {before_audit or "0" * len(empty)}\n')
            history.git('update-ref', '--stdin', data=transaction.encode())
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            with os.fdopen(os.dup(descriptor), 'wb') as handle:
                handle.write(next_index)
                handle.flush()
                os.fsync(handle.fileno())
            lock.replace(path)
            write_json(history.state_path, after, durable=True)
            journal.unlink()
            (history.run_dir / 'publication-index.next').unlink()
            return first, second
    except BaseException:
        if prepared:
            history._defer_refs = False
            try:
                recover(history)
                restored = json.loads(history.state_path.read_text())
                history.head, history.initial = restored['head'], restored['initial']
                history.last_worker, history.last_record = restored['last_worker'], restored['last_record']
                history.business_head = restored['business_head']
                history.last_business_commit = restored.get('last_business_commit', {})
                history.audit_initial = restored.get('audit_initial', '')
            except (OSError, ValueError, GitHistoryError):
                # Keep the durable journal for the next locked recovery. Report
                # the last persisted state rather than an uncommitted draft.
                history.head, history.initial = old_state['head'], old_state['initial']
                history.business_head = old_state['business_head']
        else:
            history.head, history.initial = old_state['head'], old_state['initial']
            history.last_worker, history.last_record = old_state['last_worker'], old_state['last_record']
            history.business_head = old_state['business_head']
            history.last_business_commit = old_state.get('last_business_commit', {})
            history.audit_initial = old_state.get('audit_initial', '')
            lock.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
        raise
    finally:
        history._defer_refs = False
        os.close(descriptor)
