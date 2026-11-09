"""Only reviewed code reaches the business branch; index and recovery stay safe."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.approved_history import ApprovedHistory
from auto_moniter.git_history import GitHistory, GitHistoryError
from auto_moniter.publication import publish, index_path
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import workspace_fingerprint
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


class BusinessPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.auto-moniter/\n')
        (self.root / 'app.py').write_text('baseline\n')
        (self.root / 'other.py').write_text('other\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@local', 'commit', '-qm', 'initial'], check=True)
        self.before = git(self.root, 'rev-parse', 'HEAD')
        self.run = self.root / '.auto-moniter/runs/test'
        self.run.mkdir(parents=True)

    def history(self, project=None):
        history = ApprovedHistory(project or self.root, self.run)
        before = history.begin_round(1)
        (history.cwd / 'app.py').write_text('reviewed code\n')
        history.prepare_review(1, before)
        return history

    def submit(self, history, index=1):
        worker = _verdict('done', evidence=['app.py'])
        review = _verdict('done', evidence=['app.py', history.candidate['commit']])
        tree = workspace_fingerprint(history.cwd, ignore_dir=self.run)
        return publish(history, index, {**worker, 'tree': tree}, {**review, 'tree': tree},
                       json.dumps(worker), json.dumps(review))

    def test_code_is_published_to_current_branch_and_audit_remains_hidden(self):
        history = self.history()
        first, second = self.submit(history)
        after = git(self.root, 'rev-parse', 'HEAD')
        self.assertNotEqual(after, self.before)
        self.assertEqual(git(self.root, 'show', 'HEAD:app.py'), 'reviewed code')
        self.assertEqual(git(self.root, 'show', ':app.py'), 'reviewed code')
        self.assertNotIn('.auto-moniter', git(self.root, 'ls-tree', '-r', '--name-only', 'HEAD'))
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertTrue(history.ref.startswith('refs/auto-moniter/'))
        self.assertNotIn('auto-moniter/', git(self.root, 'branch', '--format=%(refname)'))
        self.assertEqual(first['business_commit']['commit'], after)
        self.assertNotEqual(second['commit'], after)
        audit_files=git(self.root,'ls-tree','-r','--name-only',history.ref).splitlines()
        self.assertTrue(audit_files)
        self.assertTrue(all(name.startswith('.auto-moniter/history/') for name in audit_files))
        self.assertEqual(ApprovedHistory(self.root, self.run).business_head['commit'], after)
        self.assertEqual(self.submit(history), (first, second))
        self.assertEqual(git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_unrelated_staged_file_is_preserved(self):
        history = self.history()
        # The reviewer did not inspect this later staging operation; it must not
        # be overwritten or silently included by the publication.
        (self.root / 'staged-only.txt').write_text('human staging\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'staged-only.txt'], check=True)
        # Scope is the immutable reviewed candidate, not a fresh working-tree add.
        self.submit(history)
        self.assertEqual(git(self.root, 'show', ':staged-only.txt'), 'human staging')
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only'), 'staged-only.txt')
        self.assertNotIn('staged-only.txt', git(self.root, 'ls-tree', '-r', '--name-only', 'HEAD'))

    def test_staged_overlap_blocks_publication_without_losing_index(self):
        history = self.history()
        (self.root / 'app.py').write_text('human staged content\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'app.py'], check=True)
        original = index_path(history).read_bytes()
        with self.assertRaisesRegex(GitHistoryError, 'overlap existing staged'):
            self.submit(history)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual(index_path(history).read_bytes(), original)
        self.assertFalse(Path(str(index_path(history))+'.lock').exists())
        self.assertFalse(history.head)

    def test_subdirectory_publish_keeps_siblings_and_unrelated_staging(self):
        sub = self.root / 'sub'
        sub.mkdir()
        (sub/'app.py').write_text('sub baseline\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'sub'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@local', 'commit', '-qm', 'sub baseline'], check=True)
        history = self.history(sub)
        (self.root/'other.py').write_text('human outside staging\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'other.py'], check=True)
        self.submit(history)
        self.assertEqual(git(self.root, 'show', 'HEAD:other.py'), 'other')
        self.assertEqual(git(self.root, 'show', ':other.py'), 'human outside staging')
        self.assertEqual(git(self.root, 'show', 'HEAD:sub/app.py'), 'reviewed code')
        self.assertEqual(git(self.root, 'show', 'HEAD:app.py'), 'baseline')

    def test_ref_batch_failure_advances_neither_reference(self):
        history = self.history()
        original = history.git
        def fail(*args, **kwargs):
            if args[:2] == ('update-ref', '--stdin'):
                raise GitHistoryError('simulated transaction rejection')
            return original(*args, **kwargs)
        with patch.object(history, 'git', side_effect=fail):
            with self.assertRaisesRegex(GitHistoryError, 'transaction rejection'):
                self.submit(history)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        self.assertFalse(history.head)
        self.assertFalse((self.run/'publication.json').exists())
        self.assertEqual(git(self.root, 'status', '--porcelain'), 'M app.py')

    def test_interruption_after_ref_transaction_is_reconciled_idempotently(self):
        history = self.history()
        original = history.git
        def interrupt(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[:2] == ('update-ref', '--stdin'):
                raise OSError('interrupted after atomic ref update')
            return value
        with patch.object(history, 'git', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.submit(history)
        restored = ApprovedHistory(self.root, self.run)
        self.assertEqual(git(self.root, 'show', ':app.py'), 'reviewed code')
        self.assertEqual(restored.business_head['commit'], git(self.root, 'rev-parse', 'HEAD'))
        self.submit(restored)
        self.assertEqual(git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_full_gate_never_publishes_before_checks_and_approved_result_can_resume(self):
        run = self.root / 'run'
        worker = ScriptedProvider('worker', [{'verdict':_verdict('done', evidence=['app.py']), 'touch':'app.py'}])
        owner = self
        class Reviewer(ScriptedProvider):
            def _command(inner, session, prompt, read_only):
                if inner.calls:
                    owner.assertEqual(git(owner.root,'rev-parse','HEAD'),owner.before)
                    inner.scripts[0]['verdict']['evidence'].append(re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt.read_text()).group(1))
                return super()._command(session,prompt,read_only)
        reviewer = Reviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)}, {'verdict':_verdict('done',evidence=['app.py'])}])
        monitor = Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            git_history='required',run_dir=run,max_rounds=1,retry_delay_s=0,checks=['test -f app.py']),log=lambda _:None)
        original = monitor._commit_approved
        def stop_after_publish(*args):
            value = original(*args)
            raise OSError('crash after durable publication but before module bookkeeping')
        with patch.object(monitor,'_commit_approved',side_effect=stop_after_publish):
            result = monitor.run()
        self.assertEqual(result.status,'failed')
        # A failed monitor remains a historical failure; simulate a recoverable
        # process exit, keeping the exact immutable approval and source state.
        state=json.loads((run/'state.json').read_text());state['status']='running'
        (run/'state.json').write_text(json.dumps(state))
        no_worker=ScriptedProvider('worker',[]);no_reviewer=ScriptedProvider('reviewer',[])
        report=Supervisor(RunConfig('',self.root,no_worker,reviewer=no_reviewer,workflow='auto',
            git_history='required',run_dir=run,resume=True,max_rounds=1,retry_delay_s=0),log=lambda _:None).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(no_worker.calls,[]);self.assertEqual(no_reviewer.calls,[])
        self.assertEqual(git(self.root,'rev-list','--count','HEAD'),'2')

    def test_staged_conflict_resume_reuses_approved_candidate_without_agents(self):
        (self.root/'app.py').write_text('human staged draft\n')
        subprocess.run(['git','-C',str(self.root),'add','app.py'],check=True)
        run=self.root/'run'
        worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
        class Reviewer(ScriptedProvider):
            def _command(inner,session,prompt,read_only):
                if inner.calls:
                    inner.scripts[0]['verdict']['evidence'].append(re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt.read_text()).group(1))
                return super()._command(session,prompt,read_only)
        reviewer=Reviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)}, {'verdict':_verdict('done',evidence=['app.py'])}])
        config=RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            git_history='required',run_dir=run,max_rounds=1,retry_delay_s=0,checks=['test -f app.py'])
        failed=Supervisor(config,log=lambda _:None).run()
        self.assertEqual(failed.status,'recovery_failed')
        self.assertEqual(git(self.root,'show',':app.py'),'human staged draft')
        subprocess.run(['git','-C',str(self.root),'reset','-q','HEAD','--','app.py'],check=True)
        no_worker=ScriptedProvider('worker',[]);no_reviewer=ScriptedProvider('reviewer',[])
        resumed=Supervisor(RunConfig('',self.root,no_worker,reviewer=no_reviewer,workflow='auto',
            git_history='required',run_dir=run,resume=True,max_rounds=1,retry_delay_s=0),log=lambda _:None).run()
        self.assertEqual(resumed.status,'done',resumed.summary)
        self.assertEqual(no_worker.calls,[]);self.assertEqual(no_reviewer.calls,[])
        self.assertEqual(git(self.root,'show','HEAD:app.py'),'changed')

    def test_unapproved_candidate_survives_gc_without_becoming_a_branch(self):
        history=self.history()
        candidate=history.candidate['commit']
        subprocess.run(['git','-C',str(self.root),'reflog','expire','--expire=now','--all'],check=True)
        subprocess.run(['git','-C',str(self.root),'gc','--prune=now'],check=True,capture_output=True)
        restored=ApprovedHistory(self.root,self.run)
        self.assertEqual(restored.candidate['commit'],candidate)
        self.assertEqual(git(self.root,'show',candidate+':app.py'),'reviewed code')
        self.assertNotIn('auto-moniter',git(self.root,'branch','--format=%(refname)'))
        self.assertFalse(restored.head)

    def test_second_module_keeps_prior_committed_evidence_and_publishes_next_delta(self):
        from tests.test_modules import outcome,PLAN
        run=self.root/'run'
        worker=ScriptedProvider('worker',[
            {'verdict':outcome(['A'],plan=PLAN),'touch':'app.py'},
            {'verdict':outcome(['B'],'requirement')}])
        class Reviewer(ScriptedProvider):
            def _command(inner,session,prompt,read_only):
                if inner.calls:
                    inner.scripts[0]['verdict']['evidence'].append(re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt.read_text()).group(1))
                return super()._command(session,prompt,read_only)
        reviewer=Reviewer('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':outcome(['A'])},{'verdict':outcome(['B'],'requirement')}])
        report=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            git_history='required',run_dir=run,max_rounds=2,retry_delay_s=0,checks=['test -f app.py']),log=lambda _:None).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(worker.calls),2)
        self.assertEqual(git(self.root,'diff','HEAD','--name-only'),'')
        state=json.loads((run/'state.json').read_text())
        self.assertTrue(all(v['status']=='accepted' for v in state['modules']))
        self.assertEqual(git(self.root,'rev-list','--count','HEAD'),'2')

    def test_web_resume_accepts_durable_approved_head_ahead_of_state(self):
        from console.recovery import resume_arguments
        from auto_moniter.providers import resolve_provider
        from types import SimpleNamespace
        history=self.history()
        prior=history.export()
        config=RunConfig('',self.root,resolve_provider('codex'),reviewer=resolve_provider('codex'))
        state={'cwd':str(self.root),'workflow':'review-first','checks':[],
               'provider_config':Supervisor._provider_config(SimpleNamespace(config=config)),
               'git_history':prior}
        self.submit(history)
        args=['run','--workflow','review-first','--cwd',str(self.root),'--run-dir',str(self.run),
              '--provider','codex','--reviewer','codex']
        self.assertIn('--resume',resume_arguments({'argv':args},self.run,state))
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=f@local',
                        'commit','--allow-empty','-qm','external unapproved change'],check=True)
        with self.assertRaisesRegex(ValueError,'业务 HEAD'):
            resume_arguments({'argv':args},self.run,state)

    def test_archive_removes_temporary_candidate_ref_but_preserves_business_code(self):
        from auto_moniter.git_maintenance import maintenance, restore
        from console.git_records import read_git_record
        history=self.history();first,second=self.submit(history)
        state={'cwd':str(self.root),'state_version':1,'status':'done','git_history':history.export()}
        (self.run/'state.json').write_text(json.dumps(state));(self.run/'report.json').write_text(json.dumps({'status':'done','git_history':history.export()}))
        (self.run/'supervisor.lock').touch()
        code=git(self.root,'rev-parse','HEAD')
        result=maintenance(self.root,runs_dirs=[self.run.parent],operation='cleanup',
            older_than=0,keep_runs=0,apply=True)
        self.assertEqual(result['errors'],[])
        self.assertEqual(git(self.root,'rev-parse','HEAD'),code)
        self.assertEqual(git(self.root,'show','HEAD:app.py'),'reviewed code')
        self.assertFalse(history.git('rev-parse','--verify',history.work_ref,check=False))
        restore(self.root,self.run)
        shown=read_git_record(self.root,json.loads((self.run/'git-history.json').read_text()),second)
        self.assertEqual(shown['commit'],second['commit'])

    def test_later_round_deletes_file_created_by_previous_approved_round(self):
        history=ApprovedHistory(self.root,self.run)
        before=history.begin_round(1)
        (self.root/'new.py').write_text('temporary capability\n')
        history.prepare_review(1,before)
        self.submit(history)
        self.assertEqual(git(self.root,'show','HEAD:new.py'),'temporary capability')
        before=history.begin_round(2)
        (self.root/'new.py').unlink()
        history.prepare_review(2,before)
        self.submit(history,2)
        self.assertNotIn('new.py',git(self.root,'ls-tree','-r','--name-only','HEAD').splitlines())
        self.assertEqual(git(self.root,'status','--porcelain'),'')

    def test_file_directory_transitions_publish_exact_candidate(self):
        history=ApprovedHistory(self.root,self.run)
        before=history.begin_round(1)
        (self.root/'app.py').unlink();(self.root/'app.py').mkdir()
        (self.root/'app.py/child.py').write_text('nested source\n')
        history.prepare_review(1,before)
        self.submit(history)
        self.assertEqual(git(self.root,'show','HEAD:app.py/child.py'),'nested source')
        before=history.begin_round(2)
        (self.root/'app.py/child.py').unlink();(self.root/'app.py').rmdir()
        (self.root/'app.py').write_text('back to file\n')
        history.prepare_review(2,before)
        self.submit(history,2)
        self.assertEqual(git(self.root,'show','HEAD:app.py'),'back to file')
        self.assertEqual(git(self.root,'status','--porcelain'),'')

    def test_round_checkpoint_interrupted_after_work_ref_update_recovers(self):
        history=ApprovedHistory(self.root,self.run)
        before=history.begin_round(1)
        (self.root/'app.py').write_text('review candidate survives interruption\n')
        original=history._save
        def crash_before_save():
            raise OSError('interrupted before work checkpoint save')
        with patch.object(history,'_save',side_effect=crash_before_save):
            with self.assertRaisesRegex(OSError,'interrupted before work'):
                history.prepare_review(1,before)
        restored=ApprovedHistory(self.root,self.run)
        self.assertEqual(restored.candidate['round'],1)
        self.assertEqual(git(self.root,'show',restored.candidate['commit']+':app.py'),
                         'review candidate survives interruption')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)

    def test_creation_interrupted_after_work_ref_creation_recovers(self):
        def crash_before_save(obj):
            raise OSError('interrupted initial checkpoint save')
        with patch.object(ApprovedHistory,'_save',crash_before_save):
            with self.assertRaisesRegex(OSError,'interrupted initial'):
                ApprovedHistory(self.root,self.run)
        restored=ApprovedHistory(self.root,self.run)
        self.assertTrue(restored.work_head)
        self.assertEqual(git(self.root,'show',restored.initial+':app.py'),'baseline')
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.before)

    def test_interrupted_partial_index_write_recovers_without_losing_staging(self):
        history=self.history()
        (self.root/'human.txt').write_text('keep staged\n')
        subprocess.run(['git','-C',str(self.root),'add','human.txt'],check=True)
        sync=os.fsync
        interrupted=False
        def partial_sync(fd):
            nonlocal interrupted
            target=Path(os.readlink('/proc/self/fd/'+str(fd)))
            if not interrupted and target==Path(str(index_path(history))+'.lock') and (self.run/'publication-index.next').exists():
                interrupted=True
                os.ftruncate(fd,17)
                sync(fd)
                raise OSError('interrupted in partial Git index write')
            return sync(fd)
        with patch('auto_moniter.publication.os.fsync',side_effect=partial_sync):
            with self.assertRaisesRegex(OSError,'partial Git index'):
                self.submit(history)
        restored=ApprovedHistory(self.root,self.run)
        self.assertEqual(git(self.root,'show',':app.py'),'reviewed code')
        self.assertEqual(git(self.root,'show',':human.txt'),'keep staged')
        self.assertEqual(git(self.root,'diff','--cached','--name-only'),'human.txt')
        self.assertFalse((self.run/'publication.json').exists())
        self.assertEqual(restored.business_head['commit'],git(self.root,'rev-parse','HEAD'))

    def test_checkpoint_recovery_refuses_external_work_ref_change(self):
        history=ApprovedHistory(self.root,self.run)
        before=history.begin_round(1)
        (self.root/'app.py').write_text('pending candidate\n')
        with patch.object(history,'_save',side_effect=OSError('interrupt checkpoint')):
            with self.assertRaises(OSError):
                history.prepare_review(1,before)
        history.git('update-ref',history.work_ref,self.before)
        with self.assertRaisesRegex(GitHistoryError,'changed externally'):
            ApprovedHistory(self.root,self.run)
        self.assertEqual(git(self.root,'rev-parse',history.work_ref),self.before)
        self.assertTrue((self.run/'work-checkpoint.json').is_file())
