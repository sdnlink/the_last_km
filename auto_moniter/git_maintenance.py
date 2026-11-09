"""Explicit, locked migration and bounded archival of task Git audit references."""
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from datetime import datetime, timezone

from auto_moniter.git_history import GitHistory, GitHistoryError
from auto_moniter.locking import workspace_lease
from auto_moniter.paths import project_runs_dir
from auto_moniter.retention import _FLAGS, _locks, _json_at, _check_mounts, _root_path
from auto_moniter.storage import write_json

FINAL = {'done', 'failed', 'stalled', 'max_rounds'}


def handle(cwd):
    value = object.__new__(GitHistory)
    value.cwd = Path(cwd).expanduser().resolve()
    value.root = Path(value.git('rev-parse', '--show-toplevel').decode().strip()).resolve()
    return value


def archive_path(git, key):
    common = Path(git.git('rev-parse', '--git-common-dir').decode().strip())
    common = common if common.is_absolute() else git.cwd / common
    directory = common / 'auto-moniter-archives'
    if directory.is_symlink():
        raise GitHistoryError('Archive directory cannot be a symlink')
    return directory / (key + '.bundle')


def replace_ref(value, old, new):
    if isinstance(value, dict):
        return {k: replace_ref(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_ref(v, old, new) for v in value]
    return new if value == old else value


def documents(run):
    result = {}
    for name in ('git-history.json', 'state.json', 'report.json'):
        path = run / name
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Unsafe task checkpoint')
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('Invalid task checkpoint')
            result[name] = value
    return result


def save_documents(run, docs):
    for name, value in docs.items():
        stamp = (run/name).stat() if name == 'report.json' and (run/name).exists() else None
        write_json(run / name, value, durable=True)
        if stamp:
            os.utime(run/name, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def checked_out(git, ref):
    return ('branch ' + ref) in git.git('worktree', 'list', '--porcelain').decode().splitlines()


def ref_value(git, ref):
    return git.git('rev-parse', '--verify', ref, check=False).decode().strip()


def migrate(git, run, docs, key):
    old, new = 'refs/heads/auto-moniter/' + key, 'refs/auto-moniter/' + key
    head = docs['git-history.json']['head']
    source, target = ref_value(git, old), ref_value(git, new)
    if checked_out(git, old):
        raise ValueError('Legacy audit branch is checked out; switch that worktree before migration')
    if source and source != head or target and target != head:
        raise ValueError('Audit ref changed or migration target conflicts')
    if head and not (source or target):
        raise ValueError('Saved audit commit has no matching ref')
    if head and not target:
        git.git('update-ref', new, head, '0' * len(head))
    # Keep the legacy ref until ALL mutable checkpoints are durable. An
    # interrupted migration can be repeated; either reference still pins history.
    save_documents(run, {name: replace_ref(value, old, new) for name, value in docs.items()})
    if source:
        git.git('update-ref', '-d', old, source)


def archive(git, run, docs, key):
    history = docs['git-history.json']
    if (history.get('project') != str(git.cwd)
            or hashlib.sha256(os.fsencode(run.resolve())).hexdigest()[:20] != key
            or history.get('ref') not in ('refs/auto-moniter/'+key, 'refs/heads/auto-moniter/'+key)):
        raise ValueError('Task history does not match archive identity')
    if history['ref'].startswith('refs/heads/'):
        migrate(git, run, docs, key)
        docs = documents(run)
        history = docs['git-history.json']
    ref, head = history['ref'], history['head']
    if not head:
        return
    if ref_value(git, ref) != head or checked_out(git, ref):
        raise ValueError('Audit ref changed or is checked out')
    target = archive_path(git, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.bundle.tmp')
    if target.is_symlink() or temporary.is_symlink():
        raise ValueError('Unsafe archive path')
    if not target.exists():
        git.git('bundle', 'create', str(temporary), ref)
        git.git('bundle', 'verify', str(temporary))
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        temporary.replace(target)
    if not any(line.split()[:2] == [head, ref] for line in
               git.git('bundle', 'list-heads', str(target)).decode().splitlines()):
        raise ValueError('Existing archive does not match task history')
    receipt = {'ref': ref, 'head': head, 'path': str(target),
               'at': datetime.now(timezone.utc).isoformat()}
    write_json(target.with_suffix('.json'), {'project': str(git.cwd), 'run_dir': str(run),
               'status': docs['state.json']['status'], 'history': history, 'archive': receipt}, durable=True)
    for name, value in docs.items():
        saved = value if name == 'git-history.json' else value.get('git_history')
        if isinstance(saved, dict):
            saved['archived'] = receipt
    save_documents(run, docs)
    transaction = f'delete {ref} {head}\n'
    work_ref = history.get('work_ref')
    if work_ref:
        if work_ref != 'refs/auto-moniter-work/' + key:
            raise ValueError('Invalid task work ref')
        current_work = ref_value(git, work_ref)
        if current_work:
            if current_work != history.get('work_head'):
                raise ValueError('Task work ref changed before archival')
            transaction += f'delete {work_ref} {current_work}\n'
    git.git('update-ref', '--stdin', data=transaction.encode())
    # Only remove monitor-owned flat record files. No project or arbitrary
    # nested directory is eligible for deletion here.
    records = git.cwd / '.auto-moniter/history' / key
    if not records.is_symlink() and records.is_dir():
        _check_mounts(records)
        for path in records.iterdir():
            if re.fullmatch(r'\d+-(worker|reviewer)\.json', path.name) and path.is_file() and not path.is_symlink():
                path.unlink()
        if not any(records.iterdir()):
            records.rmdir()


def maintenance(cwd, *, runs_dirs=None, operation='list', apply=False, older_than=7*86400,
                keep_runs=5, include_failed=False, keep_archives=10, archive_older_than=90*86400):
    git = handle(cwd)
    roots = runs_dirs or [project_runs_dir(git.cwd), git.cwd/'.auto-moniter/runs', git.cwd/'.auto-moniter/console/runs']
    if operation not in {'list', 'migrate', 'cleanup'}:
        raise ValueError('Invalid Git history operation')
    if min(older_than, keep_runs, keep_archives, archive_older_than) < 0:
        raise ValueError('Invalid history retention policy')
    result = {'project': str(git.cwd), 'operation': operation, 'applied': apply, 'runs': [], 'errors': []}
    rows, seen = [], set()
    for root in roots:
        root = _root_path(git.cwd, Path(root))
        if not root.exists():
            continue
        for run in sorted(root.iterdir()):
            if not run.is_dir() or run.is_symlink() or run.resolve() in seen or not (run/'git-history.json').exists():
                continue
            seen.add(run.resolve())
            row = {'run_dir': str(run), 'selected': False, 'reason': 'invalid'}
            result['runs'].append(row)
            try:
                directory = os.open(run, _FLAGS | os.O_DIRECTORY)
                try:
                    with _locks(directory):
                        docs = documents(run)
                        state, history = docs['state.json'], docs['git-history.json']
                        key = hashlib.sha256(os.fsencode(run.resolve())).hexdigest()[:20]
                        if (Path(state['cwd']).resolve() != git.cwd or history.get('project') != str(git.cwd)
                                or history.get('ref') not in ('refs/auto-moniter/'+key, 'refs/heads/auto-moniter/'+key)):
                            raise ValueError('Task does not match project or run identity')
                        if (run/'process.json').exists() or (run/'publication.json').exists():
                            raise ValueError('Process or publication recovery is pending')
                        archived = history.get('archived')
                        row.update(status=state['status'], ref=history['ref'], head=history['head'], archived=bool(archived))
                        report = docs.get('report.json', {})
                        final = state['status'] in FINAL and report.get('status') == state['status']
                        finished = (run/'report.json').stat().st_mtime if final else None
                        row.update(reason='recoverable_run_preserved', finished_at=finished)
                        if operation == 'migrate' and archived and not ref_value(git, history['ref']):
                            row['reason'] = 'archived_preserved'
                        elif operation == 'migrate':
                            old = 'refs/heads/auto-moniter/'+key
                            row['selected'] = history['ref'] == old or bool(ref_value(git, old))
                            row['reason'] = 'migration_ready' if row['selected'] else 'already_hidden'
                        elif final:
                            row['reason'] = 'eligible' if state['status']=='done' or include_failed else 'failed_run_preserved'
                        rows.append((row, run, key, docs))
                finally:
                    os.close(directory)
            except (OSError, ValueError, GitHistoryError) as exc:
                row['reason'] = str(exc)
    final_rows = sorted((r for r in rows if r[0]['finished_at'] is not None),
                        key=lambda r: (r[0]['finished_at'], str(r[1])), reverse=True)
    now = time.time()
    if operation == 'cleanup':
        for position, (row, run, key, docs) in enumerate(final_rows):
            if row['reason'] != 'eligible':
                continue
            if row['archived']:
                if ref_value(git, docs['git-history.json']['ref']):
                    row.update(selected=True, reason='finish_interrupted_archive')
                continue
            if position < keep_runs:
                row['reason'] = 'keep_recent'
            elif now - row['finished_at'] < older_than:
                row['reason'] = 'too_recent'
            else:
                row.update(selected=True, reason='archive_ready')
    if apply:
        with workspace_lease(git.cwd):
            for row, run, key, original in rows:
                if not row['selected']:
                    continue
                try:
                    directory = os.open(run, _FLAGS | os.O_DIRECTORY)
                    try:
                        with _locks(directory):
                            docs = documents(run)
                            if docs != original or (run/'process.json').exists() or (run/'publication.json').exists():
                                raise ValueError('Task changed since preview')
                            if operation == 'migrate':
                                migrate(git, run, docs, key)
                                row['reason'] = 'migrated'
                            elif operation == 'cleanup':
                                archive(git, run, docs, key)
                                row['reason'] = 'archived'
                    finally:
                        os.close(directory)
                except (OSError, ValueError, GitHistoryError) as exc:
                    row['reason'] = 'not_applied: ' + str(exc)
                    result['errors'].append({'run_dir': str(run), 'error': str(exc)})
            result['archives'] = expire_archives(git, keep_archives, archive_older_than,
                                                 apply=operation == 'cleanup', errors=result['errors'])
    else:
        result['archives'] = expire_archives(git, keep_archives, archive_older_than,
                                             apply=False, errors=result['errors'])
    return result


def expire_archives(git, keep, age, *, apply, errors):
    directory = archive_path(git, '0'*20).parent
    if not directory.is_dir():
        return []
    rows = []
    for bundle in directory.glob('*.bundle'):
        if bundle.is_symlink() or not re.fullmatch(r'[0-9a-f]{20}\.bundle', bundle.name):
            continue
        manifest_path = bundle.with_suffix('.json')
        if not manifest_path.is_file() or manifest_path.is_symlink() or manifest_path.stat().st_size > 4*1024*1024:
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get('run_dir'), str):
            continue
        run = Path(manifest['run_dir'])
        archived = manifest.get('archive')
        if (manifest.get('project') != str(git.cwd) or not run.is_absolute()
                or hashlib.sha256(os.fsencode(run.resolve())).hexdigest()[:20] != bundle.stem
                or manifest.get('status') not in FINAL or not isinstance(archived, dict)
                or archived.get('ref') not in ('refs/auto-moniter/'+bundle.stem, 'refs/heads/auto-moniter/'+bundle.stem)):
            continue
        info = bundle.stat()
        rows.append({'path':str(bundle), 'run_dir':str(run), 'ref':archived['ref'],
                     'at':info.st_mtime, 'identity':[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
                     'selected':False, 'reason':'keep_recent'})
    rows.sort(key=lambda r:r['at'], reverse=True)
    for position, row in enumerate(rows):
        if ref_value(git, row['ref']):
            row['reason'] = 'live_ref_preserved'
            continue
        if position < keep:
            continue
        if time.time() - row['at'] < age:
            row['reason'] = 'too_recent'
            continue
        row.update(selected=True, reason='archive_expiry_ready')
        if not apply:
            continue
        try:
            from contextlib import ExitStack
            with ExitStack() as stack:
                run = Path(row['run_dir'])
                if run.is_symlink():
                    raise ValueError('Unsafe archived task directory')
                if run.exists():
                    fd = os.open(run, _FLAGS | os.O_DIRECTORY)
                    stack.callback(os.close, fd)
                    stack.enter_context(_locks(fd))
                    state = _json_at(fd, 'state.json')
                    if state.get('status') not in FINAL or (run/'process.json').exists() or (run/'publication.json').exists():
                        raise ValueError('Archived task is active or recoverable')
                if ref_value(git, row['ref']):
                    raise ValueError('Archive was restored; live ref retained')
                bundle = Path(row['path'])
                info = bundle.lstat()
                if bundle.is_symlink() or [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns] != row['identity']:
                    raise ValueError('Archive changed since preview')
                bundle.unlink()
                row['reason'] = 'archive_expired'
        except (OSError, ValueError, GitHistoryError) as exc:
            row['reason'] = 'not_expired: ' + str(exc)
            errors.append({'archive':row['path'],'error':str(exc)})
    return rows


def restore(cwd, run):
    git = handle(cwd)
    run = Path(run).expanduser().absolute()
    for path in (run, *run.parents):
        if path.is_symlink():
            raise ValueError('Unsafe task directory')
    run = run.resolve()
    key = hashlib.sha256(os.fsencode(run)).hexdigest()[:20]
    target = archive_path(git, key)
    with workspace_lease(git.cwd):
        from contextlib import ExitStack
        with ExitStack() as stack:
            if run.is_dir():
                directory = os.open(run, _FLAGS | os.O_DIRECTORY)
                stack.callback(os.close, directory)
                stack.enter_context(_locks(directory))
                docs = documents(run)
                history = docs['git-history.json']
                if (run/'process.json').exists() or (run/'publication.json').exists():
                    raise ValueError('Task recovery is pending')
            else:
                manifest_path = target.with_suffix('.json')
                if not manifest_path.is_file() or manifest_path.is_symlink():
                    raise ValueError('Archive metadata is absent')
                manifest = json.loads(manifest_path.read_text())
                if manifest.get('project') != str(git.cwd) or manifest.get('run_dir') != str(run):
                    raise ValueError('Archive metadata does not match task')
                docs, history = {}, manifest['history']
            if history.get('project') != str(git.cwd) or history.get('ref') not in ('refs/auto-moniter/'+key, 'refs/heads/auto-moniter/'+key):
                raise ValueError('Archive does not belong to this project and task')
            ref, head = history['ref'], history['head']
            if not target.is_file() or target.is_symlink():
                raise ValueError('History archive is absent or expired')
            if not any(line.split()[:2] == [head, ref] for line in git.git('bundle', 'list-heads', str(target)).decode().splitlines()):
                raise ValueError('Archive does not match task')
            actual = ref_value(git, ref)
            if actual and actual != head:
                raise ValueError('Ref has changed; refusing to overwrite it')
            git.git('bundle', 'unbundle', str(target))
            if not actual:
                git.git('update-ref', ref, head, '0'*len(head))
            for name, value in docs.items():
                saved = value if name=='git-history.json' else value.get('git_history')
                if isinstance(saved, dict):
                    saved.pop('archived', None)
            save_documents(run, docs)
            return {'run_dir': str(run), 'ref': ref, 'head': head, 'restored': True,
                    'run_records_present': bool(docs)}
