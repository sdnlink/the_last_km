"""Resumption continues recorded analysis without dispatching unapproved work."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import requirement_context
from auto_moniter.directions import Directions
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.verdict import Verdict
from auto_moniter.workspace import file_snapshot
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class RequirementReviewResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root/'run'
        (self.root/'app.py').write_text('def parse_input():\n    return 0\n')
        self.original = 'Implement the original feature with all acceptance criteria.'

    def monitor(self, worker, reviewer, *, resume=False, function_dispatch=True, **kwargs):
        return Supervisor(RunConfig('' if resume else self.original,self.root,worker,
            reviewer=reviewer,workflow='review-first',function_dispatch=function_dispatch,
            git_history='off',run_dir=self.run,retry_delay_s=0,max_rounds=3,
            checks=[CHECK],resume=resume,**kwargs),log=lambda _:None)

    def final_task(self,ident='F1'):
        value=task(ident,'app.py',scope='requirement')
        value['criterion_ids']=['main:1','req:1:1','req:1:2']
        return value

    def worker(self):
        return FunctionWorker('worker',[{**result('F1','done',status='done'),'touch':'app.py',
            'source':'def parse_input():\n    return 42\n'}])

    def reject_plan(self):
        worker=ScriptedProvider('worker',[])
        reviewer=ScriptedProvider('reviewer',[plan(self.final_task('task:1')),plan(self.final_task('task:1'))])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])
        return reviewer

    def resume_delivery(self):
        worker=self.worker()
        reviewer=ScriptedProvider('reviewer',[plan(self.final_task()),result('F1','accepted',status='done')])
        report=self.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(worker.calls),1)
        return reviewer.calls[0]['prompt']

    def context(self,prompt):
        marker='Saved requirement analysis and input changes (context, not approval):\n'
        return json.JSONDecoder().raw_decode(prompt.split(marker,1)[1])[0]

    def test_invalid_assignment_resume_repairs_saved_analysis_without_full_review(self):
        self.reject_plan()
        prompt=self.resume_delivery()
        self.assertIn('Do not restart a full repository review',prompt)
        self.assertNotIn('Before any worker implementation, read the project and elaborate',prompt)
        context=self.context(prompt)
        self.assertFalse(context['workspace_changed'])
        self.assertEqual(context['changed_files'],[])
        self.assertFalse(context['checks_changed'])
        self.assertIn('Invalid function assignment id',context['rejection'])
        self.assertEqual(context['previous_response']['acceptance_criteria'],BRIEF['acceptance_criteria'])
        self.assertEqual(context['previous_response']['next_task']['id'],'task:1')

    def test_resume_highlights_changed_added_removed_and_permission_only_files(self):
        (self.root/'removed.py').write_text('old')
        (self.root/'mode.py').write_text('same')
        self.reject_plan()
        (self.root/'app.py').write_text('def parse_input():\n    return 9\n')
        (self.root/'added.py').write_text('new')
        (self.root/'removed.py').unlink()
        (self.root/'mode.py').chmod((self.root/'mode.py').stat().st_mode ^ 0o100)
        context=self.context(self.resume_delivery())
        self.assertTrue(context['workspace_changed'])
        self.assertEqual(context['changed_files'],['added.py','app.py','mode.py','removed.py'])
        self.assertEqual(context['changed_file_count'],4)

    def test_needs_input_resume_keeps_findings_and_focuses_on_new_amendment(self):
        blocked={**BRIEF,'status':'needs_input','blockers':['Confirm the test environment'],
                 'summary':'Inspect only the remaining environment prerequisite'}
        worker=ScriptedProvider('worker',[])
        reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(blocked)}])
        report=self.monitor(worker,reviewer,function_dispatch=False).run()
        self.assertEqual(report.status,'action_required',report.summary)
        direction=Directions(self.run).append('The isolated test environment is ready.')
        worker=FunctionWorker('worker',[{'verdict':_verdict('done',evidence=['app.py']),
            'touch':'app.py','source':'def parse_input():\n    return 42\n'}])
        reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done',evidence=['app.py'])}])
        report=self.monitor(worker,reviewer,resume=True,function_dispatch=False).run()
        self.assertEqual(report.status,'done',report.summary)
        context=self.context(reviewer.calls[0]['prompt'])
        self.assertEqual(context['previous_response']['blockers'],blocked['blockers'])
        self.assertEqual(context['new_direction_ids'],[direction['id']])
        self.assertFalse(context['workspace_changed'])
        self.assertIn('The isolated test environment is ready.',reviewer.calls[0]['prompt'])

    def test_legacy_failure_uses_full_response_as_unapproved_context(self):
        self.reject_plan()
        (self.run/'requirement-review-context.json').unlink()
        context=self.context(self.resume_delivery())
        self.assertEqual(context['previous_response']['next_task']['id'],'task:1')
        self.assertIsNone(context['changed_files'])
        self.assertTrue(context['workspace_changed'])
        self.assertIn('legacy reviewer artifact',context['source'])
        self.assertIn('no acceptance',context['approval'])

    def test_call_budget_interruption_keeps_completed_analysis_for_resume(self):
        reviewer=ScriptedProvider('reviewer',[plan(self.final_task('task:1'))])
        report=self.monitor(ScriptedProvider('worker',[]),reviewer,max_agent_calls=1).run()
        self.assertEqual(report.status,'budget_exceeded',report.summary)
        self.assertTrue((self.run/'requirement-review-context.json').exists())
        context=self.context(self.resume_delivery_with_budget())
        self.assertEqual(context['previous_response']['next_task']['id'],'task:1')
        self.assertFalse(context['workspace_changed'])

    def resume_delivery_with_budget(self):
        reviewer=ScriptedProvider('reviewer',[plan(self.final_task()),result('F1','accepted',status='done')])
        report=self.monitor(self.worker(),reviewer,resume=True,max_agent_calls=10).run()
        self.assertEqual(report.status,'done',report.summary)
        return reviewer.calls[0]['prompt']

    def test_context_never_substitutes_for_a_new_validated_plan(self):
        self.reject_plan()
        reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':'unavailable','exit_code':1}])
        worker=ScriptedProvider('worker',[])
        report=self.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])
        state=json.loads((self.run/'state.json').read_text())
        self.assertIsNone(state['function_tasks']['current'])
        self.assertEqual(state['function_tasks']['accepted'],[])

    def test_valid_ready_requirement_resume_skips_requirement_review(self):
        worker=FunctionWorker('worker',[{**result('F1','incomplete'),'touch':'app.py'}])
        reviewer=ScriptedProvider('reviewer',[plan(self.final_task())])
        monitor=self.monitor(worker,reviewer)
        monitor.log=lambda message:monitor.request_stop() if 'worker status=' in message else None
        report=monitor.run()
        self.assertEqual(report.status,'stopped',report.summary)
        reviewer=ScriptedProvider('reviewer',[result('F1','accepted',status='done')])
        report=self.monitor(self.worker(),reviewer,resume=True).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(reviewer.calls),1)
        self.assertIn('Dispatched assignment:',reviewer.calls[0]['prompt'])
        self.assertNotIn('Saved requirement analysis and input changes',reviewer.calls[0]['prompt'])

    def test_next_function_planning_continues_approved_brief(self):
        first=task('F1','app.py')
        second=task('F2','next.py',scope='requirement',depends=['F1'])
        second['criterion_ids']=['main:1','req:1:1','req:1:2']
        worker=FunctionWorker('worker',[{**result('F1','done'),'touch':'app.py'},
            {**result('F2','done',status='done',file='next.py'),'touch':'next.py'}])
        reviewer=ScriptedProvider('reviewer',[plan(first),result('F1','accepted'),plan(second),
            result('F2','accepted',status='done',file='next.py')])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertNotIn('Do not restart a full repository review',reviewer.calls[0]['prompt'])
        self.assertIn('Do not restart a full repository review',reviewer.calls[2]['prompt'])
        self.assertIn('Accepted function records:',reviewer.calls[2]['prompt'])
        self.assertIn('F1',reviewer.calls[2]['prompt'])

    def test_wrong_original_or_cycle_context_is_not_reused(self):
        self.reject_plan()
        snapshot=file_snapshot(self.root,ignore_dir=self.run)
        for request,cycle in [('Different request',1),(self.original,2)]:
            with self.subTest(request=request,cycle=cycle):
                self.assertIsNone(requirement_context.load(self.run,self.root,request,cycle,snapshot,[CHECK],[]))

    def test_regular_followup_review_reuses_completed_audit(self):
        monitor=self.monitor(ScriptedProvider('worker',[]),ScriptedProvider('reviewer',[]),
            function_dispatch=False)
        monitor._audit_feedback=Verdict('continue',summary='Existing audit found one defect')
        prompt=monitor._review_prompt(2,Verdict('done'),self.run/'worker.log','tree')
        self.assertIn('A fresh reviewer session or task resume is not a reason to repeat',prompt)
        self.assertNotIn('Inspect ALL execution entry points',prompt)
