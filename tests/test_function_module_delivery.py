"""Final function reports jointly confirm the whole requirement's module ledger."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_supervisor import ScriptedProvider


MODULES = [
    {'id':'A', 'title':'Input model', 'acceptance':['Input model works']},
    {'id':'B', 'title':'Integrated product', 'acceptance':['Product integrates the model'], 'depends_on':['A']},
]


def assignment(ident, file, module, *, final=False):
    value = task(ident, file, depends=['F1'] if final else [], scope='requirement' if final else 'function')
    value.update(module_id=module, criterion_ids=[module + ':1', 'req:1:2' if final else 'req:1:1'])
    return value


def conclusion(ident, decision, ids, *, final=False, file='app.py', next_task=None):
    value = result(ident, decision, status='done' if final else 'continue', file=file, next_task=next_task)
    value['verdict']['module_ids'] = ids
    return value


class FunctionModuleDeliveryTests(unittest.TestCase):
    def test_large_final_criterion_sets_have_matching_schema_and_validation_limits(self):
        from auto_moniter import function_tasks
        from auto_moniter.verdict import VERDICT_SCHEMA
        value=copy.deepcopy(self.final)
        value['criterion_ids']=['B:'+str(index) for index in range(1,45)]
        self.assertEqual(len(function_tasks.validate(value)['criterion_ids']),44)
        schema=function_tasks.schema(VERDICT_SCHEMA,gate=True,integration=True)
        for variant in schema['properties']['next_task']['anyOf'][1:]:
            for key,limit in function_tasks.GATE_LIST_LIMITS.items():
                self.assertEqual(variant['properties'][key]['maxItems'],limit)
        for key,limit in function_tasks.GATE_LIST_LIMITS.items():
            invalid=copy.deepcopy(value);invalid[key]=[f'item-{index}' for index in range(limit+1)]
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'Invalid function assignment '+key):
                function_tasks.validate(invalid)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = assignment('F1', 'app.py', 'A')
        self.final = assignment('F2', 'next.py', 'B', final=True)

    def planning(self):
        value = plan(self.first)
        brief = json.loads(value['text'])
        brief['modules'] = copy.deepcopy(MODULES)
        value['text'] = json.dumps(brief)
        return value

    def workers(self, final_ids=None):
        return FunctionWorker('worker', [
            {**conclusion('F1', 'done', ['A']), 'touch':'app.py'},
            {**conclusion('F2', 'done', ['A','B'] if final_ids is None else final_ids,
                          final=True, file='next.py'), 'touch':'next.py'},
        ])

    def reviewers(self, final_ids=None):
        return ScriptedProvider('reviewer', [self.planning(),
            conclusion('F1', 'accepted', [], next_task=self.final),
            conclusion('F2', 'accepted', ['A','B'] if final_ids is None else final_ids,
                       final=True, file='next.py'),
        ])

    def monitor(self, worker, reviewer, **options):
        return Supervisor(RunConfig('Build both modules', self.root, worker, reviewer=reviewer,
            workflow='review-first', function_dispatch=True, git_history='off', run_dir=self.root/'run',
            max_rounds=2, retry_delay_s=0, checks=[CHECK], **options), log=lambda _:None)

    def test_final_assignment_accepts_pending_prerequisite_modules_with_verified_function_coverage(self):
        worker, reviewer = self.workers(), self.reviewers()
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (2,3))
        saved = json.loads((self.root/'run/state.json').read_text())
        self.assertEqual([item['status'] for item in saved['modules']], ['accepted','accepted'])
        self.assertEqual([item['task']['id'] for item in saved['function_tasks']['accepted']], ['F1','F2'])
        for prompt in (worker.calls[-1]['prompt'], reviewer.calls[-1]['prompt']):
            self.assertIn('module_ids=["A", "B"]', prompt)
            self.assertIn('not file edit permissions', prompt)

    def test_legacy_final_report_recovers_read_only_without_repeating_implementation(self):
        worker, reviewer = self.workers(final_ids=['B']), self.reviewers(final_ids=['B'])
        reviewer.scripts.append(conclusion('F2', 'accepted', ['B'], final=True, file='next.py'))
        old_monitor = self.monitor(worker, reviewer)
        # Reproduce the old current-module-only final report policy.
        with patch.object(old_monitor, '_validate_function_worker', return_value=[]):
            stopped = old_monitor.run()
        self.assertEqual(stopped.status, 'action_required', stopped.summary)
        self.assertIn('function handoff', stopped.summary.lower())
        files = {name:(self.root/name).read_bytes() for name in ('app.py','next.py')}
        repair = FunctionWorker('worker', [conclusion('F2', 'done', ['A','B'], final=True, file='next.py')])
        review = ScriptedProvider('reviewer', [conclusion('F2', 'accepted', ['A','B'], final=True, file='next.py')])
        resumed = self.monitor(repair, review, resume=True).run()
        self.assertEqual(resumed.status, 'done', resumed.summary)
        self.assertEqual((len(repair.calls), len(review.calls)), (1,1))
        self.assertTrue(repair.calls[0]['read_only'])
        self.assertIn('Worker report correction only', repair.calls[0]['prompt'])
        for name, expected in files.items():
            self.assertEqual((self.root/name).read_bytes(), expected)
        originals = list((self.root/'run').glob('002-worker-report-original-*-text.md'))
        self.assertEqual(len(originals), 1)
        self.assertEqual(json.loads(originals[0].read_text())['module_ids'], ['B'])

    def test_unknown_or_omitted_modules_do_not_validate_final_worker_completion(self):
        monitor = self.monitor(self.workers(), self.reviewers())
        from auto_moniter.function_tasks import FunctionTasks
        from auto_moniter.modules import ModuleLedger
        from auto_moniter.verdict import parse_verdict
        monitor.modules = ModuleLedger(MODULES)
        monitor.function_tasks = FunctionTasks()
        monitor.function_tasks.current = self.final
        for ids in (['B'], ['A'], ['A','B','unknown'], ['A','B','B']):
            with self.subTest(ids=ids):
                value = parse_verdict(json.dumps(conclusion('F2','done',ids,final=True)['verdict']),
                                      function_gate=True, worker_submission=True)
                self.assertTrue(monitor._validate_function_worker(value))
                self.assertEqual(value.task_status, 'incomplete')

    def test_missing_final_review_modules_get_one_read_only_correction(self):
        worker, reviewer = self.workers(), self.reviewers(final_ids=['B'])
        reviewer.scripts.append(conclusion('F2','accepted',['A','B'],final=True,file='next.py'))
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,4))
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertIn('Reviewer-only repair of the SAME candidate', reviewer.calls[-1]['prompt'])
        self.assertIn('module_ids=["A", "B"]', reviewer.calls[-1]['prompt'])
        self.assertEqual(len(list((self.root/'run').glob('002-reviewer-repair-*-text.md'))), 1)

    def test_reviewer_task_id_typo_is_repaired_without_another_worker(self):
        worker, reviewer = self.workers(), self.reviewers()
        correct = copy.deepcopy(reviewer.scripts[-1])
        reviewer.scripts[-1]['verdict']['task_id'] = 'wrong-task'
        reviewer.scripts.append(correct)
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,4))
        self.assertIn('dispatched function ID', reviewer.calls[-1]['prompt'])

    def test_repeated_invalid_review_stops_after_one_correction_and_preserves_candidate(self):
        worker, reviewer = self.workers(), self.reviewers(final_ids=['B'])
        reviewer.scripts.append(conclusion('F2','accepted',['B'],final=True,file='next.py'))
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('Invalid function handoff report', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,4))
        self.assertIn('return 42', (self.root/'next.py').read_text())
        saved = json.loads((self.root/'run/state.json').read_text())
        self.assertTrue(all(item['status']=='pending' for item in saved['modules']))
        self.assertEqual([x['task']['id'] for x in saved['function_tasks']['accepted']], ['F1'])

    def test_report_correction_honors_the_original_call_budget(self):
        worker, reviewer = self.workers(), self.reviewers(final_ids=['B'])
        report = self.monitor(worker, reviewer, max_agent_calls=5).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,3))
        self.assertTrue((self.root/'next.py').exists())

    def test_final_dispatch_still_rejects_uncovered_prerequisite_criterion(self):
        planning = self.planning()
        brief = json.loads(planning['text'])
        brief['modules'][0]['acceptance'].append('Required uncovered failure path')
        planning['text'] = json.dumps(brief)
        worker = FunctionWorker('worker', [{**conclusion('F1','done',['A']), 'touch':'app.py'}])
        reviewer = ScriptedProvider('reviewer', [planning,
            conclusion('F1','accepted',[],next_task=self.final)])
        monitor = self.monitor(worker, reviewer)
        original_control = monitor._process_control
        def stop_after_rejection(active=False):
            if monitor.function_tasks.accepted:
                monitor.request_stop()
            return original_control(active)
        monitor._process_control = stop_after_rejection
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(len(worker.calls), 1)
        saved = json.loads((self.root/'run/state.json').read_text())
        self.assertIn('A:2', saved['planning_rejection']['reason'])
        self.assertTrue(all(item['status']=='pending' for item in saved['modules']))
