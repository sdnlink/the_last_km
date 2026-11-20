"""Recognize a new business input without reusing approval of the previous HEAD."""
import re

from auto_moniter.git_history import GitHistoryError


class BusinessHeadChanged(GitHistoryError):
    pass


def _has_rewritten_checkpoint(git, before, after):
    """Find the same ordered history of code trees on the new branch's mainline."""
    if git('rev-parse', '--is-shallow-repository', check=False).strip() != b'false':
        return False
    # Bound proof work for large or unrelated histories. Shared commit IDs
    # anchor the comparison immediately, even if their predecessors are absent.
    rows = git('--no-replace-objects', 'log', '--topo-order', '--max-count=1024',
               '--no-show-signature', '--no-notes', '--format=%H %T %P',
               before, after, '--', check=False).splitlines()
    commits = {}
    for row in rows:
        commit, tree, *parents = row.decode('ascii').split()
        commits[commit] = (tree, parents)
    if before not in commits:
        return False

    def same_history(candidate):
        pending = [(before, candidate)]
        old_to_new, new_to_old = {}, {}
        while pending:
            old, new = pending.pop()
            if old in old_to_new:
                if old_to_new[old] != new:
                    return False
                continue
            if new in new_to_old:
                return False
            old_to_new[old], new_to_old[new] = new, old
            if old == new:
                continue
            if old not in commits or new not in commits:
                return False
            old_tree, old_parents = commits[old]
            new_tree, new_parents = commits[new]
            if old_tree != new_tree or len(old_parents) != len(new_parents):
                return False
            pending.extend(zip(old_parents, new_parents))
        return True

    checkpoint_tree = commits[before][0]
    while after in commits:
        tree, parents = commits[after]
        if tree == checkpoint_tree and same_history(after):
            return True
        after = parents[0] if parents else ''
    return False


def validate_change(git, before, after):
    if before == after:
        return
    ref = after.get('ref', '')
    if (ref != before.get('ref') or not ref.startswith('refs/heads/')
            or ref.startswith('refs/heads/auto-moniter/') or not after.get('commit')):
        raise GitHistoryError('Business branch changed or HEAD is detached; restore the task branch before continuing')
    old, new = before.get('commit', ''), after['commit']
    if not all(isinstance(oid, str) and re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', oid)
               for oid in (old, new) if oid != ''):
        raise GitHistoryError('Invalid business commit identity')
    git('--no-replace-objects', 'cat-file', '-e', new + '^{commit}')
    if old and git('--no-replace-objects', 'merge-base', old, new, check=False).decode().strip() != old:
        if not _has_rewritten_checkpoint(git, old, new):
            raise GitHistoryError('Business history was rewritten; reconcile the task branch before continuing')
    if git('ls-files', '--unmerged'):
        raise GitHistoryError('Business worktree has unresolved merge conflicts; resolve them before continuing')


def update_baseline(history, quality, before, current):
    """Adopt committed acceptance assets, leaving uncommitted edits detectable."""
    from auto_moniter.quality import Quality
    empty = history.git('hash-object', '-t', 'tree', '--stdin', data=b'').decode().strip()
    changed = [name.decode(errors='surrogateescape') for name in history.git(
        'diff', '--name-only', '-z', before.get('commit') or empty, history.business_head['commit'],
        '--', '.', ':(exclude).auto-moniter').split(b'\0') if name]
    proposed = dict(quality.baseline)
    for name in changed:
        absolute = str(history.cwd / name)
        if absolute in current:
            proposed[absolute] = current[absolute]
    probe = Quality(quality.config, proposed)
    probe.command_assets = quality.command_assets.copy()
    protected = set(quality.protected(history.cwd)) | set(probe.protected(history.cwd))
    result = dict(quality.baseline)
    for name in changed:
        absolute = str(history.cwd / name)
        if absolute not in protected:
            continue
        if history.git('--literal-pathspecs', 'diff', '--no-ext-diff', '--no-textconv', '--name-only',
                       history.business_head['commit'], '--', name):
            continue
        if absolute in current:
            result[absolute] = current[absolute]
        elif not (history.cwd / name).exists() and not (history.cwd / name).is_symlink():
            result.pop(absolute, None)
    return result
