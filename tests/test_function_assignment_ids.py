"""Malformed reviewer identifiers get one correction before dispatching work."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from auto_moniter import function_tasks, requirements_review
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.verdict import VERDICT_SCHEMA
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider


def final_task(ident='F1', file='app.py', **kwargs):
    value = task(ident, file, scope='requirement', **kwargs)
    value['criterion_ids'] = ['main:1', 'req:1:1', 'req:1:2']
    return value


class AssignmentIdentifierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root/'run'

    def monitor(self, worker, reviewer, **kwargs):
        return Supervisor(RunConfig('Implement the original request', self.root, worker,
            reviewer=reviewer, workflow='review-first', function_dispatch=True,
            git_history='off', run_dir=self.run, retry_delay_s=0, max_rounds=3,
            checks=[CHECK], **kwargs), log=lambda _:None)

    def worker(self, ident='F1', file='app.py'):
        return FunctionWorker('worker', [{**result(ident, 'done', status='done', file=file),
            'touch':file, 'source':'def parse_input():\n    return 42\n'}])

    def test_provider_schema_rejects_colons_in_all_assignment_variants(self):
        for planning in (False, True):
            base = requirements_review.SCHEMA if planning else VERDICT_SCHEMA
            schema = function_tasks.schema(base, planning=planning, gate=True, integration=True)
            variants = schema['properties']['next_task']['anyOf'][1:]
            self.assertEqual(len(variants), 3)
            for variant in variants:
                for field in ['id', 'module_id', *(['integration_scope'] if 'integration_scope' in variant['properties'] else [])]:
                    constraint = variant['properties'][field]
                    for value, accepted in [('F1', True), ('auth-01_v2.0', True), ('a'*64, True),
                                            ('task:1', False), ('', False), ('a'*65, False),
                                            ('task name', False), ('任务1', False), ('-F1', False)]:
                        with self.subTest(planning=planning, field=field, value=value):
                            self.assertEqual(bool(re.fullmatch(constraint['pattern'], value)), accepted)
            self.assertIn('pattern', schema['properties']['modules']['items']['properties']['id'])
        # Building planning schemas must not narrow unrelated verdict schemas.
        self.assertNotIn('pattern', VERDICT_SCHEMA['properties']['modules']['items']['properties']['id'])

    def test_reviewer_repairs_task_colon_id_before_worker_runs(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [plan(final_task('task:1')),
            plan(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 3)
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertTrue(all(call['session_id'] == '' for call in reviewer.calls))
        self.assertIn('task:1', reviewer.calls[1]['prompt'])
        self.assertIn('Watcher rejection: Invalid function assignment id:', reviewer.calls[1]['prompt'])
        self.assertIn('ASCII', reviewer.calls[0]['prompt'])
        self.assertIn('task_id=F1', worker.calls[0]['prompt'])
        rejected = list(self.run.glob('000-reviewer-repair-*-text.md'))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(json.loads(rejected[0].read_text())['next_task']['id'], 'task:1')
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['usage']['agent_calls'], 4)
        self.assertEqual(state['function_tasks']['accepted'][0]['task']['id'], 'F1')
        self.assertEqual(state['requirements_review']['acceptance_criteria'], BRIEF['acceptance_criteria'])

    def test_identifier_repair_is_bounded_and_never_dispatches_invalid_work(self):
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [plan(final_task('task:1')), plan(final_task('task:2'))])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('Invalid function assignment id:', report.summary)
        self.assertIn('Colons and spaces are not allowed', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(worker.calls, [])
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['function_tasks'], {'current':None, 'accepted':[]})
        self.assertEqual(json.loads((self.run/'000-reviewer-text.md').read_text())['next_task']['id'], 'task:2')

    def test_unrelated_plan_rejections_do_not_enter_identifier_repair(self):
        invalid = final_task()
        invalid['test_files'] = ['docs/not_a_test.py']
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [plan(invalid)])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(worker.calls, [])
        self.assertFalse(list(self.run.glob('*reviewer-repair-*')))

    def test_workspace_change_prevents_retry_and_preserves_the_changed_file(self):
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [{**plan(final_task('task:1')), 'touch':'unexpected.py'}])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('Workspace changed during requirement review', report.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(worker.calls, [])
        self.assertEqual((self.root/'unexpected.py').read_text(), 'changed\n')
        self.assertFalse(list(self.run.glob('*reviewer-repair-*')))

    def test_repair_obeys_cumulative_call_budget(self):
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [plan(final_task('task:1'))])
        report = self.monitor(worker, reviewer, max_agent_calls=1).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(worker.calls, [])
        self.assertTrue(list(self.run.glob('*reviewer-repair-*-text.md')))

    def test_stop_arriving_after_invalid_plan_prevents_another_model_call(self):
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [plan(final_task('task:1'))])
        monitor = self.monitor(worker, reviewer)
        invoke = monitor._invoke
        def stop_after_return(*args, **kwargs):
            turn = invoke(*args, **kwargs)
            (self.run/'control.json').write_text(json.dumps({'action':'stop'}))
            return turn
        monitor._invoke = stop_after_return
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(worker.calls, [])

    def test_replanning_repairs_id_and_keeps_already_accepted_work(self):
        worker = FunctionWorker('worker', [{**result('F1', 'done'), 'touch':'app.py'},
            {**result('F2', 'done', status='done', file='next.py'), 'touch':'next.py'}])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py')), result('F1', 'accepted'),
            plan(final_task('task:2', 'next.py', depends=['F1'])),
            plan(final_task('F2', 'next.py', depends=['F1'])),
            result('F2', 'accepted', status='done', file='next.py')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 5)
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual([item['task']['id'] for item in state['function_tasks']['accepted']], ['F1', 'F2'])
        self.assertEqual(state['requirements_review']['acceptance_criteria'], BRIEF['acceptance_criteria'])

    def test_invalid_plan_can_resume_with_a_corrected_identifier(self):
        reviewer = ScriptedProvider('reviewer', [plan(final_task('task:1')), plan(final_task('task:1'))])
        first = self.monitor(ScriptedProvider('worker', []), reviewer).run()
        self.assertEqual(first.status, 'action_required', first.summary)
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(json.loads((self.run/'state.json').read_text())['usage']['agent_calls'], 5)
