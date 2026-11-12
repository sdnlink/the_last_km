"""Independent acceptance advances submitted functions without report resubmission."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_supervisor import ScriptedProvider


SOURCE = 'def parse_input():\n    return 42\n'


def submission(*, final=False):
    value = result('F1', 'done', status='done' if final else 'continue')
    value['verdict'].update(
        gaps=['Watcher checks and reviewer acceptance are pending; not claimed as passed.'],
        next_actions=['watcher 执行固定检查；reviewer 独立验收。'])
    return {**value, 'source':SOURCE}


def approval(*, close=False, next_task=None):
    value = result('F1', 'accepted', status='done' if next_task is None else 'continue', next_task=next_task)
    if close:
        value['verdict']['issue_resolutions'] = [{'id':'AUD-1',
            'summary':'The report handoff is not an implementation defect; current code and tests meet F1.',
            'evidence':['app.py']}]
    return value


class FunctionReportAcceptanceTests(unittest.TestCase):
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

    def seed_report_issue(self, monitor):
        emit = monitor._event
        def event(kind, **data):
            emit(kind, **data)
            if kind == 'function_assigned' and data['task_id'] == 'F1':
                monitor._function_issue_ids = monitor.audit.record(
                    ['main/F1 completion report contains nonempty gaps and next_actions; source is correct.'], 0, ['main'])
        monitor._event = event

    def test_nonempty_report_notes_do_not_block_final_acceptance_or_watcher_checks(self):
        checked = self.root/'run/acceptance-ran'
        command = "python3 -c 'from pathlib import Path; Path(\"run/acceptance-ran\").touch()'"
        worker = FunctionWorker('worker', [submission(final=True)])
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), approval()])
        monitor = self.monitor(worker, reviewer)
        monitor.config.checks.append(command)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))
        self.assertTrue(checked.exists())
        raw = json.loads((self.run_dir/'001-worker-text.md').read_text())
        self.assertTrue(raw['gaps'] and raw['next_actions'])
        worker_record = next(r for r in report.records if r.role == 'worker')
        self.assertEqual(worker_record.watcher_result['function_assessment']['original']['gaps'], raw['gaps'])

    def test_report_issue_closes_and_next_function_starts_on_first_acceptance(self):
        second = final_task('F2', 'next.py', depends=['F1'])
        worker = FunctionWorker('worker', [submission(),
            {**result('F2', 'done', status='done', file='next.py'), 'touch':'next.py'}])
        final_review = result('F2', 'accepted', status='done', file='next.py')
        final_review['verdict']['issue_resolutions'] = approval(close=True)['verdict']['issue_resolutions']
        reviewer = ScriptedProvider('reviewer', [plan(task('F1', 'app.py')),
            approval(close=True, next_task=second), final_review])
        monitor = self.monitor(worker, reviewer, max_rounds=2)
        self.seed_report_issue(monitor)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 3))
        self.assertEqual((self.root/'app.py').read_text(), SOURCE)
        saved = json.loads((self.run_dir/'state.json').read_text())
        self.assertEqual([item['task']['id'] for item in saved['function_tasks']['accepted']], ['F1','F2'])
        self.assertEqual(saved['audit_issues']['items'][0]['status'], 'resolved')

    def test_worker_closure_suggestions_do_not_close_audit_or_trigger_worker_resubmission(self):
        completed = submission(final=True)
        completed['verdict']['issue_resolutions'] = approval(close=True)['verdict']['issue_resolutions']
        worker = FunctionWorker('worker', [completed])
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), approval(), approval(close=True)])
        monitor = self.monitor(worker, reviewer)
        self.seed_report_issue(monitor)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 3))
        self.assertIn('Accepted function needs explicit reviewer closure for: AUD-1', reviewer.calls[-1]['prompt'])

    def test_missing_audit_closure_is_repaired_only_by_reviewer_without_implementation(self):
        worker = FunctionWorker('worker', [submission(final=True)])
        reviewer = ScriptedProvider('reviewer', [plan(final_task()), approval(), approval(close=True)])
        monitor = self.monitor(worker, reviewer)
        self.seed_report_issue(monitor)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 3))
        self.assertIn('Accepted function needs explicit reviewer closure for: AUD-1', reviewer.calls[-1]['prompt'])
        self.assertIn('no worker report or source edit is required', reviewer.calls[-1]['prompt'])

    def test_legacy_report_downgrade_resumes_review_without_worker_resubmission(self):
        completed = submission(final=True)
        completed['verdict']['issue_resolutions'] = approval(close=True)['verdict']['issue_resolutions']
        worker = FunctionWorker('worker', [completed])
        reviewer = ScriptedProvider('reviewer', [plan(final_task())])
        def old_validator(monitor, verdict):
            verdict.status = 'continue'
            verdict.task_status = 'incomplete'
            verdict.gaps.append('Report only the dispatched function with its exact task ID and verified completion evidence.')
        with patch.object(Supervisor, '_validate_function_worker', old_validator):
            report = self.monitor(worker, reviewer, max_agent_calls=2,
                quality_policy={'review_changed_files':1}).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        saved = json.loads((self.run_dir/'state.json').read_text())
        saved['audit_issues'] = {'next_id':2, 'pruned_closed':0, 'items':[{
            'id':'AUD-1', 'summary':'main/F1 report formatting rejected; implementation verified.',
            'module_ids':['main'], 'status':'open', 'first_seen_round':1, 'last_seen_round':1,
            'resolved_round':0, 'resolution':None, 'tree':''}]}
        saved['function_issue_ids'] = ['AUD-1']
        (self.run_dir/'state.json').write_text(json.dumps(saved))
        worker = FunctionWorker('worker', [])
        reviewer = ScriptedProvider('reviewer', [approval(close=True)])
        report = self.monitor(worker, reviewer, resume=True, max_agent_calls=3).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (0, 1))
        self.assertIn('close that invalid finding in this same review', reviewer.calls[0]['prompt'])
        self.assertEqual((self.root/'app.py').read_text(), SOURCE)

    def test_real_unfinished_work_cannot_be_promoted_from_legacy_report_recovery(self):
        worker = FunctionWorker('worker', [{**result('F1','incomplete',gaps=['Malformed input is accepted']), 'source':SOURCE}])
        reviewer = ScriptedProvider('reviewer', [plan(final_task())])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertFalse(any(r.watcher_result.get('submission_recovered') for r in report.records))


if __name__ == '__main__':
    unittest.main()
