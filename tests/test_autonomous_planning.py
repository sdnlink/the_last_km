"""A blocker is independently reconsidered, never silently approved."""
import json
from pathlib import Path
import tempfile
import unittest
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class AutonomousPlanningTests(unittest.TestCase):
    def run_case(self, reviews, *, max_calls=10):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', reviews)
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
