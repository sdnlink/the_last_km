"""Complete reviewer plans remain distinct from the single active dispatch."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter import function_plan
from auto_moniter.function_tasks import FunctionTasks
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_dispatch import FunctionWorker, task, plan, result, CHECK
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


def tasks():
    first = task('F1', 'a.py')
    final = task('F2', 'b.py', depends=['F1'], scope='requirement')
    return first, final


def complete_plan(first, final):
    value = plan(first)
    parsed = json.loads(value['text'])
    parsed['function_plan'] = [first, final]
    value['text'] = json.dumps(parsed)
    return value


class CompleteFunctionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        for name in ('a.py', 'b.py'):
            (self.root / name).write_text('def parse_input():\n    return 0\n')

    def monitor(self, worker, reviewer, resume=False, **extra):
        return Supervisor(RunConfig('' if resume else 'Implement both functions.', self.root, worker,
            reviewer=reviewer, workflow='auto' if resume else 'review-first', full_function_plan=True,
            function_dispatch=True, run_dir=self.run, resume=resume, git_history='off',
            checks=[CHECK], max_rounds=5, retry_delay_s=0, **extra), log=lambda _: None)

    def worker(self):
        return FunctionWorker('worker', [{**result('F1', 'done', file='a.py'), 'touch':'a.py'},
            {**result('F2', 'done', file='b.py', status='done'), 'touch':'b.py'}])

    def test_full_plan_saved_before_worker_and_serially_claimed_after_acceptance(self):
        first, final = tasks()
        root = self.run
        class ObservingWorker(FunctionWorker):
            def _command(inner, *args, **kwargs):
                saved = json.loads((root / 'state.json').read_text())
                self.assertEqual([task['id'] for task in saved['function_plan']['tasks']], ['F1', 'F2'])
                return super()._command(*args, **kwargs)
        worker = ObservingWorker('worker', self.worker().scripts)
        reviewer = ScriptedProvider('reviewer', [complete_plan(first, final),
            result('F1', 'accepted', file='a.py'), result('F2', 'accepted', file='b.py', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(reviewer.calls), 3)
        self.assertIn('COMPLETE ordered list', reviewer.calls[0]['prompt'])
        self.assertNotIn('"id": "F2"', worker.calls[0]['prompt'])
        saved = json.loads((self.run / 'state.json').read_text())
        self.assertEqual([item['task']['id'] for item in saved['function_tasks']['accepted']], ['F1', 'F2'])
        self.assertTrue(saved['full_function_plan'])

    def test_missing_incomplete_or_invalid_full_plan_never_starts_worker(self):
        first, final = tasks()
        values = []
        for changes in ({'function_plan':None}, {'function_plan':[first]},
                        {'function_plan':[final, first]}, {'function_plan':[first, first, final]}):
            value = json.loads(complete_plan(first, final)['text']);value.update(changes);values.append(value)
        bad = json.loads(complete_plan(first, final)['text']);bad['function_plan'][1]['check_ids']=['invented:1'];values.append(bad)
        for index, value in enumerate(values):
            with self.subTest(index=index):
                self.run = self.root / ('invalid-' + str(index))
                worker = ScriptedProvider('worker', [])
                reviewer = ScriptedProvider('reviewer', [{'verdict':{},'text':json.dumps(value)} for _ in range(2)])
                report = self.monitor(worker, reviewer).run()
                self.assertEqual(report.status, 'action_required', report.summary)
                self.assertEqual(worker.calls, [])

    def test_resume_keeps_current_assignment_and_pending_plan_without_replanning(self):
        first, final = tasks()
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [complete_plan(first, final), result('F1', 'accepted', file='a.py')])
        monitor = self.monitor(worker, reviewer)
        monitor.log = lambda message: monitor.request_stop() if 'worker status=' in message else None
        self.assertEqual(monitor.run().status, 'stopped')
        saved = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(saved['function_tasks']['current']['id'], 'F1')
        self.assertEqual(len(saved['function_plan']['tasks']), 2)
        worker = FunctionWorker('worker', [{**result('F2','done',file='b.py',status='done'),'touch':'b.py'}])
        reviewer = ScriptedProvider('reviewer', [result('F1','accepted',file='a.py'),
            result('F2','accepted',file='b.py',status='done')])
        resumed = self.monitor(worker, reviewer, resume=True)
        resumed.config.full_function_plan = False
        report = resumed.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertTrue(resumed.config.full_function_plan)
        self.assertEqual(len(worker.calls),1)
        self.assertNotIn('COMPLETE ordered list', reviewer.calls[0]['prompt'])

    def test_plan_validation_rejects_dependency_cycles_and_missing_criteria(self):
        first, final = tasks()
        modules = {item['id']:item for item in _verdict('continue')['modules']}
        ledger = FunctionTasks()
        bad = copy.deepcopy(first);bad['depends_on']=['F2']
        with self.assertRaisesRegex(ValueError, 'dependencies'):
            function_plan.validate([bad, final], ledger, modules, BRIEF)
        bad = copy.deepcopy(final);bad['criterion_ids']=['main:1']
        with self.assertRaisesRegex(ValueError, 'unplanned'):
            function_plan.validate([first,bad],ledger,modules,BRIEF)

    def test_existing_incremental_task_gains_full_plan_without_replacing_its_current_assignment(self):
        first, final = tasks()
        worker = FunctionWorker('worker',[{**result('F1','incomplete',file='a.py'),'touch':'a.py'}])
        reviewer = ScriptedProvider('reviewer',[plan(first)])
        monitor = self.monitor(worker,reviewer);monitor.config.full_function_plan=False
        monitor.log=lambda message:monitor.request_stop() if 'worker status=' in message else None
        self.assertEqual(monitor.run().status,'stopped')
        saved=json.loads((self.run/'state.json').read_text())
        self.assertIsNone(saved['function_plan']);self.assertEqual(saved['function_tasks']['current']['id'],'F1')
        planned=complete_plan(first,final);value=json.loads(planned['text']);value['modules']=[];planned['text']=json.dumps(value)
        worker=self.worker();reviewer=ScriptedProvider('reviewer',[planned,result('F1','accepted',file='a.py'),
            result('F2','accepted',file='b.py',status='done')])
        report=self.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertIn('COMPLETE ordered list',reviewer.calls[0]['prompt'])
        self.assertNotIn('Use this exact next_task or null',reviewer.calls[0]['prompt'])
        self.assertIn('"id": "F1"',worker.calls[0]['prompt'])

    def test_reviewer_cannot_jump_to_an_unplanned_assignment_without_replanning(self):
        first, final = tasks();extra=task('F3','c.py',depends=['F1'])
        (self.root/'c.py').write_text('def parse_input():\n    return 0\n')
        revised=copy.deepcopy(final);revised['depends_on']=['F3']
        replanning=complete_plan(extra,revised);value=json.loads(replanning['text']);value['modules']=[];replanning['text']=json.dumps(value)
        worker=FunctionWorker('worker',[{**result('F1','done',file='a.py'),'touch':'a.py'},
            {**result('F3','done',file='c.py'),'touch':'c.py'},
            {**result('F2','done',file='b.py',status='done'),'touch':'b.py'}])
        reviewer=ScriptedProvider('reviewer',[complete_plan(first,final),result('F1','accepted',file='a.py',next_task=extra),
            replanning,result('F3','accepted',file='c.py'),result('F2','accepted',file='b.py',status='done')])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertIn('COMPLETE ordered list',reviewer.calls[2]['prompt'])
        saved=json.loads((self.run/'state.json').read_text())
        self.assertEqual([item['task']['id'] for item in saved['function_tasks']['accepted']],['F1','F3','F2'])


    def test_known_cross_module_references_are_valid_without_expanding_scope(self):
        from auto_moniter.function_gate import validate_task
        first,final=tasks()
        modules={item['id']:item for item in _verdict('continue')['modules']}
        modules['recap']={'id':'recap','title':'Recap','acceptance':['Record goals'],
                          'checks':['echo separately approved check']}
        first['criterion_ids'].append('recap:1')
        final['criterion_ids']=['main:1','recap:1','req:1:1','req:1:2']
        value=function_plan.validate([first,final],FunctionTasks(),modules,BRIEF)
        self.assertEqual(value['tasks'][0]['file'],'a.py')
        self.assertEqual(value['tasks'][0]['symbol'],'parse_input')
        validate_task(first,self.root,modules,[CHECK],BRIEF)
        bad=copy.deepcopy(first);bad['check_ids']=['recap:check:1']
        with self.assertRaisesRegex(ValueError,'approved check IDs'):
            validate_task(bad,self.root,modules,[CHECK],BRIEF)
        bad=copy.deepcopy(first);bad['criterion_ids']=['recap:1']
        with self.assertRaisesRegex(ValueError,'its module criteria'):
            validate_task(bad,self.root,modules,[CHECK],BRIEF)

    def test_plan_reports_all_unknown_criteria_instead_of_stopping_at_first(self):
        first,final=tasks()
        modules={item['id']:item for item in _verdict('continue')['modules']}
        first['criterion_ids'].append('missing:1')
        final['criterion_ids'].append('main:999')
        with self.assertRaises(ValueError) as caught:
            function_plan.validate([first,final],FunctionTasks(),modules,BRIEF)
        for text in ['F1','missing:1','F2','main:999']:
            self.assertIn(text,str(caught.exception))

    def test_invalid_future_plan_is_repaired_readonly_before_worker_dispatch(self):
        first,final=tasks()
        invalid=complete_plan(first,final)
        value=json.loads(invalid['text']);value['function_plan'][1]['criterion_ids'].append('missing:1')
        invalid['text']=json.dumps(value)
        reviewer=ScriptedProvider('reviewer',[invalid,complete_plan(first,final),
            result('F1','accepted',file='a.py'),result('F2','accepted',file='b.py',status='done')])
        worker=self.worker()
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertIn('missing:1',reviewer.calls[1]['prompt'])
        self.assertIn('plan validation errors',reviewer.calls[1]['prompt'])
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertEqual(len(worker.calls),2)

    def test_preflight_does_not_leak_partial_modules_or_dispatch_when_plan_is_invalid(self):
        first,final=tasks()
        value=json.loads(complete_plan(first,final)['text'])
        value['function_plan'][1]['check_ids']=['check:999']
        reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(value)} for _ in range(2)])
        worker=ScriptedProvider('worker',[])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertEqual(worker.calls,[])
        saved=json.loads((self.run/'state.json').read_text())
        self.assertIsNone(saved['function_tasks']['current'])
        self.assertIsNone(saved['function_plan'])
        self.assertEqual(saved['modules'],[])


if __name__ == '__main__':
    unittest.main()
