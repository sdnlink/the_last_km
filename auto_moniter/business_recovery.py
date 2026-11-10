"""Recognize a new business input without reusing approval of the previous HEAD."""
from auto_moniter.git_history import GitHistoryError


class BusinessHeadChanged(GitHistoryError):
    pass


def validate_change(git, before, after):
    if before == after:
        return
    ref = after.get('ref', '')
    if (ref != before.get('ref') or not ref.startswith('refs/heads/')
            or ref.startswith('refs/heads/auto-moniter/') or not after.get('commit')):
        raise GitHistoryError('Business branch changed or HEAD is detached; restore the task branch before continuing')
    git('cat-file', '-e', after['commit'] + '^{commit}')
    if before.get('commit') and git('merge-base', before['commit'], after['commit'], check=False).decode().strip() != before['commit']:
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
