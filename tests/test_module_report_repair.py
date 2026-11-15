"""Immutable-plan failures must repair the report against the same candidate."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.modules import ModuleLedger
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class ModuleReportRepairTests(unittest.TestCase):
    def run_case(self, root, conclusions, **options):
        plan = [{'id': 'A', 'title': 'A', 'acceptance': ['Preserve failure behavior']}]
        completed = {**_verdict('done', evidence=['app.py']), 'module_ids': ['A'], 'modules': []}
        worker = ScriptedProvider('worker', [{'verdict': completed, 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            *[{'verdict': {**completed, 'modules': proposal}} for proposal in conclusions],
        ])
        monitor = Supervisor(RunConfig('Implement A', root, worker, reviewer=reviewer,
            workflow='review-first', git_history='off', modules=plan, run_dir=root/'run',
            max_rounds=1, retry_delay_s=0, **options), log=lambda _: None)
        return monitor.run(), worker, reviewer, monitor

    def test_definition_drift_repairs_reviewer_without_reimplementing(self):
        changed = {'id': 'A', 'title': 'A', 'acceptance': ['Omit failure behavior']}
        new = {'id': 'C', 'title': 'C', 'acceptance': ['unapproved addition']}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, worker, reviewer, monitor = self.run_case(root, [[new, changed], []])
            self.assertEqual(report.status, 'done', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 3))
            self.assertTrue(all(call['read_only'] for call in reviewer.calls))
            prompt = reviewer.calls[-1]['prompt']
            self.assertIn('Reviewer-only repair of the SAME candidate', prompt)
            self.assertIn('acceptance[0]', prompt)
            self.assertIn('Preserve failure behavior', prompt)
            self.assertIn('Omit failure behavior', prompt)
            self.assertEqual(set(monitor.modules.items), {'A'})
            self.assertEqual(monitor.modules.items['A']['status'], 'accepted')
            self.assertEqual(len(list((root/'run').glob('001-reviewer-repair-*.log'))), 1)

    def test_repeated_drift_stops_without_weakening_or_registering_modules(self):
        changed = {'id': 'A', 'title': 'A', 'acceptance': ['weakened']}
        with tempfile.TemporaryDirectory() as tmp:
            report, worker, reviewer, monitor = self.run_case(Path(tmp), [[changed], [changed]])
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 3))
            self.assertEqual(monitor.modules.items['A']['acceptance'], ['Preserve failure behavior'])
            self.assertEqual(monitor.modules.items['A']['status'], 'pending')

    def test_repair_cannot_bypass_existing_call_budget(self):
        changed = {'id': 'A', 'title': 'A', 'acceptance': ['weakened']}
        with tempfile.TemporaryDirectory() as tmp:
            report, worker, reviewer, _ = self.run_case(Path(tmp), [[changed]], max_agent_calls=3)
            self.assertEqual(report.status, 'budget_exceeded', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))

    def test_field_diagnostics_are_specific_and_proposal_remains_atomic(self):
        ledger = ModuleLedger([{'id': 'A', 'title': 'A', 'acceptance': ['one', 'two']}])
        before = copy.deepcopy(ledger.export())
        with self.assertRaisesRegex(ValueError, r'acceptance\[1\].*saved="two".*proposed="changed"'):
            ledger.merge([{'id': 'C', 'title': 'C', 'acceptance': ['new']},
                          {'id': 'A', 'title': 'A', 'acceptance': ['one', 'changed']}])
        self.assertEqual(ledger.export(), before)


class ModuleWorkerReportRepairTests(unittest.TestCase):
    def exercise(self, root, correction, reviews=None, **options):
        plan = [{'id': 'A', 'title': 'Base', 'acceptance': ['base works']},
                {'id': 'B', 'title': 'Feature', 'acceptance': ['feature works'], 'depends_on': ['A']}]
        milestone = {**_verdict('done', scope='module', evidence=['app.py']),
                     'module_ids': ['B'], 'modules': []}
        confirmed = {**milestone, 'module_ids': ['A', 'B']}
        worker = ScriptedProvider('worker', [
            {'verdict': milestone, 'touch': 'app.py'},
            {'verdict': confirmed, **correction},
        ])
        reviewer = ScriptedProvider('reviewer', [
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            *[{'verdict': value} for value in (reviews or [confirmed])]])
        monitor = Supervisor(RunConfig('Implement A and B', root, worker, reviewer=reviewer,
            workflow='review-first', git_history='off', modules=plan, run_dir=root/'run',
            max_rounds=1, retry_delay_s=0, checks=['test -f app.py'], **options), log=lambda _: None)
        return monitor.run(), worker, reviewer, monitor

    def test_missing_dependency_confirmation_repairs_report_read_only_before_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, worker, reviewer, monitor = self.exercise(root, {})
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual(report.rounds, 1)
            self.assertEqual([call['read_only'] for call in worker.calls], [False, True])
            self.assertEqual(len(reviewer.calls), 2)
            self.assertIn('Worker report correction only for the SAME saved candidate', worker.calls[-1]['prompt'])
            self.assertTrue(all(m['status']=='accepted' for m in monitor.modules.export()))
            state = json.loads((root/'run/state.json').read_text())
            record = next(r for r in state['rounds'] if r['role']=='worker')
            self.assertEqual(record['watcher_result']['module_report']['status'], 'corrected')
            self.assertEqual(record['watcher_result']['module_report']['original']['module_ids'], ['B'])
            self.assertTrue(record['watcher_result']['candidate_checks']['passed'])
            self.assertEqual(len(list((root/'run').glob('001-worker-report-original-*.log'))), 1)

    def test_dependency_missing_from_reviewer_gets_its_own_read_only_repair(self):
        missing = {**_verdict('done', scope='module', evidence=['app.py']),
                   'module_ids': ['B'], 'modules': []}
        valid = {**missing, 'module_ids': ['A', 'B']}
        with tempfile.TemporaryDirectory() as tmp:
            report, worker, reviewer, monitor = self.exercise(Path(tmp), {}, [missing, valid])
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 3))
            self.assertIn('invalid module dependency confirmation', reviewer.calls[-1]['prompt'])
            self.assertTrue(all(m['status']=='accepted' for m in monitor.modules.export()))

    def test_failed_correction_does_not_invent_dependency_acceptance(self):
        invalid = {**_verdict('done', scope='module', evidence=['app.py']),
                   'module_ids': ['B'], 'modules': []}
        with tempfile.TemporaryDirectory() as tmp:
            report, worker, reviewer, monitor = self.exercise(Path(tmp), {'verdict': invalid})
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 1))
            self.assertTrue(all(m['status']=='pending' for m in monitor.modules.export()))

    def test_correction_cannot_accept_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, _, reviewer, monitor = self.exercise(Path(tmp), {'touch': 'unexpected.py'})
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('Inputs changed', report.summary)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertTrue(all(m['status']=='pending' for m in monitor.modules.export()))

    def test_correction_obeys_call_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            report, worker, reviewer, monitor = self.exercise(Path(tmp), {}, max_agent_calls=2)
            self.assertEqual(report.status, 'budget_exceeded', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 1))
            self.assertTrue(all(m['status']=='pending' for m in monitor.modules.export()))

    def test_resume_of_rejected_report_does_not_repeat_implementation(self):
        invalid = {**_verdict('done', scope='module', evidence=['app.py']),
                   'module_ids': ['B'], 'modules': []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, _, _, _ = self.exercise(root, {'verdict': invalid})
            self.assertEqual(report.status, 'action_required')
            worker, reviewer = ScriptedProvider('worker', []), ScriptedProvider('reviewer', [])
            report = Supervisor(RunConfig('Implement A and B', root, worker, reviewer=reviewer,
                workflow='review-first', git_history='off', run_dir=root/'run', resume=True,
                max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (0, 0))

    def test_resume_after_duplicate_audit_ids_retries_only_report_and_keeps_failure(self):
        invalid = {**_verdict('done', scope='module', evidence=['app.py']),
                   'module_ids': ['A', 'B'], 'modules': [],
                   'issue_resolutions': [{'id': 'AUD-9', 'summary': 'not a worker closure',
                                          'evidence': ['app.py']}] * 2}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, _, _, _ = self.exercise(root, {'verdict': invalid})
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('invalid issue_resolutions', report.summary)
            original_time = (root/'app.py').stat().st_mtime_ns
            valid = {**_verdict('done', scope='module', evidence=['app.py']),
                     'module_ids': ['A', 'B'], 'modules': []}
            worker = ScriptedProvider('worker', [{'verdict': valid}])
            reviewer = ScriptedProvider('reviewer', [{'verdict': valid}])
            monitor = Supervisor(RunConfig('Implement A and B', root, worker, reviewer=reviewer,
                workflow='review-first', git_history='off', run_dir=root/'run', resume=True,
                max_rounds=1, retry_delay_s=0), log=lambda _: None)
            report = monitor.run()
            self.assertEqual(report.status, 'max_rounds', report.summary)
            self.assertEqual([c['read_only'] for c in worker.calls], [True])
            self.assertEqual(len(reviewer.calls), 1)
            self.assertEqual((root/'app.py').stat().st_mtime_ns, original_time)
            self.assertTrue(all(m['status']=='accepted' for m in monitor.modules.export()))
            saved = next(r for r in report.records if r.role=='worker').watcher_result['module_report']
            self.assertEqual(saved['status'], 'corrected')
            self.assertEqual(len(saved['attempt_history']), 1)
            self.assertIn('invalid issue_resolutions', saved['attempt_history'][0]['failure'])
            self.assertTrue(Path(saved['attempt_history'][0]['output']).is_file())
            self.assertIn('issue_resolutions MUST be []', worker.calls[0]['prompt'])
            schema = json.loads((root/'run/001-worker-report-schema.json').read_text())
            self.assertEqual(schema['properties']['issue_resolutions']['maxItems'], 0)
            original_schema = json.loads((root/'run/verdict.schema.json').read_text())
            self.assertGreater(original_schema['properties']['issue_resolutions']['maxItems'], 0)

    def test_resumed_format_correction_is_bounded_by_existing_call_budget(self):
        invalid = {**_verdict('done', scope='module', evidence=['app.py']),
                   'module_ids': ['A', 'B'], 'modules': [],
                   'issue_resolutions': [{'id': 'AUD-9', 'summary': 'duplicate',
                                          'evidence': ['app.py']}] * 2}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report, _, _, _ = self.exercise(root, {'verdict': invalid})
            self.assertEqual(report.status, 'action_required')
            worker, reviewer = ScriptedProvider('worker', []), ScriptedProvider('reviewer', [])
            report = Supervisor(RunConfig('Implement A and B', root, worker, reviewer=reviewer,
                workflow='review-first', git_history='off', run_dir=root/'run', resume=True,
                max_rounds=1, max_agent_calls=3, retry_delay_s=0), log=lambda _: None).run()
            self.assertEqual(report.status, 'budget_exceeded', report.summary)
            self.assertEqual((len(worker.calls), len(reviewer.calls)), (0, 0))

    def test_another_invalid_format_stops_after_one_read_only_attempt_per_resume(self):
        invalid = {**_verdict('done', scope='module', evidence=['app.py']),
                   'module_ids': ['A', 'B'], 'modules': [],
                   'issue_resolutions': [{'id': 'AUD-9', 'summary': 'duplicate',
                                          'evidence': ['app.py']}] * 2}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.exercise(root, {'verdict': invalid})
            worker = ScriptedProvider('worker', [{'verdict': invalid}])
            reviewer = ScriptedProvider('reviewer', [])
            monitor = Supervisor(RunConfig('Implement A and B', root, worker, reviewer=reviewer,
                workflow='review-first', git_history='off', run_dir=root/'run', resume=True,
                max_rounds=1, retry_delay_s=0), log=lambda _: None)
            report = monitor.run()
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertEqual([c['read_only'] for c in worker.calls], [True])
            self.assertFalse(reviewer.calls)
            self.assertTrue(all(m['status']=='pending' for m in monitor.modules.export()))


if __name__ == '__main__':
    unittest.main()
