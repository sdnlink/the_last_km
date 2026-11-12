"""Report mistakes must not restart implementation or bypass function acceptance."""
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.providers import ConfiguredProvider
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_supervisor import ScriptedProvider


SOURCE = 'def parse_input():\n    return 42\n'


def handoff(ident='F1', *, final=False):
    value = result(ident, 'done', status='done' if final else 'continue')
    value['verdict']['scope'] = 'module' if not final else 'requirement'
    value['verdict']['next_actions'] = ['watcher 执行固定检查；reviewer 独立复核函数和测试证据。']
    return {**value, 'source': SOURCE}


class WorkerReportRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_dir = self.root/'run'

    def monitor(self, worker, reviewer, **kwargs):
        kwargs.setdefault('max_rounds', 1)
        return Supervisor(RunConfig('Implement the original request', self.root, worker,
            reviewer=reviewer, workflow='review-first', function_dispatch=True,
            git_history='off', run_dir=self.run_dir, retry_delay_s=0,
            review_every=0, checks=[CHECK], **kwargs), log=lambda _:None)

    def test_handoff_is_corrected_read_only_before_independent_review(self):
        next_task = final_task('F2', 'next.py', depends=['F1'])
        worker = FunctionWorker('worker', [handoff(), result('F1', 'done'),
            {**result('F2', 'done', status='done', file='next.py'), 'touch':'next.py'}])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py')),
            result('F1', 'accepted', next_task=next_task), result('F2', 'accepted', status='done', file='next.py')])
        report = self.monitor(worker, reviewer, max_rounds=2).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual([c['read_only'] for c in worker.calls], [False, True, False])
        self.assertEqual(worker.calls[1]['session_id'], '')
        self.assertIn('Worker report correction only for the SAME saved candidate', worker.calls[1]['prompt'])
        self.assertNotIn('Worker time management: implement', worker.calls[1]['prompt'])
        self.assertNotIn('Only implement', worker.calls[1]['prompt'])
        self.assertIn('requires next_actions=[]', worker.calls[1]['prompt'])
        self.assertNotIn('module_ids may contain only', worker.calls[1]['prompt'])
        self.assertEqual(len(reviewer.calls), 3)
        self.assertEqual((self.root/'app.py').read_text(), SOURCE)
        worker_record = next(r for r in report.records if r.role == 'worker')
        self.assertEqual(worker_record.verdict.task_status, 'done')
        self.assertEqual(worker_record.watcher_result['worker_report']['status'], 'corrected')
        original = Path(worker_record.watcher_result['worker_report']['original_output'])
        self.assertEqual(json.loads(original.read_text())['next_actions'], handoff()['verdict']['next_actions'])
        self.assertTrue(original.with_name(original.name.replace('-text.md', '.log')).exists())
        saved = json.loads((self.run_dir/'state.json').read_text())
        self.assertEqual(len(saved['function_tasks']['accepted']), 2)
        self.assertFalse(saved['audit_issues']['items'])

    def test_valid_report_needs_no_correction(self):
        worker = FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'source':SOURCE}])
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertFalse(any(r.watcher_result.get('worker_report') for r in report.records))

    def test_corrected_report_still_requires_independent_acceptance(self):
        worker = FunctionWorker('worker', [handoff(final=True), result('F1', 'done', status='done')])
        reviewer = ScriptedProvider('reviewer', [plan(final_task()),
            result('F1', 'changes_required', gaps=['main/app.py: malformed inputs still return success'])])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(reviewer.calls), 2)
        saved = json.loads((self.run_dir/'state.json').read_text())
        self.assertFalse(saved['function_tasks']['accepted'])
        self.assertEqual(saved['audit_issues']['items'][0]['status'], 'open')

    def test_substantive_gaps_do_not_become_report_only_work(self):
        worker = FunctionWorker('worker', [{**result('F1', 'done', gaps=['Malformed inputs are still accepted']),
                                           'source':SOURCE}])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py'))])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertFalse(worker.calls[0]['read_only'])
        self.assertIn('Malformed inputs are still accepted', report.records[0].verdict.gaps)

    def test_correction_can_honestly_report_missing_evidence_without_approval(self):
        original = handoff()
        original['verdict']['evidence'] = []
        worker = FunctionWorker('worker', [original,
            result('F1', 'incomplete', gaps=['No existing behavioral verification evidence'])])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py'))])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertTrue(worker.calls[1]['read_only'])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(report.records[0].verdict.task_status, 'incomplete')
        self.assertFalse(json.loads((self.run_dir/'state.json').read_text())['function_tasks']['accepted'])

    def test_second_invalid_report_stops_after_one_correction(self):
        worker = FunctionWorker('worker', [handoff(), handoff()])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py'))])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('requires next_actions=[]', report.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(report.records[0].verdict.task_status, 'incomplete')
        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [])
        resumed = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(resumed.status, 'action_required', resumed.summary)
        self.assertFalse(worker.calls or reviewer.calls)

    def test_workspace_change_during_report_correction_cannot_be_accepted(self):
        correction = {**result('F1', 'done'), 'source':SOURCE.replace('42', '99')}
        worker = FunctionWorker('worker', [handoff(), correction])
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py'))])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('changed during read-only function report correction', report.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(report.records[0].watcher_result['worker_report']['status'], 'stale')

    def test_budget_interruption_resumes_only_report_on_saved_candidate(self):
        worker = FunctionWorker('worker', [handoff(final=True)])
        reviewer = ScriptedProvider('reviewer', [plan(final_task())])
        report = self.monitor(worker, reviewer, max_agent_calls=2).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        saved = json.loads((self.run_dir/'state.json').read_text())
        self.assertEqual(saved['rounds'][0]['watcher_result']['worker_report']['status'], 'pending')
        worker = FunctionWorker('worker', [result('F1', 'done', status='done')])
        reviewer = ScriptedProvider('reviewer', [result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer, resume=True, max_agent_calls=4).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertTrue(worker.calls[0]['read_only'])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual((self.root/'app.py').read_text(), SOURCE)

    def test_writable_worker_mode_is_narrowed_only_for_correction(self):
        worker = FunctionWorker('worker', [handoff(final=True), result('F1', 'done', status='done')])
        worker.mode = 'full-access'
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(worker.mode, 'full-access')
        self.assertTrue(worker.calls[1]['read_only'])

    def test_worker_permission_overrides_cannot_weaken_report_correction(self):
        worker = FunctionWorker('worker', [handoff(final=True)])
        reviewer = ScriptedProvider('reviewer', [plan(final_task())])
        report = self.monitor(worker, reviewer, extra_args=['--sandbox=danger-full-access']).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('requires a supported read-only worker', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 1)

    def test_configured_provider_delegate_uses_read_only_mode_without_changing_settings(self):
        worker = FunctionWorker('worker', [handoff(final=True), result('F1', 'done', status='done')])
        worker.mode = 'full-access'
        configured = ConfiguredProvider('codex', worker, None, {})
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), result('F1', 'accepted', status='done')])
        report = self.monitor(configured, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(worker.mode, 'full-access')
        self.assertEqual(configured.mode, 'full-access')
        self.assertTrue(worker.calls[1]['read_only'])


if __name__ == '__main__':
    unittest.main()
