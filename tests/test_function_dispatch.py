import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.delivery_diagnosis import diagnose
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


def task(ident, file, *, depends=(), scope='function'):
    return {'id':ident,'module_id':'main','file':file,'symbol':'parse_input',
            'objective':'Implement '+ident,'inputs':'Raw input','outputs':'Validated input',
            'boundaries':['Reject malformed values'],'verification':['Valid and invalid inputs produce distinct outcomes'],
            'depends_on':list(depends),'delivery_scope':scope}


def result(ident, decision, *, status='continue', file='app.py', next_task=None, gaps=()):
    return {'verdict':{**_verdict(status, gaps=list(gaps), evidence=[file]),
                       'modules':[], 'task_id':ident,'task_status':decision,'next_task':next_task}}


def plan(first):
    return {'verdict':{},'text':json.dumps({**BRIEF,'next_task':first,
            'modules':_verdict('continue')['modules']})}


class DispatchTests(unittest.TestCase):
    def run_monitor(self, root, worker, reviewer, **kwargs):
        return Supervisor(RunConfig('ORIGINAL BACKLOG MUST NOT REACH WORKER',root,worker,reviewer=reviewer,
            workflow='review-first',function_dispatch=True,git_history='off',run_dir=root/'run',
            review_every=0,retry_delay_s=0,**kwargs),log=lambda _:None).run()

    def test_review_repair_stays_on_first_function_then_dispatches_next(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=task('F1','app.py');second=task('F2','next.py',depends=['F1'],scope='requirement')
            w=ScriptedProvider('worker',[
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
            report=self.run_monitor(root,w,r,max_rounds=3,checks=['test -f next.py'])
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
            w=ScriptedProvider('worker',[{**result('F1','done'),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(task('F1','app.py')),result('F1','accepted',next_task=second)])
            s=Supervisor(RunConfig('task',root,w,reviewer=r,workflow='review-first',function_dispatch=True,
                git_history='off',run_dir=root/'run',retry_delay_s=0),log=lambda _:None)
            emit=s._event
            def stop(kind,**data):
                emit(kind,**data)
                if kind=='function_assigned' and data['task_id']=='F2':s.request_stop()
            s._event=stop
            self.assertEqual(s.run().status,'stopped')
            w=ScriptedProvider('worker',[{**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
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
            w=ScriptedProvider('worker',[{**result('F1','done'),'touch':'app.py'}])
            r=ScriptedProvider('reviewer',[plan(first),result('F1','accepted',next_task=second)])
            s=Supervisor(RunConfig('task',root,w,reviewer=r,workflow='review-first',function_dispatch=True,
                git_history='off',run_dir=root/'run',max_runtime_s=600,retry_delay_s=0),log=lambda _:None)
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
