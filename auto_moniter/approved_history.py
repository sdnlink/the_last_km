"""Stage immutable review trees; create commits only after joint acceptance."""
import json
from pathlib import Path
from auto_moniter.git_history import GitHistory, GitHistoryError


class ApprovedHistory(GitHistory):
    def __init__(self,cwd,run_dir):
        import hashlib,os
        self.cwd,self.run_dir=cwd.resolve(),run_dir.resolve()
        self.key=hashlib.sha256(os.fsencode(self.run_dir)).hexdigest()[:20]
        self.ref='refs/auto-moniter/'+self.key
        self.directory=self.cwd/'.auto-moniter/history'/self.key
        self.state_path=self.run_dir/'git-history.json'
        self.root=Path(self.git('rev-parse','--show-toplevel').decode().strip()).resolve()
        for p in (self.cwd/'.auto-moniter',self.cwd/'.auto-moniter/history',self.directory):
            if p.is_symlink():raise GitHistoryError('History directory cannot be a symlink')
        self.directory.mkdir(parents=True,exist_ok=True)
        self.head=self.initial='';self.last_worker={};self.last_record={};self.candidate={}
        self.business_head=self._business_identity()
        self.last_reviewed={}
        self.business_commits=True
        self.last_business_commit={}
        self.audit_initial=''
        self.work_ref=''
        self.work_head=''
        if self.state_path.exists():
            saved=json.loads(self.state_path.read_text())
            if not saved.get('approval_only') or saved.get('project')!=str(self.cwd) or saved.get('ref') not in (self.ref, 'refs/heads/auto-moniter/'+self.key):
                raise GitHistoryError('Saved Git policy does not match review-first workflow')
            self.ref=saved['ref']
            from auto_moniter.publication import recover
            recover(self)
            saved=json.loads(self.state_path.read_text())
            self.head,self.initial=saved['head'],saved['initial']
            self.last_worker,self.last_record=saved.get('last_worker',{}),saved.get('last_record',{})
            self.candidate=saved.get('candidate',{})
            self.last_reviewed=saved.get('last_reviewed',{})
            self.business_head=saved.get('business_head',self.business_head)
            self.business_commits=saved.get('business_commits',False)
            self.last_business_commit=saved.get('last_business_commit',{})
            self.audit_initial=saved.get('audit_initial','')
            self.work_ref=saved.get('work_ref','')
            self.work_head=saved.get('work_head','')
            self.verify()
        else:
            if not self.business_head['ref'].startswith('refs/heads/') or self.business_head['ref'].startswith('refs/heads/auto-moniter/'):
                raise GitHistoryError('Start review-first on the business branch that should receive approved code')
            if self.git('rev-parse','--verify',self.ref,check=False):
                raise GitHistoryError('Existing branch has no run checkpoint')
            self.initial=self._tree()
            self.work_ref='refs/auto-moniter-work/'+self.key
            if self.git('rev-parse','--verify',self.work_ref,check=False):
                raise GitHistoryError('Existing task work ref has no checkpoint')
            self.work_head=self.git('commit-tree',self.initial,
                *(['-p',self.business_head['commit']] if self.business_head['commit'] else []),
                data=b'Unpublished task input\n').decode().strip()
            self.git('update-ref',self.work_ref,self.work_head,'0'*len(self.work_head))
            self._save()

    def verify(self):
        if self._business_identity()!=self.business_head:
            raise GitHistoryError('Business HEAD changed outside watcher approval; inspect unauthorized or concurrent commits')
        if self.work_ref and self.git('rev-parse','--verify',self.work_ref,check=False).decode().strip()!=self.work_head:
            raise GitHistoryError('Task work ref changed outside watcher')
        if getattr(self, '_defer_refs', False):
            actual = self.git('rev-parse', '--verify', self.ref, check=False).decode().strip()
            if actual != self._ref_before:
                raise GitHistoryError('Git audit ref changed during publication')
            return
        if self.head:
            if self.business_commits:
                if self.git('rev-parse','--verify',self.ref).decode().strip()!=self.head:
                    raise GitHistoryError('Git audit ref changed outside watcher')
                self.git('cat-file','-e',self.initial+'^{tree}')
                return
            return super().verify()
        if self.git('rev-parse','--verify',self.ref,check=False):
            raise GitHistoryError('Unapproved branch created outside the watcher')
        if self.initial:self.git('cat-file','-e',self.initial+'^{tree}')

    def _business_identity(self):
        return {'ref':self.git('symbolic-ref','-q','HEAD',check=False).decode().strip(),
                'commit':self.git('rev-parse','--verify','HEAD',check=False).decode().strip()}

    def begin_round(self,index):
        self.verify()
        return self._tree()

    def prepare_review(self,index,before):
        self.verify()
        self.candidate={'base':before,'commit':self._tree(),'round':index,
                        'record':str(self.run_dir/f'{index:03d}-worker-text.md'),'kind':'uncommitted_tree'}
        if self.business_commits:
            import tempfile
            from auto_moniter.publication import business_tree
            with tempfile.TemporaryDirectory(prefix='auto-moniter-business-tree-') as tmp:
                self.candidate['business_tree'] = business_tree(self, Path(tmp)/'index')
            next_work=self.git('commit-tree',self.candidate['commit'],'-p',self.work_head,
                               data=f'Pending task round {index}\n'.encode()).decode().strip()
            self.git('update-ref',self.work_ref,next_work,self.work_head)
            self.work_head=next_work
        self._save()

    def review_context(self):
        self.verify()
        candidate = self.candidate
        head = candidate['commit']
        base = self.last_reviewed.get('tree') or self.initial
        patch = self.git('diff', '--no-ext-diff', '--no-textconv', '--stat', base, head,
                         '--', '.', ':(exclude).auto-moniter').decode(errors='replace')
        changes = self.git('diff', '--no-ext-diff', '--no-textconv', base, head,
                           '--', '.', ':(exclude).auto-moniter').decode(errors='replace')
        from auto_moniter.runlog import clipped
        return '\n'.join([
            'Pending immutable Git TREE snapshot, not an approved commit.',
            f'Worker checkpoint: {head}', f'Worker conclusion: {candidate["record"]}',
            f'Previous completed business review tree: {base}',
            'Inspect this delta and its affected callers, requirements and open AUDs:',
            self.diff_command(base, head),
            'Reuse unchanged verified findings from the previous business audit. '
            'Reopen them only for a changed dependency, changed evidence, failed check or concrete counterexample. '
            'Do not repeat a full audit or demand another identical test run just because the round changed.',
            'The cumulative diff remains available when shared behavior or missing scope requires it:',
            self.diff_command(self.initial, head),
            ('Business target: ' + self.business_head['ref'] + '\nExact code publication diff (including initially dirty input):\n'
             + self.diff_command(self.business_head['commit'], self.candidate['business_tree'])
             if self.business_commits and self.business_head['commit'] else
             'Unborn business branch: review the complete candidate source before its first code commit.'),
            'Account for the publication diff before approval; reuse already verified unchanged portions. '
            'Cite the FULL worker checkpoint hash and actual changed-file evidence. '

            'No file writes, tests, builds or Git commits are permitted for the reviewer.',
            'Delta stat:\n' + clipped(patch, 12000), 'Delta:\n' + clipped(changes, 24000)])

    def remember_review(self, index):
        if self.candidate.get('round') == index:
            self.last_reviewed = {'round': index, 'tree': self.candidate['commit']}
            self._save()

    def _commit(self, tree, message):
        if not getattr(self, '_defer_refs', False):
            return super()._commit(tree, message)
        parents = ['-p', self.head] if self.head else []
        self.head = self.git('commit-tree', tree, *parents, data=(message+'\n').encode()).decode().strip()
        return self.head

    def _save(self):
        if not getattr(self, '_defer_refs', False):
            super()._save()

    def review_problems(self,evidence):
        if not self.candidate or not any(self.candidate['commit'] in item for item in evidence):
            return ['Review must cite the full pending worker tree snapshot hash']
        return []

    def record(self,index,role,data,*,before,raw=''):
        if not self.head:
            if self.business_commits:
                empty=self.git('hash-object','-t','tree','--stdin',data=b'').decode().strip()
                self.audit_initial=self._commit(empty,'Task audit metadata baseline')
            else:
                self._commit(self.initial,'Approved development input baseline')
                self.initial=self.head
            self._save()
        result=super().record(index,role,data,before=before,raw=raw)
        if self.business_commits:
            business=self.last_business_commit
            prefix=self.cwd.relative_to(self.root)
            result['changed_files']=[str(Path(p).relative_to(prefix)) if str(prefix)!='.' else p
                                     for p in business.get('changed_files',[])]
            result['diff_command']=self.diff_command(business.get('parent') or self.initial,business['commit'])
        return result

    def _tree(self, record=None):
        if not self.business_commits:
            return super()._tree(record)
        if record is None:
            previous=self.head
            self.head=''
            try:
                return super()._tree()
            finally:
                self.head=previous
        import tempfile
        with tempfile.TemporaryDirectory(prefix='auto-moniter-audit-index-') as tmp:
            env={'GIT_INDEX_FILE':str(Path(tmp)/'index')}
            self.git('read-tree',self.head,env=env)
            self.git('--literal-pathspecs','add','--force','--',str(record.relative_to(self.cwd)),env=env)
            return self.git('write-tree',env=env).decode().strip()

    def export(self):
        return {**super().export(),'approval_only':True,'candidate':self.candidate,'business_head':self.business_head,
                'last_reviewed':self.last_reviewed, 'business_commits':self.business_commits,
                'last_business_commit':self.last_business_commit, 'audit_initial':self.audit_initial,
                'work_ref':self.work_ref, 'work_head':self.work_head}
