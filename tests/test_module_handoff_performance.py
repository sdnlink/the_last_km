"""Real loop and Git regression checks for bounded module handoff."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.git_history import GitHistory
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import file_snapshot, snapshot_fingerprint, workspace_fingerprint
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class ModuleHandoffTests(unittest.TestCase):
    def test_verified_module_is_accepted_while_other_original_modules_remain_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = [{'id': key, 'title': key, 'acceptance': ['behavior works']} for key in ['A', 'B']]
            submitted = {**_verdict('done', scope='module', evidence=['app.py']),
                         'modules': [], 'module_ids': ['A']}
            worker = ScriptedProvider('worker', [{'verdict': submitted, 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(BRIEF)}, {'verdict': submitted}])
            report = Supervisor(RunConfig('Implement A and B', root, worker, reviewer=reviewer,
                workflow='review-first', git_history='off', run_dir=root/'run', modules=plan,
                max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, 'max_rounds', report.summary)
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual({m['id']:m['status'] for m in state['modules']}, {'A':'accepted','B':'pending'})
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))

    def test_scoped_changed_audit_repair_gets_review_without_promoting_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repair = _verdict('continue', scope='module', gaps=['Other work remains'], evidence=['boundary.py'])
            repair['done'] = ['Fixed the reported boundary handling']
            worker = ScriptedProvider('worker', [
                {'verdict': _verdict('done', scope='module', evidence=['app.py']), 'touch': 'app.py'},
                {'verdict': repair, 'touch': 'boundary.py'},
                # Same bytes and same claim must not buy another model review.
                {'verdict': repair, 'touch': 'boundary.py'},
            ])
            reviewer = ScriptedProvider('reviewer', [
                {'verdict': {}, 'text': json.dumps(BRIEF)},
                {'verdict': _verdict('continue', gaps=['app.py: handle empty input'])},
                {'verdict': _verdict('continue', gaps=['AUD-1: still missing a failure case'])},
            ])
            report = Supervisor(RunConfig('Implement the requirement', root, worker, reviewer=reviewer,
                workflow='review-first', git_history='off', run_dir=root/'run', review_every=99,
                max_rounds=3, retry_delay_s=0,
                quality_policy={'review_interval_s': 0, 'review_changed_files': 0}), log=lambda _: None).run()
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 3))
            self.assertEqual(report.records[2].verdict.status, 'continue')
            state = json.loads((root/'run/state.json').read_text())
            self.assertTrue(all(m['status'] != 'accepted' for m in state['modules']))
            checkpoints = [json.loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
            self.assertEqual([e['reason'] for e in checkpoints if e['event']=='review_checkpoint'],
                             ['completion', 'audit_repair'])
            self.assertIn('Pending unrelated modules or reviewer approval are not', worker.calls[1]['prompt'])

    def test_new_boundary_snapshot_detects_same_size_rewrite_mode_delete_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source.py'; source.write_text('first')
            (root/'a').mkdir(); (root/'a/file.py').write_text('nested')
            (root/'a.py').write_text('sibling with shared prefix')
            link = root/'link'; link.symlink_to('source.py')
            previous = None
            changes = [lambda: None, lambda: source.write_text('other'), lambda: source.chmod(0o700),
                       lambda: link.unlink(), lambda: source.unlink()]
            for change in changes:
                change()
                snapshot = file_snapshot(root)
                tree = snapshot_fingerprint(root, snapshot)
                self.assertEqual(tree, workspace_fingerprint(root))
                self.assertNotEqual(tree, previous)
                previous = tree

    def test_unreadable_snapshot_cannot_be_used_as_a_valid_fingerprint(self):
        with self.assertRaises(PermissionError):
            snapshot_fingerprint(Path('/project'), {'/project/private.py': 'unreadable'})

    def test_large_git_snapshot_uses_one_literal_index_update_and_preserves_real_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project=root/'project'; project.mkdir(); run=root/'run'; run.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(project), *args])
            git('init', '-q')
            (project/'staged.txt').write_text('staged')
            git('add', 'staged.txt')
            (project/'staged.txt').write_text('unstaged')
            names = [f'part-{i}.py' for i in range(600)] + ['space name', 'line\nbreak', ':(exclude)literal']
            for name in names:(project/name).write_text('source')
            (project/'.gitignore').write_text('ignored.bin\n')
            (project/'ignored.bin').write_text('generated evidence')
            index_before=git('ls-files', '--stage', '-z')
            calls=[]
            original=GitHistory.git
            def call(history, *args, **kwargs):
                calls.append(args)
                return original(history,*args,**kwargs)
            # Git handles content hashing; Python must not pre-read all content too.
            with patch.object(GitHistory,'git',call), patch('auto_moniter.workspace._file_token',side_effect=AssertionError('duplicate content read')):
                history=GitHistory.open(project,run,'required')
            self.assertEqual(len([a for a in calls if a==('update-index','--add','-z','--stdin')]),1)
            self.assertEqual(index_before,git('ls-files','--stage','-z'))
            paths=set(git('ls-tree','-r','--name-only','-z',history.initial).decode().split('\0'))
            self.assertTrue(set(names)<=paths)
            self.assertNotIn('ignored.bin',paths)
            self.assertEqual(git('show',history.initial+':staged.txt'),b'unstaged')
            (project/names[0]).unlink()
            changed=history.begin_round(1)
            self.assertNotIn(names[0],git('ls-tree','-r','--name-only','-z',changed).decode().split('\0'))
            self.assertEqual(index_before,git('ls-files','--stage','-z'))

class AuditScopeTests(unittest.TestCase):
    def test_future_module_findings_keep_ids_and_do_not_block_current_module(self):
        from auto_moniter.audit import AuditLedger
        audit=AuditLedger()
        audit.update(['M1／missing check','M2/summary missing','M3:detail missing'],[],1,'tree',['M1'],
                     lambda _: [],known_modules={'M1','M2','M3'})
        self.assertEqual([i['module_ids'] for i in audit.items],[['M1'],['M2'],['M3']])
        audit.update([], [{'id':'AUD-1','summary':'Fix verified','evidence':['app.py']}],2,'new',['M1'],
                     lambda _: [],known_modules={'M1','M2','M3'})
        self.assertEqual(audit.pending({'M1'}),[])
        self.assertEqual([i['id'] for i in audit.pending()],['AUD-2','AUD-3'])
        # Repeated findings retain their existing IDs, not duplicate issues.
        reported,_=audit.update(['M2/summary missing'],[],3,'same',['M1'],lambda _: [],known_modules={'M1','M2','M3'})
        self.assertEqual(reported,['AUD-2'])

    def test_legacy_scope_repair_retains_states_and_ambiguous_or_unknown_ownership(self):
        from auto_moniter.audit import AuditLedger
        audit=AuditLedger()
        audit.record(['M2／future page missing','M9/unknown','Shared issue mentions M2 elsewhere'],1,['M1'])
        before=[(i['id'],i['summary'],i['status']) for i in audit.items]
        changes=audit.reconcile_modules({'M1','M2'})
        self.assertEqual(changes,[{'id':'AUD-1','before':['M1'],'after':['M2']}])
        self.assertEqual(before,[(i['id'],i['summary'],i['status']) for i in audit.items])
        self.assertEqual([i['module_ids'] for i in audit.items],[['M2'],['M1'],['M1']])
        self.assertEqual(audit.reconcile_modules({'M1','M2'}),[])

class RequirementNewlineResumeTests(unittest.TestCase):
    def test_crlf_legacy_and_new_hashes_resume_but_changed_requirement_is_rejected(self):
        import hashlib
        from auto_moniter.requirements_review import requirement_hash
        for legacy, tamper in [(False,False),(True,False),(True,True)]:
            with self.subTest(legacy=legacy,tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);run=root/'run';original='Implement A\r\nThen verify B'
                worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['finish B']),'touch':'app.py'}])
                reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)}])
                monitor=Supervisor(RunConfig(original,root,worker,reviewer=reviewer,workflow='review-first',
                    git_history='off',run_dir=run,retry_delay_s=0),log=lambda _:None)
                monitor.log=lambda msg:monitor.request_stop() if 'worker status=' in msg else None
                first=monitor.run();self.assertEqual(first.status,'stopped',first.summary)
                state=json.loads((run/'state.json').read_text())
                self.assertEqual(state['requirements_review']['original_sha256'],requirement_hash(original))
                if legacy:
                    # Fixture for the pre-fix protocol, not a production checkpoint migration.
                    state['requirements_review']['original_sha256']=hashlib.sha256(original.encode()).hexdigest()
                    (run/'state.json').write_text(json.dumps(state))
                if tamper:(run/'requirement.md').write_text('Different implementation scope')
                resumed_worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['boundary.py']),'touch':'boundary.py'}])
                resumed_reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('done',evidence=['boundary.py'])}])
                report=Supervisor(RunConfig(original,root,resumed_worker,reviewer=resumed_reviewer,
                    workflow='review-first',git_history='off',run_dir=run,resume=True,retry_delay_s=0,max_rounds=2),log=lambda _:None).run()
                self.assertEqual(report.status,'invalid' if tamper else 'done',report.summary)
                self.assertEqual(len(resumed_worker.calls),0 if tamper else 1)
                self.assertEqual(len(resumed_reviewer.calls),0 if tamper else 1)
