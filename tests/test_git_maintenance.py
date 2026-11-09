"""Legacy refs migrate without history loss; only ended tasks are archived."""
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.git_history import GitHistory
from auto_moniter.git_maintenance import maintenance, restore, archive_path, handle, replace_ref
from auto_moniter.retention import run_storage
from console.git_records import read_git_record


class GitMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        (self.root/'.gitignore').write_text('.auto-moniter/\n')
        (self.root/'app.py').write_text('baseline\n')
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=f@local',
                        'commit','-qm','baseline'],check=True)
        self.runs = self.root/'.auto-moniter/runs'
        self.runs.mkdir(parents=True)

    def task(self, name='old', status='done', legacy=False, days=30):
        run=self.runs/name;run.mkdir()
        history=GitHistory(self.root,run)
        checkpoint=history.record(1,'worker',{'status':'continue','summary':'historical result'},before=history.begin_round(1))
        data=history.export()
        if legacy:
            old='refs/heads/auto-moniter/'+history.key
            history.git('update-ref',old,history.head)
            history.git('update-ref','-d',history.ref,history.head)
            data=replace_ref(data,history.ref,old)
            checkpoint=replace_ref(checkpoint,history.ref,old)
            (run/'git-history.json').write_text(json.dumps(data))
        state={'cwd':str(self.root),'state_version':1,'status':status,'git_history':data,
               'rounds':[{'role':'worker','index':1,'git_checkpoint':checkpoint}]}
        (run/'state.json').write_text(json.dumps(state));(run/'report.json').write_text(json.dumps({'status':status,'git_history':data}))
        (run/'supervisor.lock').touch()
        stamp=time.time()-days*86400;os.utime(run/'report.json',(stamp,stamp))
        return run,data,checkpoint

    def maintenance(self, **kwargs):
        return maintenance(self.root,runs_dirs=[self.runs],**kwargs)

    def test_preview_is_read_only_and_migration_preserves_web_history(self):
        run,history,checkpoint=self.task(legacy=True)
        before={p:p.read_bytes() for p in run.iterdir() if p.is_file()}
        result=self.maintenance(operation='migrate')
        self.assertTrue(result['runs'][0]['selected'])
        self.assertEqual({p:p.read_bytes() for p in before},before)
        self.assertTrue(handle(self.root).git('rev-parse','--verify',history['ref']))
        applied=self.maintenance(operation='migrate',apply=True)
        self.assertEqual(applied['errors'],[])
        saved=json.loads((run/'git-history.json').read_text())
        self.assertEqual(saved['head'],history['head'])
        self.assertTrue(saved['ref'].startswith('refs/auto-moniter/'))
        self.assertFalse(handle(self.root).git('rev-parse','--verify',history['ref'],check=False))
        self.assertEqual(read_git_record(self.root,history,checkpoint)['content']['result']['summary'],'historical result')
        self.assertEqual(GitHistory(self.root,run).ref,saved['ref'])
        self.assertEqual(self.maintenance(operation='migrate',apply=True)['runs'][0]['reason'],'already_hidden')

    def test_active_or_recoverable_tasks_are_not_cleaned(self):
        statuses=['running','stopped','blocked','budget_exceeded','action_required','recovery_failed']
        for status in statuses:self.task(status,status=status)
        run,_,_=self.task('locked')
        with (run/'supervisor.lock').open('r') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,include_failed=True,apply=True)
        self.assertFalse(any(v['selected'] for v in result['runs']))
        self.assertEqual(len(list(self.runs.iterdir())),7)

    def test_archive_is_verified_then_ref_removed_and_restore_survives_gc(self):
        run,history,checkpoint=self.task()
        result=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        self.assertEqual(result['errors'],[])
        git=handle(self.root);bundle=archive_path(git,history['ref'].split('/')[-1])
        self.assertTrue(bundle.is_file())
        self.assertFalse(git.git('rev-parse','--verify',history['ref'],check=False))
        subprocess.run(['git','-C',str(self.root),'reflog','expire','--expire=now','--all'],check=True)
        subprocess.run(['git','-C',str(self.root),'gc','--prune=now'],check=True,capture_output=True)
        self.assertTrue(restore(self.root,run)['restored'])
        saved=json.loads((run/'git-history.json').read_text())
        self.assertNotIn('archived',saved)
        self.assertEqual(read_git_record(self.root,saved,checkpoint)['commit'],checkpoint['commit'])

    def test_failed_bundle_verification_never_deletes_ref(self):
        run,history,_=self.task()
        original=GitHistory.git
        def fail(obj,*args,**kwargs):
            if args[:2]==('bundle','verify'):raise ValueError('bundle verification failed')
            return original(obj,*args,**kwargs)
        with patch.object(GitHistory,'git',fail):
            result=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        self.assertTrue(result['errors'])
        self.assertTrue(handle(self.root).git('rev-parse','--verify',history['ref']))
        self.assertNotIn('archived',json.loads((run/'git-history.json').read_text()))

    def test_checked_out_legacy_branch_is_not_migrated(self):
        run,history,_=self.task(legacy=True)
        subprocess.run(['git','-C',str(self.root),'checkout','-q',history['ref'].removeprefix('refs/heads/')],check=True)
        result=self.maintenance(operation='migrate',apply=True)
        self.assertTrue(result['errors'])
        self.assertEqual(json.loads((run/'git-history.json').read_text())['ref'],history['ref'])

    def test_age_and_count_retention_preserves_newest_runs(self):
        old,_,_=self.task('old',days=40)
        kept,_,_=self.task('kept',days=30)
        recent,_,_=self.task('recent',days=1)
        result=self.maintenance(operation='cleanup',keep_runs=1,older_than=7*86400)
        selected=[Path(v['run_dir']).name for v in result['runs'] if v['selected']]
        self.assertEqual(set(selected),{'old','kept'})
        self.assertFalse(json.loads((old/'git-history.json').read_text()).get('archived'))

    def test_bundle_restore_after_run_directory_removal(self):
        run,history,_=self.task()
        self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        shutil.rmtree(run)
        result=restore(self.root,run)
        self.assertTrue(result['restored']);self.assertFalse(result['run_records_present'])
        self.assertEqual(handle(self.root).git('rev-parse',history['ref']).decode().strip(),history['head'])

    def test_migration_conflict_preserves_legacy_ref(self):
        run,history,_=self.task(legacy=True)
        git=handle(self.root);hidden=history['ref'].replace('refs/heads/','refs/')
        business=git.git('rev-parse','HEAD').decode().strip();git.git('update-ref',hidden,business)
        result=self.maintenance(operation='migrate',apply=True)
        self.assertTrue(result['errors'])
        self.assertEqual(git.git('rev-parse',history['ref']).decode().strip(),history['head'])
        self.assertEqual(json.loads((run/'git-history.json').read_text())['ref'],history['ref'])

    def test_run_cleanup_archives_git_history_before_deleting_checkpoint(self):
        run,history,_=self.task(legacy=True)
        result=run_storage(self.root,runs_dir=self.runs,older_than=0,keep_runs=0,apply=True)
        self.assertEqual(result['errors'],[])
        self.assertFalse(run.exists())
        bundle=archive_path(handle(self.root),history['ref'].split('/')[-1])
        self.assertTrue(bundle.is_file())
        restored=restore(self.root,run)
        self.assertTrue(restored['restored'])
        self.assertTrue(restored['ref'].startswith('refs/auto-moniter/'))

    def test_archive_expiry_requires_age_and_count_and_preview_does_not_delete(self):
        run,history,_=self.task()
        self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        bundle=archive_path(handle(self.root),history['ref'].split('/')[-1])
        stamp=time.time()-100*86400;os.utime(bundle,(stamp,stamp))
        preview=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,keep_archives=0)
        self.assertTrue(preview['archives'][0]['selected']);self.assertTrue(bundle.exists())
        preserved=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,keep_archives=1,apply=True)
        self.assertTrue(bundle.exists())
        applied=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,keep_archives=0,apply=True)
        self.assertEqual(applied['errors'],[]);self.assertFalse(bundle.exists())
        self.assertTrue(bundle.with_suffix('.json').is_file())

    def test_restored_archive_is_not_expired_while_its_ref_is_live(self):
        run,history,_=self.task()
        self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        bundle=archive_path(handle(self.root),history['ref'].split('/')[-1])
        restore(self.root,run)
        # Preserve recent task refs; expiration must not remove its backing bundle.
        result=self.maintenance(operation='cleanup',keep_runs=5,older_than=0,keep_archives=0,
                                archive_older_than=0,apply=True)
        self.assertTrue(bundle.is_file())
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['archives'][0]['reason'],'live_ref_preserved')

    def test_corrupt_existing_bundle_does_not_allow_live_ref_deletion(self):
        run,history,_=self.task()
        self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        bundle=archive_path(handle(self.root),history['ref'].split('/')[-1])
        restore(self.root,run)
        contents=bundle.read_bytes();header_end=contents.index(b'\n\n')+2
        bundle.write_bytes(contents[:header_end]+b'PACK-corrupt')
        result=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
        self.assertTrue(result['errors'])
        self.assertEqual(handle(self.root).git('rev-parse',history['ref']).decode().strip(),history['head'])

    def test_cleanup_and_console_deletion_preserve_pending_work_checkpoints(self):
        from console.deletion import inactive_run
        for name in ('publication.json','work-checkpoint.json'):
            with self.subTest(name=name):
                run,history,_=self.task(name.replace('.','-'))
                (run/name).write_text('{}')
                result=self.maintenance(operation='cleanup',keep_runs=0,older_than=0,apply=True)
                row=next(v for v in result['runs'] if v['run_dir']==str(run))
                self.assertFalse(row['selected'])
                storage=run_storage(self.root,runs_dir=self.runs,keep_runs=0,older_than=0,apply=True)
                self.assertTrue(run.exists())
                with self.assertRaisesRegex(ValueError,'待恢复的提交记录'):
                    with inactive_run(run):
                        self.fail('Pending recovery cannot be deleted')
