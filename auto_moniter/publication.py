"""Publish reviewed code and audit refs together, preserving unrelated staged work."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from auto_moniter.git_history import GitHistoryError
from auto_moniter.redaction import redact
from auto_moniter.storage import write_json
from auto_moniter.workspace import _ignored


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
    committed = entries(history, history.business_head['commit']) if history.business_head['commit'] else {}
    selected = set()
    for name in original.keys() | candidate.keys() | published.keys() | committed.keys():
        path = history.root / os.fsdecode(name)
        try:
            path.relative_to(history.cwd)
        except ValueError:
            continue
        # HEAD also contains paths deleted before this task's initial snapshot.
        # Apply the same source scope as review; preserve siblings and outputs.
        if not _ignored(path, history.run_dir, cwd=history.cwd):
            selected.add(name)
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


def remove_owned_lock(path, identity):
    """A failed transaction cannot remove a lock substituted by another owner."""
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
        path.unlink()


def acquire_index_lock(path, marker):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    identity = None
    try:
        identity = os.fstat(descriptor)
        offset = 0
        while offset < len(marker):
            written = os.write(descriptor, marker[offset:])
            if written <= 0:
                raise OSError('Cannot write publication index-lock marker')
            offset += written
        os.fsync(descriptor)
        return descriptor, identity
    except BaseException:
        try:
            remove_owned_lock(path, identity)
        finally:
            os.close(descriptor)
        raise


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


class PublicationRefused(GitHistoryError):
    """Stop or budget refused a new publication before refs moved."""


class StagedChangesConflict(GitHistoryError):
    """Publication needs a staging decision; keep the candidate resumable."""


def check_unborn_staging(history):
    """Detect an unavoidable first-publication conflict before any model calls."""
    if not history.business_commits or history.business_head['commit']:
        return
    empty = history.git('hash-object', '-t', 'tree', '--stdin', data=b'').decode().strip()
    staged = set(filter(None, history.git('diff', '--cached', '--name-only', '-z', empty).split(b'\0')))
    overlap = sorted(staged & entries(history, history.initial).keys())
    if overlap:
        paths = ', '.join(os.fsdecode(path) for path in overlap[:10])
        raise StagedChangesConflict(
            f'Business branch has no initial commit and {len(overlap)} staged source paths overlap '
            f'the publication baseline: {paths}. Preserve staging, commit the existing input baseline '
            'separately, then resume this run; do not add the pending worker files to that baseline.')


def _reviewed_checks(receipt, index):
    """Describe the receipt supplied after Watcher's candidate and approval gates."""
    if not receipt:
        return ['Check receipt unavailable.']
    commands = receipt['context']['commands']
    if not commands:
        return ['No checks configured for this candidate.']

    if 'reused_from' in receipt:
        reused = receipt['reused_from']
        source = reused.get('round') if isinstance(reused, dict) else None
        origin = (f'Reused from round {source}.' if type(source) is int and 0 < source < index
                  else 'Reused checks. Source round unavailable.')
    else:
        origin = f'Executed for round {index}.'
    results = [origin]
    for command, check in zip(commands, receipt['checks']):
        details = check.get('status') or 'Result detail unavailable'
        if type(check.get('exit_code')) is int:
            details += f' (exit {check["exit_code"]})'
        command = redact(command).replace('\n', r'\n').replace('\r', r'\r')
        results.append(f'- {command} — {details}')
    return results


def _portable_commit_text(text, cwd=None):
    """Normalize local paths for Git display only; never change executable evidence."""
    # Match URLs first so their paths, including query values, remain intact.
    tokens = re.compile(
        r'(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"\x27]+)|'
        r'(?P<quoted>[\"\x27](?:/|[A-Za-z]:[\\/])[^\n\r]*?[\"\x27])|'
        r'(?<![A-Za-z0-9_./\\])(?P<path>(?:/|[A-Za-z]:[\\/])'
        r'[^\s<>\"\x27`,;:)\]}\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+)')

    def display(match):
        if match.group('url'):
            # file URLs name local files rather than portable remote resources.
            return '<local-path>' if match.group().lower().startswith('file://') else match.group()
        value = match.group()
        quote = value[0] if match.group('quoted') else ''
        path = value[1:-1] if quote else value
        suffix = ''
        while path.endswith('.'):
            suffix = '.' + suffix
            path = path[:-1]
        # Tool names are stable even when Python/Node live in a user installation.
        name = path.replace('\\', '/').rsplit('/', 1)[-1]
        if re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', name):
            shown = 'python'
        elif name == 'node':
            shown = 'node'
        elif cwd and path.startswith(str(cwd).rstrip('/') + '/'):
            relative = path[len(str(cwd).rstrip('/')) + 1:]
            shown = relative if '..' not in relative.split('/') else '<local-path>'
        else:
            shown = '<local-path>'
        return quote + shown + suffix + quote

    return tokens.sub(display, redact(text))


def _business_commit_message(index, previous_round, worker, reviewer, changed, problem, receipt,
                             *, previous_requirement_round=0, cwd=None):
    task = (receipt or {}).get('context', {}).get('task') or {}
    if reviewer.get('task_status') == 'accepted' and task.get('delivery_scope') == 'function':
        scope = 'function'
    elif worker.get('scope') == reviewer.get('scope') == 'requirement':
        scope = 'requirement'
    elif 'module' in (worker.get('scope'), reviewer.get('scope')):
        scope = 'module'
    else:
        scope = 'scope unavailable'
    modules = sorted(set(worker.get('module_ids', [])) & set(reviewer.get('module_ids', [])))
    if scope == 'function' and task.get('module_id'):
        modules = [task['module_id']]

    changes = worker.get('done', [])
    subject = ''
    for text in [worker.get('summary', ''), *changes]:
        text = _portable_commit_text(text, cwd).strip()
        if not text:
            continue
        line = ' '.join(text.splitlines()[0].split())
        normalized = line.casefold().rstrip('.。')
        if (normalized in {'done', 'complete', 'completed', 'worker', '已完成', '完成'}
                or re.fullmatch(r'implement reviewed task round [0-9]+', normalized)):
            continue
        subject = line
        break
    paths = ', '.join(os.fsdecode(path) for path in sorted(changed))
    subject = subject or 'Update ' + ' '.join(redact(paths).split())
    if len(subject) > 100:
        subject = subject[:99].rstrip() + '…'

    # Reconfirmation updates the closure round and tree. Require a finding
    # reported in this requirement cycle, within the approved module scope.
    module_ids = set(modules)
    issues = [item for item in reviewer.get('audit_issues', {}).get('items', [])
              if item['status'] == 'resolved' and previous_round < item['resolved_round'] <= index
              and item['last_seen_round'] > previous_requirement_round
              and item['tree'] == reviewer.get('tree')
              and (not item['module_ids'] or module_ids.intersection(item['module_ids']))]
    parts = [subject, '', 'Problem', problem or 'No separate problem description recorded for this delivery.']
    parts.extend(f'- {item["id"]} — {item["summary"]}' for item in issues)
    parts.extend(['', 'Behavior change', *[f'- {change}' for change in (changes or [subject])],
                  f'Changed paths: {paths}', '', 'Independent review',
                  f'Approved ({scope}) in round {index}.'])
    if modules:
        parts.append('Modules: ' + ', '.join(modules))
    if reviewer.get('summary'):
        parts.append(reviewer['summary'])
    parts.extend(f'- {item["id"]} resolved — {item["resolution"]["summary"]}' for item in issues)
    parts.extend(['', 'Watcher checks', *_reviewed_checks(receipt, index)])
    return _portable_commit_text('\n'.join(parts), cwd).replace('\0', '').encode('utf-8', errors='replace') + b'\n'


def publish(history, index, worker_data, reviewer_data, worker_text, reviewer_text, boundary=None, *,
            problem='', receipt=None, previous_requirement_round=0):
    history.verify()
    previous = history.last_business_commit
    if (previous.get('round') == index and previous.get('candidate_tree') == history.candidate['commit']
            and previous.get('commit') == history.business_head['commit']):
        return history.last_worker, history.last_record
    if boundary and boundary() is False:
        raise PublicationRefused('Publication refused before preparation: watcher stop or budget is active')
    before_business = dict(history.business_head)
    if not before_business['ref'].startswith('refs/heads/'):
        raise GitHistoryError('Business publication requires a checked-out local branch')
    path = index_path(history)
    lock = path.with_name(path.name + '.lock')
    marker = ('auto-moniter publication ' + history.key).encode()
    old_state = history.export()
    journal = history.run_dir / 'publication.json'
    prepared = False
    descriptor, stat = acquire_index_lock(lock, marker)
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
                raise StagedChangesConflict('Reviewed paths overlap existing staged changes; preserve staging and resolve before publication')
            code_entries = entries(history, code_tree)
            updates = b''.join(code_entries.get(p, b'0 ' + zero + b'\t' + p + b'\0') for p in sorted(changed))
            if updates:
                history.git('-C', str(history.root), 'update-index', '-z', '--index-info', data=updates, env=sync_env)
            next_index = synced.read_bytes()
            # Stop during tree and index preparation, before commit objects or
            # deferred audit records are created.
            if boundary and boundary() is False:
                raise PublicationRefused('Publication refused during preparation: watcher stop or budget is active')
            commit = parent
            if code_tree != parent_tree:
                message = _business_commit_message(
                    index, previous.get('round', 0), worker_data, reviewer_data, changed, problem, receipt,
                    previous_requirement_round=previous_requirement_round, cwd=history.cwd)
                commit = history.git('commit-tree', code_tree, *(['-p', parent] if parent else []),
                                     data=message).decode().strip()
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
            # Recheck immediately before the irreversible ref transaction. A stop
            # or exhausted budget here must not advance main or the audit ref.
            # Preparation remains recoverable through the existing journal.
            if boundary and boundary() is False:
                raise PublicationRefused('Publication refused before ref transaction: watcher stop or budget is active')
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
            remove_owned_lock(lock, stat)
            journal.unlink(missing_ok=True)
        raise
    finally:
        history._defer_refs = False
        os.close(descriptor)
