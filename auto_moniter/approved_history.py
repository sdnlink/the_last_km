"""Stage immutable review trees; create commits only after joint acceptance."""
import json
from pathlib import Path
from auto_moniter.git_history import GitHistory, GitHistoryError


class ApprovedHistory(GitHistory):
    def __init__(self,cwd,run_dir):
        import hashlib,os
        self.cwd,self.run_dir=cwd.resolve(),run_dir.resolve()
        self.key=hashlib.sha256(os.fsencode(self.run_dir)).hexdigest()[:20]
        self.ref='refs/heads/auto-moniter/'+self.key
        self.directory=self.cwd/'.auto-moniter/history'/self.key
        self.state_path=self.run_dir/'git-history.json'
        self.root=Path(self.git('rev-parse','--show-toplevel').decode().strip()).resolve()
        for p in (self.cwd/'.auto-moniter',self.cwd/'.auto-moniter/history',self.directory):
            if p.is_symlink():raise GitHistoryError('History directory cannot be a symlink')
        self.directory.mkdir(parents=True,exist_ok=True)
        self.head=self.initial='';self.last_worker={};self.last_record={};self.candidate={}
        self.business_head=self._business_identity()
        self.last_reviewed={}
        if self.state_path.exists():
            saved=json.loads(self.state_path.read_text())
            if not saved.get('approval_only') or saved.get('project')!=str(self.cwd) or saved.get('ref')!=self.ref:
                raise GitHistoryError('Saved Git policy does not match review-first workflow')
            self.head,self.initial=saved['head'],saved['initial']
            self.last_worker,self.last_record=saved.get('last_worker',{}),saved.get('last_record',{})
            self.candidate=saved.get('candidate',{})
            self.last_reviewed=saved.get('last_reviewed',{})
            self.business_head=saved.get('business_head',self.business_head)
            self.verify()
        else:
            if self.git('rev-parse','--verify',self.ref,check=False):
                raise GitHistoryError('Existing branch has no run checkpoint')
            self.initial=self._tree()
            self._save()

    def verify(self):
        if self._business_identity()!=self.business_head:
            raise GitHistoryError('Business HEAD changed outside watcher approval; inspect unauthorized or concurrent commits')
        if self.head:
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
            'Cite the FULL worker checkpoint hash and actual changed-file evidence. '
            'No file writes, tests, builds or Git commits are permitted for the reviewer.',
            'Delta stat:\n' + clipped(patch, 12000), 'Delta:\n' + clipped(changes, 24000)])

    def remember_review(self, index):
        if self.candidate.get('round') == index:
            self.last_reviewed = {'round': index, 'tree': self.candidate['commit']}
            self._save()

    def review_problems(self,evidence):
        if not self.candidate or not any(self.candidate['commit'] in item for item in evidence):
            return ['Review must cite the full pending worker tree snapshot hash']
        return []

    def record(self,index,role,data,*,before,raw=''):
        if not self.head:
            self._commit(self.initial,'Approved development input baseline')
            self.initial=self.head
            self._save()
        return super().record(index,role,data,before=before,raw=raw)

    def export(self):
        return {**super().export(),'approval_only':True,'candidate':self.candidate,'business_head':self.business_head,
                'last_reviewed':self.last_reviewed}
