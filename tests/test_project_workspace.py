"""Real Git isolation, dependency acceptance, and concurrent-input recovery."""
import concurrent.futures
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.locking import workspace_lease
from auto_moniter.project_workspace import ProjectWorkspace, create, git, task_status, validate_scope
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import file_lock, file_snapshot
from tests.test_business_recovery import SnapshotReviewer
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'project'
        self.source.mkdir()
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR':str(self.root/'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        git(self.source,'init','-q')
        git(self.source,'config','user.name','Fixture')
        git(self.source,'config','user.email','fixture@local')
        for name in ['app.py','other.py','shared.py']:
            (self.source/name).write_text('original\n')
        (self.source/'.gitignore').write_text('.auto-moniter/\n')
        git(self.source,'add','.')
        git(self.source,'commit','-qm','baseline')
        self.baseline = git(self.source,'rev-parse','HEAD')
        self.count = 0

    def workspace(self,scope=None,dependencies=None,**options):
        self.count += 1
        ident = f'{self.count:032x}'
        manifest = create(self.source,self.root/('run'+str(self.count)),ident,'task '+str(self.count),
                          scope or ['app.py'],dependencies or [],['git diff --check'],**options)
        return ProjectWorkspace.open(manifest['cwd'],manifest['run_dir'])

    def commit(self,root,name,content='external\n'):
        (root/name).write_text(content)
        git(root,'add','--',name)
        git(root,'commit','-qm','external input')
        return git(root,'rev-parse','HEAD')

    def complete(self,project):
        (project.run/'supervisor.lock').touch()
        write_json(project.run/'state.json',{'status':'done','git_history':{
            'last_business_commit':{'commit':git(project.cwd,'rev-parse','HEAD')},
            'business_head':{'ref':project.manifest['branch']}}})
        write_json(project.run/'report.json',{'status':'done','rounds':1,'summary':'accepted','stop_reason':'done'})

    def test_disjoint_tasks_hold_independent_leases_and_preserve_source_edits(self):
        (self.source/'app.py').write_text('human draft\n')
        a,b=self.workspace(),self.workspace(['other.py'])
        with workspace_lease(a.cwd),workspace_lease(b.cwd):
            (a.cwd/'app.py').write_text('task A\n')
            (b.cwd/'other.py').write_text('task B\n')
        self.assertEqual((self.source/'app.py').read_text(),'human draft\n')
        self.assertEqual((b.cwd/'app.py').read_text(),'original\n')
        self.assertNotEqual(a.manifest['branch'],b.manifest['branch'])
        a.observe(force=True)
        self.assertIn('app.py',a.context()['source_dirty'])

    def test_overlapping_tasks_require_dependencies_and_invalid_scope_is_rejected(self):
        a=self.workspace()
        with self.assertRaisesRegex(ValueError,'重叠'):
            self.workspace()
        b=self.workspace(dependencies=[a.manifest['id']])
        b.observe(force=True)
        self.assertEqual(b.waiting(),[a.manifest['id']])
        for scope in [['.'],['../outside'],['src/*'],['.git/config'],[]]:
            with self.subTest(scope=scope),self.assertRaises(ValueError):
                validate_scope(scope)

    def test_existing_linked_source_worktree_uses_repository_relative_ownership(self):
        self.workspace()
        linked=self.root/'linked-source'
        git(self.source,'worktree','add','-qb','linked-source',str(linked))
        manifest=create(linked,self.root/'linked-run','f'*32,'linked task',['other.py'],[],['git diff --check'])
        self.assertTrue(Path(manifest['cwd']).is_dir())
        with self.assertRaisesRegex(ValueError,'重叠'):
            create(linked,self.root/'overlap-run','e'*32,'overlap',['app.py'],[],['git diff --check'])

    def test_failed_creation_validation_does_not_reserve_ownership(self):
        def reject(_): raise ValueError('bad acceptance assets')
        with self.assertRaisesRegex(ValueError,'bad acceptance'):
            self.workspace(validate=reject)
        self.assertEqual(len(git(self.source,'worktree','list','--porcelain').split('worktree '))-1,1)
        self.assertEqual(git(self.source,'for-each-ref','--format=%(refname)','refs/heads/auto-moniter-task/'),'')
        self.workspace()

    def test_dependency_waits_for_final_watcher_report_and_lock_release(self):
        a=self.workspace()
        b=self.workspace(['other.py'],[a.manifest['id']])
        self.commit(a.cwd,'app.py','accepted peer\n')
        write_json(a.run/'state.json',{'status':'done'})
        b.sync()
        self.assertEqual((b.cwd/'app.py').read_text(),'original\n')
        self.complete(a)
        with file_lock(a.run/'supervisor.lock'):
            self.assertEqual(task_status(a.run),'finalizing')
            b.sync()
            self.assertTrue(b.waiting())
        b.sync()
        self.assertFalse(b.waiting())
        self.assertEqual((b.cwd/'app.py').read_text(),'accepted peer\n')
        self.assertEqual((self.source/'app.py').read_text(),'original\n')

    def test_done_report_cannot_accept_later_unreviewed_branch_changes(self):
        a=self.workspace()
        b=self.workspace(['other.py'],[a.manifest['id']])
        self.complete(a)
        self.commit(a.cwd,'app.py','unreviewed change\n')
        b.sync()
        self.assertEqual(b.waiting(),[a.manifest['id']])
        self.assertEqual(b.current['peers'][0]['status'],'unverified_branch')
        self.assertEqual((b.cwd/'app.py').read_text(),'original\n')

    def test_peer_draft_changes_are_observed_but_never_imported(self):
        a=self.workspace()
        b=self.workspace(['other.py'])
        b.observe(force=True)
        b.mark_reviewed(b.signature)
        (a.cwd/'app.py').write_text('unaccepted draft\n')
        self.assertTrue(b.observe(force=True))
        self.assertTrue(b.needs_review)
        self.assertIn('app.py',b.current['peers'][0]['unaccepted_changes']['files'])
        b.sync()
        self.assertEqual((b.cwd/'app.py').read_text(),'original\n')

    def test_new_source_commit_merges_without_losing_worker_draft_or_untracked(self):
        a=self.workspace()
        (a.cwd/'app.py').write_text('worker draft\n')
        (a.cwd/'notes.txt').write_text('keep me')
        commit=self.commit(self.source,'shared.py')
        a.sync()
        self.assertFalse(a.sync_errors)
        self.assertEqual(git(a.cwd,'merge-base','HEAD',commit),commit)
        self.assertEqual((a.cwd/'app.py').read_text(),'worker draft\n')
        self.assertEqual((a.cwd/'notes.txt').read_text(),'keep me')
        a.mark_reviewed(a.signature)
        self.assertFalse(ProjectWorkspace.open(a.cwd,a.run).needs_review)

    def test_conflict_preserves_index_and_work_and_reports_to_reviewer(self):
        a=self.workspace()
        self.commit(a.cwd,'app.py','task commit\n')
        (a.cwd/'other.py').write_text('staged human draft\n')
        git(a.cwd,'add','other.py')
        self.commit(self.source,'app.py','source conflict\n')
        before=file_snapshot(a.cwd)
        index=git(a.cwd,'ls-files','--stage','-v','-z')
        head=git(a.cwd,'rev-parse','HEAD')
        a.sync()
        self.assertTrue(a.context()['sync_errors'])
        self.assertEqual(file_snapshot(a.cwd),before)
        self.assertEqual(git(a.cwd,'ls-files','--stage','-v','-z'),index)
        self.assertEqual(git(a.cwd,'rev-parse','HEAD'),head)
        self.assertEqual(git(a.cwd,'ls-files','--unmerged'),'')

    def test_actual_merge_conflict_is_aborted_without_losing_work(self):
        a=self.workspace()
        self.commit(a.cwd,'app.py','task commit\n')
        self.commit(self.source,'app.py','source commit\n')
        (a.cwd/'notes.txt').write_text('untracked human notes')
        before=file_snapshot(a.cwd)
        index=git(a.cwd,'ls-files','--stage','-v','-z')
        a.sync()
        self.assertIn('app.py',' '.join(a.sync_errors))
        self.assertEqual(file_snapshot(a.cwd),before)
        self.assertEqual(git(a.cwd,'ls-files','--stage','-v','-z'),index)
        self.assertEqual(git(a.cwd,'rev-parse','--verify','MERGE_HEAD',check=False),'')

    def test_source_rewrite_is_reported_without_merging(self):
        a=self.workspace()
        commit=self.commit(self.source,'shared.py')
        a.sync()
        self.assertFalse(a.sync_errors)
        git(self.source,'update-ref',a.manifest['source_ref'],self.baseline,commit)
        a=ProjectWorkspace.open(a.cwd,a.run)
        a.sync()
        self.assertIn('rewritten',' '.join(a.sync_errors))
        self.assertEqual(git(a.cwd,'rev-parse','HEAD'),commit)

    def test_scope_check_includes_prior_turn_changes_and_rename_source(self):
        a=self.workspace()
        (a.cwd/'other.py').write_text('out of scope\n')
        self.assertIn('other.py',a.pending_scope_errors())
        git(a.cwd,'mv','shared.py','app2.py')
        self.assertIn('shared.py',a.pending_scope_errors())
        self.assertIn('app2.py',a.pending_scope_errors())

    def monitor(self,project,worker,reviewer,**options):
        return Supervisor(RunConfig('Original requirement',project.cwd,worker,reviewer=reviewer,
            workflow='review-first',git_history='required',run_dir=project.run,retry_delay_s=0,
            max_rounds=4,max_agent_calls=15,checks=['git diff --check'],**options),log=lambda _:None)

    def worker(self,project,name='app.py',count=1):
        return ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=[name]),
                'touch':str(project.cwd/name)} for _ in range(count)])

    def reviewer(self,count=1):
        return SnapshotReviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)} for _ in range(count)]
                      +[{'verdict':_verdict('done',evidence=['app.py'])}])

    def test_change_during_requirement_review_retries_and_worker_gets_current_code(self):
        a=self.workspace()
        worker=self.worker(a)
        reviewer=self.reviewer(2)
        original=reviewer._command
        commits=[]
        def command(*args):
            prepared=original(*args)
            if not commits: commits.append(self.commit(self.source,'shared.py'))
            return prepared
        reviewer._command=command
        result=self.monitor(a,worker,reviewer).run()
        self.assertEqual(result.status,'done',result.summary)
        self.assertEqual(len(worker.calls),1)
        self.assertEqual(len(reviewer.calls),3)
        self.assertIn(commits[0],worker.calls[0]['prompt'])
        self.assertEqual((a.cwd/'shared.py').read_text(),'external\n')
        self.assertEqual((self.source/'app.py').read_text(),'original\n')

    def test_source_change_during_worker_discards_candidate_and_updates_assignment(self):
        a=self.workspace()
        worker=self.worker(a,count=2)
        reviewer=self.reviewer(2)
        original=worker._command
        commits=[]
        def command(*args):
            prepared=original(*args)
            if not commits: commits.append(self.commit(self.source,'shared.py'))
            return prepared
        worker._command=command
        result=self.monitor(a,worker,reviewer).run()
        self.assertEqual(result.status,'done',result.summary)
        self.assertEqual(len(worker.calls),2)
        self.assertEqual(len(reviewer.calls),3)
        self.assertIn(commits[0],worker.calls[1]['prompt'])
        self.assertEqual(worker.calls[1]['session_id'],'')

    def test_changed_workspace_during_readonly_review_is_reexamined_not_accepted(self):
        a=self.workspace()
        worker=self.worker(a,count=2)
        reviewer=SnapshotReviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['app.py'])},
            {'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['app.py'])}])
        original=reviewer._command
        def command(*args):
            prepared=original(*args)
            if len(reviewer.calls)==2: (a.cwd/'app.py').write_text('concurrent edit\n')
            return prepared
        reviewer._command=command
        result=self.monitor(a,worker,reviewer).run()
        self.assertEqual(result.status,'done',result.summary)
        self.assertEqual(len(worker.calls),2)
        self.assertEqual(len(reviewer.calls),4)
        self.assertIn('local_changes',reviewer.calls[2]['prompt'])
        self.assertTrue(any(r.failure and 'stale' in r.failure for r in result.records))

    def test_dependency_delivery_uses_actual_watcher_checkpoint(self):
        a=self.workspace()
        b=self.workspace(['other.py'],[a.manifest['id']])
        result=self.monitor(a,self.worker(a),self.reviewer()).run()
        self.assertEqual(result.status,'done',result.summary)
        b.observe(force=True)
        self.assertFalse(b.waiting())
        reviewer=SnapshotReviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['other.py'])}])
        result=self.monitor(b,self.worker(b,'other.py'),reviewer).run()
        self.assertEqual(result.status,'done',result.summary)
        self.assertEqual((b.cwd/'app.py').read_text(),'changed\n')
        self.assertEqual((self.source/'app.py').read_text(),'original\n')

    def test_noop_worker_cannot_publish_prior_out_of_scope_change(self):
        a=self.workspace()
        worker=self.worker(a,count=2)
        original=worker._command
        def command(*args):
            prepared=original(*args)
            if len(worker.calls)==1: (a.cwd/'other.py').write_text('unexpected edit\n')
            return prepared
        worker._command=command
        reviewer=SnapshotReviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['app.py'])},
            {'verdict':_verdict('done',evidence=['app.py'])}])
        monitor=self.monitor(a,worker,reviewer)
        monitor.config.max_rounds=2
        result=monitor.run()
        self.assertNotEqual(result.status,'done',result.summary)
        self.assertEqual(git(a.cwd,'rev-parse','HEAD'),self.baseline)
        self.assertTrue(any('scope' in gap for r in result.records for gap in r.verdict.gaps))

    def test_change_during_final_cleanup_reopens_review_instead_of_failing(self):
        a=self.workspace()
        worker=self.worker(a,count=2)
        reviewer=SnapshotReviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['app.py'])},
            {'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['app.py'])}])
        monitor=self.monitor(a,worker,reviewer)
        original=monitor._close_delivered_sessions
        commits=[]
        def close():
            result=original()
            if monitor._delivery_tree and not commits:
                commits.append(self.commit(self.source,'shared.py'))
            return result
        monitor._close_delivered_sessions=close
        result=monitor.run()
        self.assertEqual(result.status,'done',result.summary)
        self.assertEqual(len(worker.calls),2)
        self.assertEqual(len(reviewer.calls),4)
        self.assertIn(commits[0],worker.calls[1]['prompt'])

    def test_reviewer_cannot_dispatch_function_owned_by_another_scope(self):
        from tests.test_function_dispatch import task, plan
        a=self.workspace()
        worker=ScriptedProvider('worker',[])
        reviewer=SnapshotReviewer('reviewer',[plan(task('OUTSIDE','other.py'))])
        report=self.monitor(a,worker,reviewer,function_dispatch=True).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('declared task scope',report.summary)
        self.assertEqual(worker.calls,[])
        self.assertEqual(git(a.cwd,'rev-parse','HEAD'),self.baseline)

    def test_function_dispatch_replans_preserved_work_with_fresh_scoped_assignment(self):
        from tests.test_function_dispatch import FunctionWorker, task, result, plan
        a=self.workspace()
        first=task('F1','app.py')
        final=task('F2','app.py',scope='requirement')
        final['criterion_ids']=['main:1','req:1:1','req:1:2']
        worker=FunctionWorker('worker',[
            {**result('F1','done'),'touch':str(a.cwd/'app.py'),'source':'def parse_input():\n    return 42\n'},
            {**result('F2','done',status='done'),'touch':str(a.cwd/'app.py'),'source':'def parse_input():\n    return 43\n'}])
        reviewer=SnapshotReviewer('reviewer',[plan(first),plan(final),result('F2','accepted',status='done')])
        original=worker._command
        commits=[]
        def command(*args):
            prepared=original(*args)
            if not commits: commits.append(self.commit(self.source,'shared.py'))
            return prepared
        worker._command=command
        monitor=self.monitor(a,worker,reviewer,function_dispatch=True)
        monitor.config.checks=["python3 -c 'from app import parse_input; assert parse_input() == 43'"]
        report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(worker.calls),2)
        self.assertIn('F1',reviewer.calls[1]['prompt'])
        self.assertIn('interrupted_assignment',reviewer.calls[1]['prompt'])
        self.assertIn('F2',worker.calls[1]['prompt'])
        self.assertEqual((a.cwd/'app.py').read_text(),'def parse_input():\n    return 43\n')
        state=json.loads((a.run/'state.json').read_text())
        self.assertEqual(state['requirements_review']['acceptance_criteria'],BRIEF['acceptance_criteria'])
        self.assertEqual([item['task']['id'] for item in state['function_tasks']['accepted']],['F2'])

    def test_two_real_supervisors_can_develop_one_project_concurrently(self):
        a,b=self.workspace(),self.workspace(['other.py'])
        # Stable observations are pre-reviewed by each monitor; synchronizing
        # here proves worker subprocesses overlap, not merely sequential leases.
        barrier=threading.Barrier(2)
        def run(project,name):
            worker=self.worker(project,name)
            original=worker._command
            def command(*args):
                if not worker.scripts:
                    worker.scripts.append({'verdict':_verdict('done',evidence=[name])})
                prepared=original(*args)
                if len(worker.calls)==1:
                    barrier.wait(timeout=10)
                return prepared
            worker._command=command
            reviewer=SnapshotReviewer('reviewer',[])
            original_review=reviewer._command
            def review(session,prompt,readonly):
                reviewer.scripts.append({'verdict':_verdict('done',evidence=[name])}
                    if 'Worker checkpoint:' in prompt.read_text() else {'verdict':{},'text':json.dumps(BRIEF)})
                return original_review(session,prompt,readonly)
            reviewer._command=review
            return self.monitor(project,worker,reviewer).run()
        # Peer lifecycle changes intentionally trigger re-review in live runs;
        # freeze only status to isolate concurrency from the separate recovery tests.
        with patch('auto_moniter.project_workspace.task_status',return_value='running'):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(run,a,'app.py'),pool.submit(run,b,'other.py')]
                results=[f.result(timeout=40) for f in futures]
        self.assertEqual([r.status for r in results],['done','done'],[r.summary for r in results])
        self.assertEqual((self.source/'app.py').read_text(),'original\n')
        self.assertEqual((self.source/'other.py').read_text(),'original\n')
