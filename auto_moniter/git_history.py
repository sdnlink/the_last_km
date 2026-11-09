"""Project-local Git checkpoints using a private index and a dedicated audit branch."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.redaction import safe
from auto_moniter.runlog import clipped
from auto_moniter.storage import write_json
from auto_moniter.workspace import file_snapshot


class GitHistoryError(RuntimeError):
    pass


class GitHistory:
    def __init__(self, cwd: Path, run_dir: Path):
        self.cwd, self.run_dir = cwd.resolve(), run_dir.resolve()
        self.key = hashlib.sha256(os.fsencode(self.run_dir)).hexdigest()[:20]
        self.ref = 'refs/auto-moniter/' + self.key
        self.directory = self.cwd / '.auto-moniter/history' / self.key
        self.state_path = self.run_dir / 'git-history.json'
        self.root = Path(self.git('rev-parse', '--show-toplevel').decode().strip()).resolve()
        if not self.cwd.is_relative_to(self.root):
            raise GitHistoryError('Git repository does not contain the target project')
        self.head = ''
        self.initial = ''
        self.last_worker = {}
        self.last_record = {}
        for path in (self.cwd / '.auto-moniter', self.cwd / '.auto-moniter/history', self.directory):
            if path.is_symlink():
                raise GitHistoryError('Git history directory must not be a symbolic link')
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text(encoding='utf-8'))
            if saved.get('project') != str(self.cwd) or saved.get('ref') not in (self.ref, 'refs/heads/auto-moniter/' + self.key):
                raise GitHistoryError('Saved Git history belongs to another project or run')
            self.ref = saved['ref']
            self.head, self.initial = saved['head'], saved['initial']
            self.last_worker, self.last_record = saved.get('last_worker', {}), saved.get('last_record', {})
            self.verify()
        else:
            existing = self.git('rev-parse', '--verify', self.ref, check=False).strip()
            if existing:
                raise GitHistoryError('Git audit branch exists without its run checkpoint; refusing to overwrite it')
            self._commit(self._tree(), 'Initial project worktree baseline')
            self.initial = self.head
            self._save()

    @classmethod
    def open(cls, cwd: Path, run_dir: Path, mode: str = 'auto') -> GitHistory | None:
        if mode == 'off':
            if (run_dir / 'git-history.json').exists():
                raise GitHistoryError('Cannot disable Git history when resuming an audited run')
            return None
        try:
            probe = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd,
                                   capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if mode == 'required' or (cwd / '.git').exists() or (run_dir / 'git-history.json').exists():
                raise GitHistoryError(f'Cannot inspect project Git repository: {exc}') from exc
            return None
        if probe.returncode:
            if mode == 'required' or (cwd / '.git').exists() or (run_dir / 'git-history.json').exists():
                raise GitHistoryError('A working Git repository is required for project history')
            return None
        return cls(cwd, run_dir)

    def git(self, *args: str, data: bytes | None = None, env: dict | None = None, check: bool = True) -> bytes:
        environment = os.environ.copy()
        environment.update({'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
                            'GIT_AUTHOR_NAME': 'auto-moniter', 'GIT_AUTHOR_EMAIL': 'auto-moniter@localhost',
                            'GIT_COMMITTER_NAME': 'auto-moniter', 'GIT_COMMITTER_EMAIL': 'auto-moniter@localhost'})
        if env:
            environment.update(env)
        try:
            result = subprocess.run(['git', '-C', str(self.cwd), *args], input=data,
                                    capture_output=True, env=environment, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHistoryError(f'Git history command failed: {exc}') from exc
        if check and result.returncode:
            raise GitHistoryError('Git history command failed: ' + result.stderr.decode(errors='replace')[-1500:])
        return result.stdout if not result.returncode else b''

    def verify(self) -> None:
        actual = self.git('rev-parse', '--verify', self.ref).decode().strip()
        if actual != self.head:
            raise GitHistoryError('Git audit branch changed outside this monitor; refusing to overwrite history')
        self.git('cat-file', '-e', self.initial + '^{commit}')

    def _tree(self, record: Path | None = None) -> str:
        # Source selection matches acceptance and excludes runtime/build artifacts.
        files = [Path(name).relative_to(self.cwd).as_posix() for name in file_snapshot(self.cwd, ignore_dir=self.run_dir)]
        with tempfile.TemporaryDirectory(prefix='auto-moniter-git-index-') as tmp:
            env = {'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
            self.git('read-tree', '--empty', env=env)
            for offset in range(0, len(files), 128):
                self.git('--literal-pathspecs', 'add', '--force', '--', *files[offset:offset + 128], env=env)
            if self.head:
                # Carry immutable older records from Git, not writable project files.
                records = self.git('-C', str(self.root), '--literal-pathspecs', 'ls-tree', '-r', '-z',
                                   self.head, '--', self.directory.relative_to(self.root).as_posix())
                lines = []
                for entry in records.split(b'\0'):
                    if entry:
                        mode, _kind, rest = entry.split(b' ', 2)
                        lines.append(mode + b' ' + rest + b'\0')
                if lines:
                    self.git('update-index', '-z', '--index-info', data=b''.join(lines), env=env)
            if record:
                self.git('--literal-pathspecs', 'add', '--force', '--', record.relative_to(self.cwd).as_posix(), env=env)
            return self.git('write-tree', env=env).decode().strip()

    def _commit(self, tree: str, message: str) -> str:
        if self.head:
            self.verify()
        parents = ['-p', self.head] if self.head else []
        commit = self.git('commit-tree', tree, *parents, data=(message + '\n').encode()).decode().strip()
        self.git('update-ref', self.ref, commit, self.head or '0' * len(commit))
        self.head = commit
        return commit

    def begin_round(self, index: int) -> str:
        self.verify()
        tree = self._tree()
        if tree != self.git('rev-parse', self.head + '^{tree}').decode().strip():
            self._commit(tree, f'Round {index}: project input baseline')
        self._save()
        return self.head

    def record(self, index: int, role: str, data: dict, *, before: str, raw: str = '') -> dict:
        self.verify()
        name = f'{index:03d}-{role}.json'
        path = self.directory / name
        relative = path.relative_to(self.root).as_posix()
        existing = self.git('-C', str(self.root), '--literal-pathspecs', 'ls-tree', self.head, '--', relative)
        if existing or path.is_symlink():
            raise GitHistoryError(f'Git round record already exists: {name}; refusing to replace it')
        payload = safe({'version': 1, 'at': datetime.now(timezone.utc).isoformat(), 'project': str(self.cwd),
                        'run': self.key, 'role': role, 'round': index, 'before': before,
                        'reviewed_worker': self.last_worker if role == 'reviewer' else None,
                        'result': data, 'raw_conclusion': clipped(raw)})
        write_json(path, payload, durable=True)
        tree = self._tree(path)
        outcome = (data.get('watcher_result') or {}).get('status') if role == 'reviewer' else None
        title = f'Round {index} {role}: {data.get("status", "unknown")}'
        if outcome:
            title += f'; audit {outcome}'
        commit = self._commit(tree, title + '\n\n' +
                              str(safe(data.get('summary', '')))[:1000])
        changed = self.git('diff', '--no-ext-diff', '--no-textconv', '--relative', '--name-only', '-z', before, commit,
                           '--', '.', ':(exclude).auto-moniter').split(b'\0')
        result = {'ref': self.ref, 'base': before, 'commit': commit, 'record': str(path.relative_to(self.cwd)),
                  'round': index, 'role': role, 'changed_files': [os.fsdecode(name) for name in changed if name],
                  'diff_command': self.diff_command(before, commit)}
        self.last_record = result
        if role == 'worker':
            self.last_worker = result
        self._save()
        return result

    def diff_command(self, base: str, head: str) -> str:
        return shlex.join(['git', '-C', str(self.cwd), 'diff', '--no-ext-diff', '--no-textconv', base, head,
                           '--', '.', ':(exclude).auto-moniter'])

    def review_context(self) -> str:
        self.verify()
        worker = self.last_worker
        base, head = worker['base'], worker['commit']
        patch = self.git('diff', '--no-ext-diff', '--no-textconv', '--stat', base, head,
                         '--', '.', ':(exclude).auto-moniter').decode(errors='replace')
        changes = self.git('diff', '--no-ext-diff', '--no-textconv', base, head,
                           '--', '.', ':(exclude).auto-moniter').decode(errors='replace')
        return '\n'.join(['Git review in the target project is required.',
                          f'Project directory: {self.cwd}', f'Worker conclusion: {worker["record"]}',
                          f'Worker checkpoint: {head}', f'Round baseline: {base}',
                          'Run BOTH commands and inspect their actual changes against the requirement and worker conclusion:',
                          self.diff_command(base, head), self.diff_command(self.initial, head),
                          'The first diff isolates this worker round; the second covers all work since the run baseline.',
                          'Untracked additions and deletions are included in these Git checkpoints.',
                          'Cite the FULL worker checkpoint hash in your evidence, plus changed-file evidence.',
                          'Do not edit files or create Git commits; the monitor records your conclusion after you return.',
                          'Actual Git diff stat:\n' + clipped(patch, 12000),
                          'Monitor-executed round Git diff (run the commands above to inspect beyond this excerpt):\n' +
                          clipped(changes, 32000)])

    def review_problems(self, evidence: list[str]) -> list[str]:
        if not any(self.last_worker['commit'] in item for item in evidence):
            return ['Git review evidence must cite the full worker checkpoint hash: ' + self.last_worker['commit']]
        return []

    def export(self) -> dict:
        return {'version': 1, 'enabled': True, 'project': str(self.cwd), 'ref': self.ref,
                'directory': str(self.directory), 'initial': self.initial, 'head': self.head,
                'last_worker': self.last_worker, 'last_record': self.last_record}

    def _save(self) -> None:
        write_json(self.state_path, self.export(), durable=True)
