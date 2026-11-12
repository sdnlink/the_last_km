"""Isolated task branches and observable inputs for parallel project work."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from auto_moniter.health import read_run_snapshot
from auto_moniter.paths import state_root
from auto_moniter.storage import write_json
from auto_moniter.workspace import file_lock, file_snapshot, _file_token, _ignored


class ProjectInputsChanged(RuntimeError):
    """Restart from durable watcher state instead of accepting a stale result."""


def git(cwd, *args, check=True):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, timeout=30)
    if check and result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip() or 'Git project operation failed')
    return result.stdout.decode(errors='surrogateescape').rstrip('\n')


def read(path):
    if path.is_symlink():
        raise ValueError('Project coordination file cannot be a symlink')
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Project coordination file exceeds its limit')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid project coordination record')
        return value
    except FileNotFoundError:
        return {}


def validate_scope(scope):
    if not isinstance(scope, list) or not 1 <= len(scope) <= 50:
        raise ValueError('并行任务需要填写 1 到 50 个负责的文件或目录，每行一项')
    result = []
    for name in scope:
        if (not isinstance(name, str) or not name.strip() or len(name) > 1000
                or '\0' in name or '\n' in name or '\r' in name):
            raise ValueError('任务范围必须是有效的项目相对路径')
        name = name.strip()
        path = Path(name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or name in ('.', './')
                or any(part in {'.git', '.auto-moniter'} for part in path.parts)
                or any(c in name for c in '*?[]')):
            raise ValueError('任务范围只能指定项目相对文件或目录，不能是整个项目、Git 元数据或通配符')
        normalized = path.as_posix() + ('/' if name.endswith('/') else '')
        if normalized not in result:
            result.append(normalized)
    return result


def contains(scope, name):
    return any(name == item.rstrip('/') or (item.endswith('/') and name.startswith(item)) for item in scope)


def overlap(left, right):
    return (any(contains(left,p.rstrip('/')) for p in right)
            or any(contains(right,p.rstrip('/')) for p in left))


def task_status(run):
    lifecycle = read(Path(run) / 'project-lifecycle.json')
    if lifecycle.get('status') == 'deleted':
        return 'cancelled'
    try:
        snapshot = read_run_snapshot(Path(run))
        status = snapshot['state'].get('status', 'unknown')
        return 'finalizing' if status == 'done' and not snapshot['finished'] else status
    except (OSError, ValueError):
        return lifecycle.get('status', 'queued')


def dirty_state(root):
    raw = subprocess.check_output(['git','-C',str(root),'status','--porcelain=v1','-z','--untracked-files=all'],timeout=10)
    dirty, digest, count = {}, hashlib.sha256(), 0
    entries = iter(raw.split(b'\0'))
    for entry in entries:
        if not entry:
            continue
        names = [os.fsdecode(entry[3:])]
        if b'R' in entry[:2] or b'C' in entry[:2]:
            original = next(entries,b'')
            if original:
                names.append(os.fsdecode(original))
        for name in names:
            if _ignored(root/name,cwd=root):
                continue
            path = root / name
            try:
                token = _file_token(path) if path.is_file() or path.is_symlink() else 'removed'
            except (FileNotFoundError,PermissionError):
                token = 'changed while sampling'
            digest.update(os.fsencode(name)+b'\0'+token.encode()+b'\0')
            count += 1
            if len(dirty) < 200:
                dirty[name] = token
    return {'files':dirty,'signature':digest.hexdigest(),'count':count}


def create(cwd, run, ident, title, scope, dependencies, checks, *, validate=None):
    """Create from committed source, leaving all original workspace edits intact."""
    cwd, run = Path(cwd).resolve(), Path(run).resolve()
    scope = validate_scope(scope)
    if (not isinstance(dependencies, list) or len(dependencies) > 30
            or any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{32}', x) for x in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        raise ValueError('任务依赖必须是已有并行任务的 ID 列表')
    root = Path(git(cwd, 'rev-parse', '--show-toplevel')).resolve()
    source_ref = git(root, 'symbolic-ref', '-q', 'HEAD')
    source_commit = git(root, 'rev-parse', '--verify', 'HEAD')
    if not source_ref.startswith('refs/heads/'):
        raise ValueError('并行任务需要有已提交版本的业务分支')
    if any(str(cwd) in command or str(root) in command for command in checks):
        raise ValueError('并行任务的验收命令请使用项目相对路径，不能检查原工作区的绝对路径')
    for name in scope:
        if not (cwd / name).resolve().is_relative_to(cwd):
            raise ValueError('任务范围不能通过符号链接指向项目外')
    common = Path(git(root, 'rev-parse', '--git-common-dir'))
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    registry = common / 'auto-moniter-projects'
    registry.mkdir(exist_ok=True)
    checkout = state_root() / 'workspaces' / ident / 'checkout'
    branch = 'auto-moniter-task/' + ident
    run.mkdir(parents=True, exist_ok=True)
    with file_lock(registry / 'creation.lock'):
        peers = [read(p) for p in registry.glob('*.json')]
        by_id = {p['id']:p for p in peers}
        if any(x not in by_id for x in dependencies):
            raise ValueError('依赖只能引用本工程已创建的并行任务')
        if any(by_id[x]['source_cwd'] != str(cwd) for x in dependencies):
            raise ValueError('依赖任务须使用同一个项目目录')
        for peer in peers:
            prefix = Path(peer['source_cwd']).relative_to(peer['source_root']).as_posix()
            peer_scope = [(prefix+'/' if prefix != '.' else '')+p for p in peer['scope']]
            prefix = cwd.relative_to(root).as_posix()
            own_scope = [(prefix+'/' if prefix != '.' else '')+p for p in scope]
            status = task_status(peer['run_dir'])
            if (status not in {'done','cancelled'} and overlap(own_scope, peer_scope)
                    and peer['id'] not in dependencies):
                raise ValueError('任务范围与 '+peer['title']+' 重叠；请拆分文件范围，或明确将其设为前置依赖')
        checkout.parent.mkdir(parents=True, exist_ok=True)
        git(root, 'worktree', 'add', '-b', branch, str(checkout), source_commit)
        target = checkout / cwd.relative_to(root)
        manifest = {'version':1,'id':ident,'title':title,'source_root':str(root),'source_cwd':str(cwd),
                    'source_ref':source_ref,'source_commit':source_commit,'target_root':str(checkout),
                    'cwd':str(target),'branch':'refs/heads/'+branch,'scope':scope,
                    'dependencies':dependencies,'registry':str(registry),'run_dir':str(run)}
        try:
            if not target.is_dir():
                raise ValueError('项目子目录必须存在于已提交的基线版本中')
            if validate:
                validate(manifest)
            write_json(run / 'project-workspace.json', manifest, durable=True)
            write_json(registry / (ident+'.json'), manifest, durable=True)
        except BaseException:
            # Remove only this newly created pristine checkout. Never force a
            # removal if a concurrent process wrote files during validation.
            try:
                if git(checkout,'rev-parse','HEAD') == source_commit:
                    git(root, 'worktree', 'remove', str(checkout))
                    git(root, 'branch', '-d', branch)
                    (run / 'project-workspace.json').unlink(missing_ok=True)
                    (registry / (ident+'.json')).unlink(missing_ok=True)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            raise
    return manifest


class ProjectWorkspace:
    @classmethod
    def open(cls, cwd, run):
        manifest = read(Path(run) / 'project-workspace.json')
        return cls(cwd, run, manifest) if manifest else None

    def __init__(self, cwd, run, manifest):
        self.cwd, self.run, self.manifest = Path(cwd).resolve(), Path(run).resolve(), manifest
        if (manifest.get('version') != 1 or manifest.get('cwd') != str(self.cwd)
                or manifest.get('run_dir') != str(self.run)
                or git(self.cwd,'symbolic-ref','-q','HEAD') != manifest['branch']
                or git(self.cwd,'rev-parse','--show-toplevel') != manifest['target_root']):
            raise ValueError('Parallel task workspace or branch does not match its saved manifest')
        self.scope = validate_scope(manifest['scope'])
        self.state = read(self.run / 'project-observation.json')
        self.last_poll = 0
        self.current = self.state.get('current', {})
        self.signature = self.state.get('signature', '')
        self.reviewed = self.state.get('reviewed', '')
        self.sync_errors = self.state.get('sync_errors', [])
        self.local_changes = self.state.get('local_changes', [])
        self.interrupted_assignment = self.state.get('interrupted_assignment')

    def observe(self, force=False):
        if not force and time.monotonic() - self.last_poll < 3:
            return False
        self.last_poll = time.monotonic()
        m = self.manifest
        root = Path(m['source_root'])
        source_commit = git(root,'rev-parse','--verify',m['source_ref'])
        dirty = dirty_state(root)
        peers = []
        for path in sorted(Path(m['registry']).glob('*.json')):
            peer = read(path)
            if peer.get('id') == m['id']:
                continue
            status = task_status(peer['run_dir'])
            commit = git(root,'rev-parse','--verify',peer['branch'],check=False)
            accepted = ''
            if status == 'done':
                history = read(Path(peer['run_dir'])/'state.json').get('git_history',{})
                if (history.get('last_business_commit',{}).get('commit') == commit
                        and history.get('business_head',{}).get('ref') == peer['branch']):
                    accepted = commit
                else:
                    status = 'unverified_branch'
            try:
                unaccepted = dirty_state(Path(peer['target_root']))
            except (OSError,subprocess.SubprocessError):
                unaccepted = {'unavailable':True}
            peers.append({'id':peer['id'],'title':peer['title'],'scope':peer['scope'],
                          'cwd':peer['cwd'],'source_cwd':peer['source_cwd'],
                          'status':status,'branch':peer['branch'],'commit':commit,'accepted_commit':accepted,
                          'unaccepted_changes':unaccepted})
        current = {'source_commit':source_commit,'source_dirty':dirty['files'],
                   'source_checkout_ref':git(root,'symbolic-ref','-q','HEAD',check=False),
                   'source_dirty_signature':dirty['signature'],'source_dirty_count':dirty['count'],'peers':peers}
        signature = hashlib.sha256(json.dumps(current,sort_keys=True).encode()).hexdigest()
        changed = signature != self.signature
        self.current,self.signature = current,signature
        if changed:
            self.save()
        return changed

    def save(self):
        write_json(self.run / 'project-observation.json', {'current':self.current,'signature':self.signature,
            'reviewed':self.reviewed,'sync_errors':self.sync_errors,'local_changes':self.local_changes,
            'interrupted_assignment':self.interrupted_assignment,
            'merged_source':self.state.get('merged_source',self.manifest['source_commit'])},durable=True)

    @property
    def needs_review(self):
        return self.signature != self.reviewed or bool(self.local_changes)

    def mark_reviewed(self, signature):
        self.reviewed = signature
        self.local_changes = []
        self.interrupted_assignment = None
        self.save()

    def retain_assignment(self, task):
        if task:
            self.interrupted_assignment = task
            self.save()

    def waiting(self):
        peers = {p['id']:p for p in self.current.get('peers',[])}
        return [ident for ident in self.manifest['dependencies'] if peers.get(ident,{}).get('status') != 'done']

    def sync(self):
        """Merge committed inputs only; a refused/conflicting merge preserves work."""
        self.observe(force=True)
        commits = [self.current['source_commit']]
        peers = {p['id']:p for p in self.current.get('peers',[])}
        commits.extend(peers[i]['accepted_commit'] for i in self.manifest['dependencies']
                       if peers.get(i,{}).get('status') == 'done' and peers[i].get('accepted_commit'))
        self.sync_errors = []
        previous_source = self.state.get('merged_source', self.manifest['source_commit'])
        if git(self.cwd,'merge-base',previous_source,commits[0],check=False) != previous_source:
            self.sync_errors.append('Source history was rewritten; reconcile the source branch before integration')
            self.save()
            return
        for commit in dict.fromkeys(commits):
            base = git(self.cwd,'merge-base','HEAD',commit,check=False)
            if base == commit:
                if commit == self.current['source_commit']:
                    self.state['merged_source'] = commit
                continue
            if not base:
                self.sync_errors.append('Input history no longer shares a base: '+commit)
                continue
            if git(self.cwd,'ls-files','--unmerged') or git(self.cwd,'rev-parse','-q','--verify','MERGE_HEAD',check=False):
                self.sync_errors.append('An existing merge conflict requires review; no Git state was changed')
                continue
            before = file_snapshot(self.cwd,ignore_dir=self.run)
            index = git(self.cwd,'ls-files','--stage','-v','-z')
            failed = False
            try:
                result = subprocess.run(['git','-C',str(self.cwd),'-c','core.hooksPath=/dev/null','-c','commit.gpgSign=false',
                                     '-c','user.name='+ (git(self.cwd,'config','user.name',check=False) or 'auto-moniter'),
                                     '-c','user.email='+ (git(self.cwd,'config','user.email',check=False) or 'auto-moniter@localhost'),
                                     'merge','--no-edit',commit],capture_output=True,timeout=30)
                error = result.stderr.decode(errors='replace')[-2000:] if result.returncode else ''
                failed = bool(result.returncode)
            except subprocess.TimeoutExpired:
                error = 'Input merge timed out'
                failed = True
            if failed:
                unmerged = git(self.cwd,'diff','--name-only','--diff-filter=U',check=False)
                if git(self.cwd,'rev-parse','-q','--verify','MERGE_HEAD',check=False):
                    git(self.cwd,'merge','--abort')
                after = file_snapshot(self.cwd,ignore_dir=self.run)
                if before != after or index != git(self.cwd,'ls-files','--stage','-v','-z'):
                    raise ValueError('Input merge recovery changed the workspace; preserve files and inspect Git state')
                self.sync_errors.append('Input '+commit+' could not merge; work preserved. Conflicts: '+unmerged+
                                        '\n'+error)
            elif commit == self.current['source_commit']:
                self.state['merged_source'] = commit
        self.save()

    def note_changes(self, files):
        self.local_changes = list(files)[:100]
        self.save()

    def context(self):
        return {**{k:self.manifest[k] for k in ('source_root','source_cwd','source_ref','branch','scope','dependencies')},
                **self.current,'sync_errors':self.sync_errors,'local_changes':self.local_changes,
                'interrupted_assignment':self.interrupted_assignment,
                'delivery':'Reviewed work is committed to this task branch; source workspaces are never overwritten.'}

    def scope_errors(self, before, after):
        return [str(Path(name).relative_to(self.cwd)) for name in before.keys() | after.keys()
                if before.get(name) != after.get(name) and not contains(self.scope,str(Path(name).relative_to(self.cwd)))]

    def pending_scope_errors(self):
        """Check the whole unpublished delta, so a later no-op cannot hide it."""
        names = git(self.cwd,'diff','--no-renames','--relative','--name-only','-z','HEAD','--','.').split('\0')
        names += git(self.cwd,'ls-files','--others','--exclude-standard','-z','--','.').split('\0')
        return sorted({name for name in names if name and not _ignored(self.cwd/name,self.run,cwd=self.cwd)
                       and not contains(self.scope,name)})
