"""A blocker is independently reconsidered, never silently approved."""
import json
from pathlib import Path
import tempfile
import unittest
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.directions import Directions
from auto_moniter import requirements_review, requirement_context
from auto_moniter.workspace import file_snapshot
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class AutonomousPlanningTests(unittest.TestCase):
    def run_case(self, reviews, *, max_calls=10, reviewer_class=ScriptedProvider):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = reviewer_class('reviewer', reviews)
        monitor = Supervisor(RunConfig('Implement requested behavior; keep external approval pending.', root,
            worker, reviewer=reviewer, workflow='review-first', run_dir=root/'run', git_history='off',
            max_rounds=1, max_agent_calls=max_calls, retry_delay_s=0), log=lambda _: None)
        return monitor.run(), worker, reviewer, root

    def blocked(self):
        return {**BRIEF, 'status':'needs_input', 'summary':'Need operator check IDs and compiler',
                'blockers':['Native compiler is unavailable; operator should register checks']}

    def brief(self, value):return {'verdict':{}, 'text':json.dumps(value)}

    def test_reconsidered_routine_blocker_can_dispatch_independent_work(self):
        report, worker, reviewer, root = self.run_case([
            self.brief(self.blocked()), self.brief(BRIEF), {'verdict':_verdict('done',evidence=['app.py'])}])
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(worker.calls),1)
        self.assertEqual(len(reviewer.calls),3)
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertIn('reassess this needs_input conclusion once',reviewer.calls[1]['prompt'])
        self.assertIn('independent authorized work',reviewer.calls[1]['prompt'])
        self.assertIn('no useful authorized next increment',reviewer.calls[0]['prompt'])
        events=[json.loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e.get('event')=='requirements_blocker_reassessment' or e.get('type')=='requirements_blocker_reassessment' for e in events),1)

    def test_genuine_decision_still_stops_without_any_worker_or_retry_loop(self):
        value={**self.blocked(),'blockers':['The owner must choose whether publication is authorized']}
        report,worker,reviewer,_=self.run_case([self.brief(value),self.brief(value)])
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])
        self.assertEqual(len(reviewer.calls),2)

    def test_call_budget_during_reconsideration_still_limits_execution(self):
        report,worker,reviewer,root=self.run_case([self.brief(self.blocked())],max_calls=1)
        self.assertEqual(report.status,'budget_exceeded',report.summary)
        self.assertEqual(worker.calls,[])
        self.assertTrue((root/'run/requirement-review-context.json').is_file())

    def test_invalid_reconsideration_does_not_auto_approve_original_block(self):
        report,worker,_,_=self.run_case([self.brief(self.blocked()),{'verdict':{},'text':'invalid'}])
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])


    def test_ready_report_with_deferred_blockers_is_repaired_without_human_stop(self):
        invalid={**BRIEF,'blockers':['Later native verification is unavailable']}
        fixed={**BRIEF,'constraints':BRIEF['constraints']+invalid['blockers']}
        report,worker,reviewer,root=self.run_case([self.brief(invalid),self.brief(fixed),
            {'verdict':_verdict('done',evidence=['app.py'])}])
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(worker.calls),1)
        self.assertTrue(all(c['read_only'] for c in reviewer.calls))
        self.assertIn('Correct only the report contradiction',reviewer.calls[1]['prompt'])
        self.assertIn('Later native verification is unavailable',worker.calls[0]['prompt'])
        self.assertTrue(list((root/'run').glob('000-reviewer-repair-*-text.md')))

    def test_repeated_report_contradiction_is_never_silently_normalized(self):
        invalid={**BRIEF,'blockers':['A real unresolved decision']}
        report,worker,reviewer,root=self.run_case([self.brief(invalid),self.brief(invalid)])
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])
        self.assertEqual(len(reviewer.calls),2)
        saved=json.loads((root/'run/requirement-review-context.json').read_text())
        self.assertEqual(saved['response']['blockers'],invalid['blockers'])
        with self.assertRaises(ValueError): requirements_review.parse(json.dumps(saved['response']))

    def test_report_repair_can_confirm_genuine_needs_input(self):
        invalid={**BRIEF,'blockers':['Choose required external authority']}
        blocked={**invalid,'status':'needs_input'}
        report,worker,reviewer,_=self.run_case([self.brief(invalid),self.brief(blocked)])
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])
        self.assertEqual(len(reviewer.calls),2)

    def test_new_amendment_during_review_supersedes_even_a_contradictory_old_report(self):
        class AmendingReviewer(ScriptedProvider):
            def _command(self,session,prompt_file,read_only):
                if not self.calls: Directions(prompt_file.parent).append('Owner confirmed source and feature acceptance.')
                return super()._command(session,prompt_file,read_only)
        invalid={**BRIEF,'blockers':['Owner has not confirmed source acceptance']}
        fixed={**BRIEF,'constraints':['Owner confirmed source and feature acceptance.']}
        report,worker,reviewer,root=self.run_case([self.brief(invalid),self.brief(fixed),
            {'verdict':_verdict('done',evidence=['app.py'])}],reviewer_class=AmendingReviewer)
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual(len(worker.calls),1)
        self.assertIn('Owner confirmed source and feature acceptance.',reviewer.calls[1]['prompt'])
        self.assertIn('Owner confirmed source and feature acceptance.',worker.calls[0]['prompt'])
        state=json.loads((root/'run/state.json').read_text())
        self.assertEqual(state['last_worker_invocation']['requirement_revision']['version'],2)
        events=[json.loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
        self.assertTrue(any(event['event']=='requirements_input_retry' for event in events))
        self.assertEqual(sum(event['event']=='requirements_report_repair' for event in events),0)

    def test_amendment_never_masks_readonly_workspace_violation(self):
        class AmendingReviewer(ScriptedProvider):
            def _command(self,session,prompt_file,read_only):
                if not self.calls: Directions(prompt_file.parent).append('Latest request')
                return super()._command(session,prompt_file,read_only)
        report,worker,reviewer,_=self.run_case([{**self.brief(BRIEF),'touch':'unauthorized.py'}],reviewer_class=AmendingReviewer)
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('Workspace changed',report.summary)
        self.assertEqual(worker.calls,[])
        self.assertEqual(len(reviewer.calls),1)

    def test_latest_failed_report_is_reused_instead_of_older_blocked_analysis(self):
        report,_,_,root=self.run_case([self.brief(self.blocked()),self.brief(self.blocked())])
        run=root/'run'
        latest={**BRIEF,'blockers':['Deferred native environment'], 'summary':'Research completed and concrete plan prepared'}
        (run/'000-reviewer-text.md').write_text(json.dumps(latest))
        context=requirement_context.load(run,root,'Implement requested behavior; keep external approval pending.',1,
            file_snapshot(root,ignore_dir=run),[],[],previous={'status':'failed','summary':'Report mismatch',
                                                           'output_file':'000-reviewer-text.md'})
        self.assertEqual(context['previous_response'],latest)
        self.assertIn('latest rejected',context['source'])
        self.assertIn('no acceptance',context['approval'])


    def test_full_function_plan_survives_report_repair_before_any_implementation(self):
        from tests.test_function_dispatch import FunctionWorker, task, result
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'app.py').write_text('def parse_input():\n    return 0\n')
            assignment=task('F1','app.py',scope='requirement')
            assignment.update(criterion_ids=['main:1','req:1:1','req:1:2'],
                              test_files=['test_new.py'],check_ids=['auto:1'])
            invalid={**BRIEF,'next_task':assignment,'modules':_verdict('continue')['modules'],
                     'function_plan':[assignment],'blockers':['Deferred external review']}
            fixed={**invalid,'blockers':[],'constraints':['Deferred external review is not marked satisfied.']}
            worker=FunctionWorker('worker',[{**result('F1','done',status='done'),
                'writes':{'app.py':'def parse_input():\n    return 42\n',
                          'test_new.py':'import unittest\nfrom app import parse_input\nclass TestNew(unittest.TestCase):\n def test_result(self): self.assertEqual(parse_input(),42)\n'}}])
            reviewer=ScriptedProvider('reviewer',[self.brief(invalid),self.brief(fixed),
                result('F1','accepted',status='done')])
            report=Supervisor(RunConfig('Implement the feature',root,worker,reviewer=reviewer,
                workflow='review-first',function_dispatch=True,full_function_plan=True,
                git_history='off',run_dir=root/'run',max_rounds=1,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'done',report.summary)
            self.assertEqual(len(worker.calls),1)
            self.assertIn('"id": "F1"',worker.calls[0]['prompt'])
            self.assertEqual(json.loads((root/'run/state.json').read_text())['function_plan']['tasks'],[assignment])
