"""Read immutable monitor records from a target repository without checking out files."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from auto_moniter.redaction import safe
from console.conclusions import present_conclusion


class GitRecordError(ValueError):
    pass


def read_git_record(project: Path, history: dict, checkpoint: dict) -> dict:
    commit, ref, record = checkpoint.get('commit', ''), history.get('ref', ''), checkpoint.get('record', '')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise GitRecordError('提交号格式不正确')
    if not re.fullmatch(r'refs/(?:heads/)?auto-moniter/[0-9a-f]{20}', ref):
        raise GitRecordError('任务未保存有效的 Git 审计分支')
    key = ref.rsplit('/', 1)[-1]
    if not re.fullmatch(r'\.auto-moniter/history/' + key + r'/\d+-(worker|reviewer)\.json', record):
        raise GitRecordError('结论文件不属于本次运行')
    project = project.resolve()

    def git(*args, check=True):
        env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat'}
        try:
            result = subprocess.run(['git', '-C', str(project), *args], capture_output=True, env=env, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitRecordError('无法读取本地 Git 记录') from exc
        if result.returncode and check:
            raise GitRecordError('本地提交或审计分支不可读取，请核对目标仓库')
        return result.stdout

    root = Path(os.fsdecode(git('rev-parse', '--show-toplevel')).strip()).resolve()
    if not git('rev-parse', '--verify', ref, check=False):
        hidden = 'refs/auto-moniter/' + key
        if git('rev-parse', '--verify', hidden, check=False):
            ref = hidden
        else:
            raise GitRecordError('审计引用已归档或不可用，请使用 history restore 恢复后查看')
    try:
        prefix = project.relative_to(root)
    except ValueError as exc:
        raise GitRecordError('Git 仓库与目标项目不匹配') from exc
    git('merge-base', '--is-ancestor', commit, ref)
    initial = history.get('audit_initial') or history.get('initial')
    if initial:
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', initial):
            raise GitRecordError('初始基线提交号格式不正确')
        git('merge-base', '--is-ancestor', initial, commit)
    object_name = commit + ':' + (prefix / record).as_posix()
    if int(git('cat-file', '-s', object_name)) > 4 * 1024 * 1024:
        raise GitRecordError('结论文件超过显示容量')
    try:
        payload = json.loads(git('show', object_name))
        result = payload['result']
        if (not isinstance(result, dict) or payload.get('role') != checkpoint.get('role')
                or payload.get('run') != key or payload.get('round') != checkpoint.get('round')):
            raise ValueError('record mismatch')
    except (ValueError, KeyError, TypeError) as exc:
        raise GitRecordError('Git 结论记录格式无效') from exc
    metadata = git('show', '-s', '--format=%H%n%ci%n%s', commit).decode(errors='replace').splitlines()
    return safe({'commit': commit, 'ref': ref, 'project': str(project), 'record': record,
                 'current_branch': git('symbolic-ref', '--quiet', '--short', 'HEAD', check=False).decode().strip(),
                 'committed_at': metadata[1], 'subject': metadata[2] if len(metadata) > 2 else '',
                 'conclusion': present_conclusion({**result, 'role': payload['role'], 'index': payload.get('round')}), 'content': payload,
                 'show_command': shlex.join(['git', '-C', str(project), 'show', object_name]),
                 'log_command': shlex.join(['git', '-C', str(project), 'log', '--oneline', ref.removeprefix('refs/heads/')])})
