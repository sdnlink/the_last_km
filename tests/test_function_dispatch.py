import json
import importlib.util
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.delivery_diagnosis import diagnose
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


def task(ident, file, *, depends=(), scope='function'):
    return {'id':ident,'module_id':'main','file':file,'symbol':'parse_input',
            'objective':'Implement '+ident,'inputs':'Raw input','outputs':'Validated input',
            'boundaries':['Reject malformed values'],'verification':['Valid and invalid inputs produce distinct outcomes'],
            'depends_on':list(depends),'delivery_scope':scope,
            'criterion_ids':['main:1', 'req:1:2' if scope == 'requirement' else 'req:1:1'],
            'check_ids':['check:1'],'test_files':[]}


CHECK = "python3 -c 'from pathlib import Path; assert any(\"def parse_input\" in p.read_text() for p in Path(\".\").glob(\"*.py\"))'"


def verification_task(ident='VERIFY', file='app.py', *, depends=()):
    return {**task(ident,file,scope='requirement',depends=depends),
            'kind':'verification','criterion_ids':['main:1','req:1:1','req:1:2']}


class FunctionWorker(ScriptedProvider):
    def _command(self, session_id, prompt_file, read_only):
        script = self.scripts[0]
        command = super()._command(session_id, prompt_file, read_only)
        script_path = Path(command.argv[-1])
        if 'source' in script or 'writes' in script:
            writes = script['writes'] if 'writes' in script else {script.get('touch', 'app.py'):script['source']}
            append = '\nfrom pathlib import Path\n'
            for name, value in writes.items():
                path = str(prompt_file.parent.parent / name)
                append += (f'Path({path!r}).unlink()\n' if value is None
                           else f'Path({path!r}).write_text({value!r})\n')
            source = script_path.read_text().replace('sys.exit(', append + 'sys.exit(', 1)
            script_path.write_text(source)
        elif script.get('touch'):
            script_path.write_text(script_path.read_text().replace("changed\\n", "def parse_input():\\n    return 42\\n"))
        return command


def result(ident, decision, *, status='continue', file='app.py', next_task=None, gaps=()):
    return {'verdict':{**_verdict(status, gaps=list(gaps), evidence=[file]),
                       'modules':[], 'task_id':ident,'task_status':decision,'next_task':next_task}}


def plan(first):
    return {'verdict':{},'text':json.dumps({**BRIEF,'next_task':first,
            'modules':_verdict('continue')['modules']})}


class DispatchTests(unittest.TestCase):
    def run_monitor(self, root, worker, reviewer, **kwargs):
        kwargs.setdefault('checks', [CHECK])
        kwargs.setdefault('review_every', 0)
        return Supervisor(RunConfig('ORIGINAL BACKLOG MUST NOT REACH WORKER',root,worker,reviewer=reviewer,
            workflow='review-first',function_dispatch=True,git_history='off',run_dir=root/'run',
            retry_delay_s=0,**kwargs),log=lambda _:None).run()

    def test_pause_during_next_function_planning_prevents_another_worker_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = FunctionWorker('worker',[
                {**result('F1','done'),'touch':'app.py'},
                {**result('F2','done',file='next.py'),'touch':'next.py'}])
            reviewer = ScriptedProvider('reviewer',[
                plan(task('F1','app.py')), result('F1','accepted'), plan(task('F2','next.py'))])
            elaborate = Supervisor._elaborate_requirement
            beat = Supervisor._beat
            boundaries = []
            def pause_after_plan(monitor, records, **kwargs):
                ok = elaborate(monitor, records, **kwargs)
                if kwargs.get('next_function'):
                    (root/'run/control.json').write_text(json.dumps({'action':'pause'}))
                return ok
            def stop_at_pause(monitor, note):
                if 'paused at a turn boundary' in note:
                    boundaries.append((len(worker.calls), len(reviewer.calls)))
                    (root/'run/control.json').write_text(json.dumps({'action':'stop'}))
                return beat(monitor, note)
            with patch.object(Supervisor, '_elaborate_requirement', pause_after_plan), \
                    patch.object(Supervisor, '_beat', stop_at_pause):
                report = self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'stopped',report.summary)
            self.assertEqual(boundaries,[(1,3)])
            self.assertFalse((root/'next.py').exists())
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['next_round'],2)
            self.assertEqual(state['function_tasks']['current']['id'],'F2')

    def test_chinese_criterion_citation_passes_handoff_without_extra_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = task('F1','app.py',scope='requirement')
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            implementation = result('F1','done',status='done')
            implementation['verdict']['evidence'] = [
                'QUOTES / main:1、req:1:3：app.py:86 在证券校验后恢复合法失败文字；报价校验保持不变。']
            worker = FunctionWorker('worker',[{**implementation,'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            self.assertTrue((root/'run/001-check-01.log').exists())
            self.assertEqual(report.records[-1].watcher_result['function']['status'],'accepted')

    def test_reviewer_corrects_missing_function_citation_without_reimplementing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = task('F1','app.py',scope='requirement')
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            worker = FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(assignment),
                result('F1','accepted',status='done',file='not-the-target.py'),
                result('F1','accepted',status='done')])
            report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,3))
            self.assertIn('Reviewer-only repair of the SAME candidate',reviewer.calls[-1]['prompt'])
            self.assertIn('missing: app.py',reviewer.calls[-1]['prompt'])
            self.assertTrue(all(call['read_only'] for call in reviewer.calls))

    def test_missing_reviewer_citation_has_only_one_bounded_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',file='missing.py'),result('F1','accepted',file='missing.py')])
            report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,3))
            self.assertIn('missing: app.py',report.summary)
            self.assertFalse((root/'run/001-check-01.log').exists())

    def test_resume_after_evidence_rejection_preserves_the_worker_candidate(self):
        from auto_moniter.workspace import evidence_paths
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = task('F1','app.py',scope='requirement')
            assignment['criterion_ids'] = ['main:1','req:1:1','req:1:2']
            implementation = result('F1','done',status='done')
            implementation['verdict']['evidence'] = ['main:1、req:1:3：app.py:86 已修复']
            worker = FunctionWorker('worker',[{**implementation,'touch':'app.py'}])
            reviewer = ScriptedProvider('reviewer',[plan(assignment),result('F1','accepted',status='done')])
            def old_parser(cwd, entries, **kwargs):
                if entries == implementation['verdict']['evidence']:
                    return []
                return evidence_paths(cwd, entries, **kwargs)
            with patch('auto_moniter.supervisor.evidence_paths',side_effect=old_parser):
                report = self.run_monitor(root,worker,reviewer,max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual(len(worker.calls),1)
            worker = FunctionWorker('worker',[])
            reviewer = ScriptedProvider('reviewer',[result('F1','accepted',status='done')])
            report = Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',
                resume=True,git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(len(worker.calls),0)
            self.assertEqual(len(reviewer.calls),1)
            self.assertTrue((root/'run/001-check-01.log').exists())

    def test_environment_repair_retries_checks_without_any_agent_call(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'project';root.mkdir();dependency=parent/'dependency-ready'
            check="test -f '"+str(dependency)+"' || exit 127"
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            next_task=task('F2','next.py',depends=['F1'],scope='requirement')
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',next_task=next_task)])
            report=self.run_monitor(root,worker,reviewer,checks=[check],max_rounds=1)
            self.assertEqual(report.status,'action_required',report.summary)
            state=json.loads((root/'run/state.json').read_text())
            original_calls=state['usage']['agent_calls']
            self.assertIn('check_retry',state['rounds'][-1]['watcher_result'])
            dependency.touch()
            worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=1,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'max_rounds',report.summary)
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'],'F2')
            self.assertEqual(state['function_tasks']['accepted'][0]['task']['id'],'F1')
            self.assertEqual(state['usage']['agent_calls'],original_calls)
            self.assertFalse(worker.calls or reviewer.calls)

    def test_changed_source_prevents_environment_review_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted')])
            report=self.run_monitor(root,worker,reviewer,checks=['exit 127'],max_rounds=1)
            self.assertEqual(report.status,'action_required')
            (root/'app.py').write_text('def parse_input(): return 0\n')
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[result('F1','accepted')])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual(len(worker.calls),1)
            self.assertEqual(len(reviewer.calls),1)

    def test_verification_closes_existing_implementation_without_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source='def parse_input():\n    return 42\n'
            (root/'app.py').write_text(source)
            worker=FunctionWorker('worker',[result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=1,
                checks=["python3 -c 'from app import parse_input; assert parse_input() == 42'"])
            self.assertEqual(report.status,'done',report.summary)
            self.assertTrue(worker.calls[0]['read_only'])
            self.assertTrue(all(c['read_only'] for c in reviewer.calls))
            self.assertEqual((root/'app.py').read_text(),source)
            accepted=json.loads((root/'run/state.json').read_text())['function_tasks']['accepted']
            self.assertEqual(accepted[0]['task']['kind'],'verification')
            self.assertEqual(accepted[0]['verification'],{'check:1':'001-check-01.log'})

    def test_verification_after_function_acceptance_does_not_require_new_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'},
                result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),
                result('F1','accepted',next_task=verification_task(depends=['F1'])),
                result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual([c['read_only'] for c in worker.calls],[False,True])
            self.assertIn('Verification-only',worker.calls[-1]['prompt'])

    def test_verification_modification_or_failed_assertion_never_delivers(self):
        for change,check in [(True,CHECK),(False,'false')]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'app.py').write_text('def parse_input(): return 0\n')
                worker=FunctionWorker('worker',[{**result('VERIFY','done',status='done'),
                    **({'touch':'app.py'} if change else {})}])
                reviewer=ScriptedProvider('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
                report=self.run_monitor(root,worker,reviewer,max_rounds=1,checks=[check])
                self.assertNotEqual(report.status,'done')
                state=json.loads((root/'run/state.json').read_text())
                self.assertEqual(state['function_tasks']['accepted'],[])

    def test_failed_verification_dispatches_a_bounded_implementation_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'app.py').write_text('def parse_input(): return 0\n')
            implementation=task('REPAIR','app.py',scope='requirement')
            implementation['criterion_ids']=['main:1','req:1:1','req:1:2']
            worker=FunctionWorker('worker',[result('VERIFY','incomplete',gaps=['Return value is incorrect']),
                {**result('REPAIR','done',status='done'),'touch':'app.py'}])
            rejected=result('VERIFY','changes_required',gaps=['main/Return value must be 42'],next_task=implementation)
            accepted=result('REPAIR','accepted',status='done')
            accepted['verdict']['issue_resolutions']=[{'id':'AUD-1','summary':'Verified return value is 42','evidence':['app.py']}]
            reviewer=ScriptedProvider('reviewer',[plan(verification_task()),rejected,accepted])
            report=self.run_monitor(root,worker,reviewer,max_rounds=2,review_every=1)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual([call['read_only'] for call in worker.calls],[True,False])
            ledger=json.loads((root/'run/state.json').read_text())['function_tasks']
            self.assertEqual([item['task']['id'] for item in ledger['accepted']],['REPAIR'])

    def test_final_verification_environment_retry_never_calls_models_again(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'project';root.mkdir();dependency=parent/'ready'
            (root/'app.py').write_text('def parse_input(): return 42\n')
            worker=FunctionWorker('worker',[result('VERIFY','done',status='done')])
            reviewer=ScriptedProvider('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
            report=self.run_monitor(root,worker,reviewer,checks=["test -f '"+str(dependency)+"' || exit 127"])
            self.assertEqual(report.status,'action_required',report.summary)
            before=json.loads((root/'run/state.json').read_text())
            dependency.touch()
            worker=FunctionWorker('worker',[]);reviewer=ScriptedProvider('reviewer',[])
            report=Supervisor(RunConfig('',root,worker,reviewer=reviewer,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertFalse(worker.calls or reviewer.calls)
            after=json.loads((root/'run/state.json').read_text())
            self.assertEqual(after['usage']['agent_calls'],before['usage']['agent_calls'])

    def test_new_human_direction_prevents_reusing_an_environment_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted')])
            self.run_monitor(root,worker,reviewer,checks=['exit 127'],max_rounds=1)
            state=json.loads((root/'run/state.json').read_text())
            from auto_moniter.supervisor import _record_from_state
            monitor=Supervisor(RunConfig('',root,FunctionWorker('worker',[]),reviewer=ScriptedProvider('reviewer',[]),
                workflow='review-first',run_dir=root/'run',resume=True),log=lambda _:None)
            monitor._load_resume()
            record=_record_from_state(state['rounds'][-1])
            self.assertIsNotNone(monitor._check_retry_review(record))
            with patch.object(monitor,'_pending_directions',return_value=True):
                self.assertIsNone(monitor._check_retry_review(record))

    def test_verification_of_committed_code_does_not_create_a_fake_code_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(['git','init','-q',str(root)],check=True)
            (root/'app.py').write_text('def parse_input(): return 42\n')
            subprocess.run(['git','add','app.py'],cwd=root,check=True)
            subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','initial'],cwd=root,check=True)
            head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            class Reviewer(ScriptedProvider):
                def _command(self, session, prompt_file, read_only):
                    match=re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt_file.read_text())
                    if match:self.scripts[0]['verdict']['evidence'].append(match[1])
                    return super()._command(session,prompt_file,read_only)
            worker=FunctionWorker('worker',[result('VERIFY','done',status='done')])
            reviewer=Reviewer('reviewer',[plan(verification_task()),result('VERIFY','accepted',status='done')])
            report=Supervisor(RunConfig('Verify existing implementation',root,worker,reviewer=reviewer,
                workflow='review-first',function_dispatch=True,git_history='required',run_dir=root/'run',
                checks=[CHECK],retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),head)
            publication=report.records[-1].git_checkpoint['business_commit']
            self.assertFalse(publication['created'])

    def test_review_repair_stays_on_first_function_then_dispatches_next(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=task('F1','app.py');second=task('F2','next.py',depends=['F1'],scope='requirement')
            w=FunctionWorker('worker',[
                {**result('F1','done'),'touch':'app.py'}, result('F1','done'),
                {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            final=result('F2','accepted',status='done',file='next.py')
            final['verdict']['issue_resolutions']=[{'id':'AUD-1','summary':'Revalidated after integration change','evidence':['app.py']}]
            r=ScriptedProvider('reviewer',[plan(first),
                result('F1','changes_required',gaps=['app.py: invalid data must be checked']),
                {**result('F1','accepted',next_task=second),'verdict':{
                    **result('F1','accepted',next_task=second)['verdict'],
                    'issue_resolutions':[{'id':'AUD-1','summary':'Verified malformed input behavior','evidence':['app.py']}]}},
                final])
            report=self.run_monitor(root,w,r,max_rounds=3)
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual((len(w.calls),len(r.calls)),(3,4))
            self.assertNotIn('F2',w.calls[0]['prompt'])
            self.assertIn('invalid data must be checked',w.calls[1]['prompt'])
            self.assertNotIn('invalid data must be checked',w.calls[2]['prompt'])
            self.assertTrue(all('ORIGINAL BACKLOG' not in call['prompt'] for call in w.calls))
            self.assertTrue(all(call['read_only'] for call in r.calls))
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual([i['task']['id'] for i in state['function_tasks']['accepted']],['F1','F2'])
            self.assertTrue((root/'run/003-check-01.log').exists())
            self.assertFalse((root/'run/001-check-01.log').exists())
            self.assertEqual(state['function_tasks']['accepted'][0]['verification'], {'check:1':'002-check-01.log'})

    def test_missing_function_or_unaccepted_dependency_never_launches_worker(self):
        for response in [{'verdict':{},'text':json.dumps(BRIEF)},plan(task('F2','app.py',depends=['F1']))]:
            with tempfile.TemporaryDirectory() as d:
                w=ScriptedProvider('worker',[]);r=ScriptedProvider('reviewer',[response])
                report=self.run_monitor(Path(d),w,r,max_rounds=1)
                self.assertEqual(report.status,'action_required',report.summary)
                self.assertEqual(w.calls,[])

    def test_reviewer_cannot_approve_an_incomplete_worker_function(self):
        with tempfile.TemporaryDirectory() as d:
            w=ScriptedProvider('worker',[{**result('F1','incomplete',gaps=['unfinished']),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted',next_task=task('F2','next.py'))])
            report=self.run_monitor(Path(d),w,r,max_rounds=1,quality_policy={'review_changed_files':1})
            self.assertEqual(report.status,'action_required',report.summary)
            state=json.loads((Path(d)/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'],'F1')
            self.assertEqual(state['function_tasks']['accepted'],[])

    def test_checkpoint_keeps_next_function_across_resume(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);second=task('F2','next.py',depends=['F1'],scope='requirement')
            w=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted',next_task=second)])
            s=Supervisor(RunConfig('task',root,w,reviewer=r,workflow='review-first',function_dispatch=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0,checks=[CHECK]),log=lambda _:None)
            emit=s._event
            def stop(kind,**data):
                emit(kind,**data)
                if kind=='function_assigned' and data['task_id']=='F2':s.request_stop()
            s._event=stop
            self.assertEqual(s.run().status,'stopped')
            w=FunctionWorker('worker',[{**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
            r=ScriptedProvider('reviewer',[result('F2','accepted',status='done',file='next.py')])
            report=Supervisor(RunConfig('',root,w,reviewer=r,workflow='review-first',resume=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertIn('Implement F2',w.calls[0]['prompt'])
            self.assertNotIn('Implement F1',w.calls[0]['prompt'])
            self.assertEqual(len(r.calls),1)


    def test_runtime_handoff_records_roles_and_current_function(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=task('F1','app.py');second=task('F2','next.py')
            w=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(first),result('F1','accepted',next_task=second)])
            s=Supervisor(RunConfig('task',root,w,reviewer=r,workflow='review-first',function_dispatch=True,
                git_history='off',run_dir=root/'run',max_runtime_s=600,retry_delay_s=0,checks=[CHECK]),log=lambda _:None)
            emit=s._event
            def advance_clock(kind,**data):
                emit(kind,**data)
                if kind=='function_accepted':s._elapsed=lambda:580
            s._event=advance_clock
            report=s.run()
            self.assertEqual(report.status,'budget_exceeded',report.summary)
            saved=json.loads((root/'run/report.json').read_text())
            d=saved['delivery_diagnosis']
            self.assertEqual(d['code'],'function_accepted_remaining_work_pending')
            self.assertEqual(d['current_function']['id'],'F2')
            self.assertEqual((d['worker']['task_id'],d['reviewer']['task_id']),('F1','F1'))
            self.assertEqual(d['last_call']['role'],'reviewer')
            self.assertEqual(len(w.calls),1)

    def test_empty_check_catalog_stops_before_planning(self):
        with tempfile.TemporaryDirectory() as d:
            worker, reviewer = ScriptedProvider('worker', []), ScriptedProvider('reviewer', [])
            report = self.run_monitor(Path(d), worker, reviewer, checks=[])
            self.assertEqual(report.status, 'action_required')
            self.assertIn('approved executable', report.summary)

    def test_portable_swift_regression_path_is_accepted_as_a_new_test(self):
        from auto_moniter.function_gate import validate_task
        with tempfile.TemporaryDirectory() as directory:
            assignment=task('F1','app.py')
            assignment['test_files']=['StockTianjiApp/PortableTests/QuoteObservationFieldRegression/main.swift']
            validate_task(assignment,Path(directory),{'main':{'acceptance':['works']}},[CHECK],BRIEF)

    def test_missing_symbol_or_outside_edit_cannot_advance(self):
        for source, reason in [('unrelated = 1\n', 'Implement or change'),
                ('outside = 2\ndef parse_input():\n    return 42\n', 'Restore edits outside')]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / 'app.py').write_text('outside = 1\ndef parse_input():\n    return 0\n')
                worker = FunctionWorker('worker', [{**result('F1','done'), 'source': source}])
                reviewer = ScriptedProvider('reviewer', [plan(task('F1','app.py')), result('F1','accepted')])
                self.run_monitor(root, worker, reviewer, max_rounds=1)
                state = json.loads((root/'run/state.json').read_text())
                self.assertEqual(state['function_tasks']['accepted'], [])
                self.assertIn(reason, state['rounds'][-1]['watcher_result']['reasons'][0])
                self.assertFalse((root/'run/001-check-01.log').exists())

    def test_regression_failure_returns_to_same_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worker = FunctionWorker('worker', [
                {**result('F1','done'), 'source': 'def parse_input():\n    return 0\n'},
                {**result('F1','done'), 'source': 'def parse_input():\n    return 42\n'}])
            reviewer = ScriptedProvider('reviewer', [plan(task('F1','app.py')),
                result('F1','accepted'), result('F1','accepted',next_task=task('F2','next.py'))])
            self.run_monitor(root, worker, reviewer, max_rounds=2,
                checks=["python3 -c 'from app import parse_input; assert parse_input() == 42'"])
            self.assertIn('Acceptance check failed', worker.calls[1]['prompt'])
            state = json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['function_tasks']['current']['id'], 'F2')
            self.assertEqual(state['function_tasks']['accepted'][0]['verification'], {'check:1':'002-check-01.log'})

    def test_premature_final_plan_never_launches_worker(self):
        with tempfile.TemporaryDirectory() as d:
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py', scope='requirement'))])
            report = self.run_monitor(Path(d), worker, reviewer)
            self.assertEqual(report.status, 'action_required')
            self.assertIn('uncovered criteria: req:1:1', report.summary)
            self.assertFalse(worker.calls)

    @unittest.skipUnless(shutil.which('node'), 'Node required for inline JavaScript scope verification')
    def test_inline_javascript_scope_checks_the_named_function(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = '<p>保留 😀</p><script>function parse_input(){return 0;} function other(){return 1;}</script>'
            (root / 'app.html').write_text(old)
            worker = FunctionWorker('worker', [
                {**result('F1','done',file='app.html'), 'touch':'app.html', 'source':old.replace('return 1', 'return 2')},
                {**result('F1','done',file='app.html'), 'touch':'app.html', 'source':old.replace('return 0', 'return 42')}])
            reviewer = ScriptedProvider('reviewer', [plan(task('F1','app.html')),
                result('F1','accepted',file='app.html'), result('F1','accepted',file='app.html')])
            self.run_monitor(root, worker, reviewer, max_rounds=2,
                checks=["python3 -c 'from pathlib import Path; assert \"return 42\" in Path(\"app.html\").read_text()'"])
            state = json.loads((root/'run/state.json').read_text())
            self.assertIn('Implement or change the assigned function', worker.calls[1]['prompt'])
            self.assertEqual(state['function_tasks']['accepted'][0]['verification'], {'check:1':'002-check-01.log'})

    @unittest.skipUnless(importlib.util.find_spec('tree_sitter_language_pack'),
                         'Install auto-moniter[languages] for tree-sitter scope verification')
    def test_registered_language_scope_is_selected_from_file_extension(self):
        from auto_moniter.function_gate import target_state
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = 'package api\n\nfunc Run() int { return 1 }\n'
            path = root / 'api.go'
            path.write_text(source)
            before = target_state(path, 'Run')
            path.write_text(source.replace('return 1', 'return 42'))
            after = target_state(path, 'Run')
            self.assertNotEqual(before['body'], after['body'])
            self.assertEqual(before['outside'], after['outside'])


class IntegrationDispatchTests(unittest.TestCase):
    CHECK = "python3 -c 'from client import load_items; assert load_items() == [42]'"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'app.py').write_text('def list_items():\n    return [1]\n')
        (self.root / 'client.py').write_text('from app import list_items\n\ndef load_items():\n    return list_items()\n')
        self.modules = _verdict('continue')['modules']
        self.scope = {'id':'pagination', 'module_id':'main', 'check_ids':['check:1'],
                      'targets':[{'file':'app.py','symbol':'list_items'},
                                 {'file':'client.py','symbol':'load_items'}]}

    def assignment(self, **overrides):
        item = task('I1', 'app.py')
        del item['file'], item['symbol']
        return {**item, 'kind':'integration', 'integration_scope':'pagination',
                'targets':self.scope['targets'], 'atomic_reason':'The client expects the new response structure',
                **overrides}

    def response(self, decision, **kwargs):
        outcome = result('I1', decision, **kwargs)
        outcome['verdict']['evidence'] = ['app.py', 'client.py']
        return outcome

    def monitor(self, worker, reviewer, *, approved=True, resume=False, run_dir=None, git_history='off', **kwargs):
        return Supervisor(RunConfig('' if resume else 'Paginate the client and server together', self.root,
            worker, reviewer=reviewer, workflow='review-first', function_dispatch=not resume, git_history=git_history,
            run_dir=run_dir or self.root/'run', review_every=0, retry_delay_s=0, resume=resume,
            modules=[] if resume else self.modules, checks=[] if resume else [self.CHECK],
            quality_policy={'integration_scopes':[self.scope]} if approved and not resume else {}, **kwargs),
            log=lambda _:None)

    def state(self):
        return json.loads((self.root/'run/state.json').read_text())

    def test_partial_extra_and_failed_regression_keep_the_same_assignment(self):
        server = 'def list_items():\n    return {"items": [42]}\n'
        prefix = 'from app import list_items\n\ndef load_items():\n'
        client = prefix + '    return list_items()["items"]\n'
        worker = FunctionWorker('worker', [
            {**self.response('done'), 'writes':{'app.py':server}},
            {**self.response('done'), 'writes':{'client.py':client, 'third.py':'extra = True\n'}},
            {**self.response('done'), 'writes':{'client.py':prefix + '    return []\n', 'third.py':None}},
            {**self.response('done'), 'writes':{'client.py':client}},
        ])
        next_task = task('F2','next.py',depends=['I1'],scope='requirement')
        reviewer = ScriptedProvider('reviewer', [plan(self.assignment()), self.response('accepted'),
            self.response('accepted'), self.response('accepted'), self.response('accepted', next_task=next_task)])
        report = self.monitor(worker, reviewer, max_rounds=4).run()
        self.assertEqual(report.status,'max_rounds',report.summary)
        self.assertIn('assigned source file did not change: client.py',worker.calls[1]['prompt'])
        self.assertIn('Restore changes outside this assignment: third.py',worker.calls[2]['prompt'])
        self.assertIn('Acceptance check failed',worker.calls[3]['prompt'])
        ledger = self.state()['function_tasks']
        self.assertEqual(ledger['current']['id'],'F2')
        self.assertEqual([a['task']['id'] for a in ledger['accepted']],['I1'])
        self.assertEqual(set(ledger['accepted'][0]['target_hashes']), {'app.py','client.py'})
        self.assertEqual(ledger['accepted'][0]['verification'], {'check:1':'004-check-01.log'})
        self.assertEqual(sorted(p.name for p in (self.root/'run').glob('*-check-*.log')),
                         ['003-check-01.log','004-check-01.log'])

    def test_missing_or_mismatched_approval_never_starts_worker(self):
        cases = [(False, {}), (True, {'targets':[self.scope['targets'][0],
                 {'file':'client.py','symbol':'another_function'}]}), (True, {'check_ids':['check:2']})]
        for i, (approved, overrides) in enumerate(cases):
            with self.subTest(approved=approved, overrides=overrides):
                worker = FunctionWorker('worker', [])
                reviewer = ScriptedProvider('reviewer', [plan(self.assignment(**overrides))])
                report = self.monitor(worker, reviewer, approved=approved, run_dir=self.root/f'run-{i}').run()
                self.assertEqual(report.status, 'action_required', report.summary)
                self.assertIn('matching preapproved scope', report.summary)
                self.assertFalse(worker.calls)

    def test_resume_keeps_original_baseline_and_approved_targets(self):
        assignment = self.assignment(delivery_scope='requirement', criterion_ids=['main:1','req:1:1','req:1:2'])
        worker = FunctionWorker('worker', [{**self.response('done',status='done'),
            'writes':{'app.py':'def list_items():\n    return {"items": [42]}\n'}}])
        reviewer = ScriptedProvider('reviewer', [plan(assignment), self.response('accepted',status='done')])
        monitor = self.monitor(worker, reviewer)
        emit = monitor._event
        def stop_on_rejection(kind, **payload):
            emit(kind, **payload)
            if kind == 'function_gate_rejected':
                monitor.request_stop()
        monitor._event = stop_on_rejection
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.state()['function_tasks']['accepted'], [])

        worker = FunctionWorker('worker', [{**self.response('done',status='done'),
            'writes':{'client.py':'from app import list_items\n\ndef load_items():\n    return list_items()["items"]\n'}}])
        reviewer = ScriptedProvider('reviewer', [self.response('accepted',status='done')])
        report = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'done', report.summary)
        entry, = self.state()['function_tasks']['accepted']
        self.assertEqual(entry['task']['id'], 'I1')
        self.assertEqual(set(entry['target_hashes']), {'app.py','client.py'})
        self.assertEqual(entry['verification'], {'check:1':'002-check-01.log'})

    def test_final_integration_publishes_both_files_in_one_commit(self):
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()
        git('init','-q')
        git('add','app.py','client.py')
        git('-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','initial')
        class GitReviewer(ScriptedProvider):
            def _command(self, session_id, prompt_file, read_only):
                match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt_file.read_text())
                if match:
                    self.scripts[0]['verdict']['evidence'].append(match[1])
                return super()._command(session_id, prompt_file, read_only)
        assignment = self.assignment(delivery_scope='requirement', criterion_ids=['main:1','req:1:1','req:1:2'])
        worker = FunctionWorker('worker',[{**self.response('done',status='done'), 'writes':{
            'app.py':'def list_items():\n    return {"items": [42]}\n',
            'client.py':'from app import list_items\n\ndef load_items():\n    return list_items()["items"]\n'}}])
        reviewer = GitReviewer('reviewer',[plan(assignment),self.response('accepted',status='done')])
        report = self.monitor(worker, reviewer, git_history='required').run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(git('rev-list','--count','HEAD'),'2')
        self.assertEqual(set(git('diff-tree','--no-commit-id','--name-only','-r','HEAD').splitlines()),
                         {'app.py','client.py'})


class DeliveryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.w={'role':'worker','index':2,'tree':'tree','scope':'module','module_ids':['M'],'status':'done'}
        self.r={**self.w,'role':'reviewer','status':'continue'}

    def test_stale_or_different_scope_is_not_disagreement(self):
        for other in [{'index':1},{'tree':'older'},{'module_ids':['N']},{'scope':'requirement'}]:
            self.assertEqual(diagnose([self.w,{**self.r,**other}])['code'],'no_comparable_review')

    def test_comparable_claims_and_watcher_result_are_separate(self):
        self.assertEqual(diagnose([self.w,self.r])['code'],'worker_reviewer_disagree')
        self.assertEqual(diagnose([{**self.w,'status':'continue'},self.r])['code'],'both_incomplete')
        self.assertEqual(diagnose([self.w,{**self.r,'status':'done'}])['code'],'joint_claim_delivery_unconfirmed')
        check={'round':2,'tree':'tree','checks':[{'status':'check_failed','exit_code':1}]}
        self.assertEqual(diagnose([self.w,{**self.r,'status':'done'}],acceptance=check)['code'],'watcher_gate_rejected')
        self.assertEqual(diagnose([self.w,{**self.r,'status':'done'}],acceptance={**check,'round':1})['code'],
                         'joint_claim_delivery_unconfirmed')
