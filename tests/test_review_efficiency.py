"""Exercise real loop transitions; a protocol failure is not an implementation gap."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.approved_history import ApprovedHistory
from auto_moniter.audit import AuditLedger
from auto_moniter.modules import ModuleLedger
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_supervisor import ScriptedProvider, _verdict
from tests.test_review_first import BRIEF


class ReviewEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'run'

    def brief(self):
        return {'verdict': {}, 'text': json.dumps(BRIEF)}

    def monitor(self, worker, reviewer, **extra):
        return Supervisor(RunConfig('Implement the original requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='off', run_dir=self.run,
            max_rounds=1, retry_delay_s=0, **extra), log=lambda _: None)

    def worker(self):
        return ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])

    def done(self):
        return {'verdict': _verdict('done', evidence=['app.py'])}

    def test_malformed_review_is_repaired_without_another_worker_turn(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [self.brief(), {'verdict': {}, 'text': '{}'}, self.done()])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 3))
        self.assertTrue(all(c['read_only'] for c in reviewer.calls))
        self.assertIn('Reviewer-only repair of the SAME candidate', reviewer.calls[-1]['prompt'])
        self.assertEqual(len(list(self.run.glob('001-reviewer-repair-*.log'))), 1)
        self.assertEqual(len([r for r in report.records if r.role == 'reviewer']), 2)
        state = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(state['usage']['agent_calls'], 4)

    def test_transport_failure_retries_reviewer_without_worker(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': {}, 'text': 'connection reset', 'exit_code': 1}, self.done()])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertIn('connection reset', reviewer.calls[-1]['prompt'])

    def test_missing_checkpoint_citation_is_reviewer_repair(self):
        import re
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        worker = self.worker()
        class Reviewer(ScriptedProvider):
            def _command(inner, session, prompt_file, read_only):
                if len(inner.calls) == 2:
                    checkpoint = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt_file.read_text()).group(1)
                    inner.scripts[0]['verdict']['evidence'].append(checkpoint)
                return super()._command(session, prompt_file, read_only)
        reviewer = Reviewer('reviewer', [self.brief(), self.done(), self.done()])
        report = Supervisor(RunConfig('Implement the original requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='required', run_dir=self.run,
            max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertIn('invalid review checkpoint evidence', reviewer.calls[-1]['prompt'])

    def stopped_review(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': {}, 'text': '{}'}, {'verdict': {}, 'text': '{}'}])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(worker.calls), 1)
        return report

    def test_resume_only_runs_pending_review_even_at_worker_round_limit(self):
        self.stopped_review()
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [self.done()])
        report = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(report.rounds, 1)

    def test_new_source_invalidates_pending_worker_reuse(self):
        self.stopped_review()
        (self.root / 'app.py').write_text('new input\n')
        worker = ScriptedProvider('worker', [])
        report = self.monitor(worker, ScriptedProvider('reviewer', []), resume=True).run()
        # No candidate bypass: the configured worker limit still stops new work.
        self.assertEqual(report.status, 'max_rounds')
        self.assertEqual(worker.calls, [])

    def test_new_human_instruction_prevents_pending_candidate_reuse(self):
        self.stopped_review()
        from auto_moniter.directions import Directions
        Directions(self.run).append('Also validate the new input case')
        report = self.monitor(ScriptedProvider('worker', []), ScriptedProvider('reviewer', []), resume=True).run()
        self.assertEqual(report.status, 'max_rounds')

    def test_call_budget_is_not_bypassed_by_reviewer_repair(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [self.brief(), {'verdict': {}, 'text': '{}'}])
        report = self.monitor(worker, reviewer, max_agent_calls=3).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        state = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(state['usage']['agent_calls'], 3)

    def test_authentication_error_does_not_retry_or_rewrite(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': {}, 'text': 'Not signed in. Set XAI_API_KEY or run grok login', 'exit_code': 1}])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))

    def test_plan_without_changes_does_not_force_full_review(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', gaps=['working'])}])
        reviewer = ScriptedProvider('reviewer', [self.brief()])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'max_rounds')
        self.assertEqual(len(reviewer.calls), 1)

    def test_source_changes_do_not_bypass_progress_review_interval(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', gaps=['implementing']), 'touch': f'part{i}.py'}
            for i in range(3)
        ])
        reviewer = ScriptedProvider('reviewer', [self.brief()])
        report = Supervisor(RunConfig('Implement the requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='off', run_dir=self.run,
            max_rounds=3, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 1))

    def test_progress_review_runs_at_configured_interval(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', gaps=['implementing']), 'touch': f'part{i}.py'}
            for i in range(3)
        ])
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': _verdict('continue', gaps=['part2.py: finish input handling'])}])
        report = Supervisor(RunConfig('Implement the requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='off', run_dir=self.run,
            max_rounds=3, review_every=3, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 2))

    def test_disabling_progress_review_still_reviews_completion(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', gaps=['implementing']), 'touch': 'part.py'},
            {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
        ])
        reviewer = ScriptedProvider('reviewer', [self.brief(), self.done()])
        report = Supervisor(RunConfig('Implement the requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='off', run_dir=self.run,
            max_rounds=2, review_every=0, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 2))

    def test_local_repair_preserves_verified_code_and_closes_exact_audit(self):
        (self.root / 'verified.py').write_text('verified behavior\n')
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
            {'verdict': _verdict('done', evidence=['boundary.py']), 'touch': 'boundary.py'},
        ])
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': _verdict('continue', gaps=['app.py: empty input raises instead of returning []'],
                actions=['Fix empty-input branch only; add boundary.py regression. Preserve verified.py.'])},
            {'verdict': _verdict('done', evidence=['boundary.py'], closed=['AUD-1'])},
        ])
        report = Supervisor(RunConfig('Implement the requirement', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='off', run_dir=self.run,
            max_rounds=2, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 3))
        repair = worker.calls[1]['prompt']
        self.assertIn('AUD-1', repair)
        self.assertIn('empty input raises', repair)
        self.assertIn('bounded repair', repair)
        self.assertIn('Preserve verified.py', repair)
        self.assertEqual((self.root / 'verified.py').read_text(), 'verified behavior\n')

    def test_happy_path_uses_one_worker_and_two_read_only_reviewer_calls(self):
        worker = self.worker()
        reviewer = ScriptedProvider('reviewer', [self.brief(), self.done()])
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))

    def test_external_blocker_is_reviewed_even_with_periodic_reviews_disabled(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('blocked')}])
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': _verdict('continue', gaps=['Dependency unavailable; independent work remains'])}])
        report = self.monitor(worker, reviewer, review_every=0).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (1, 2))

    def test_worker_start_stop_does_not_spend_an_agent_call(self):
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [self.brief()])
        supervisor = self.monitor(worker, reviewer)
        supervisor.log = lambda message: supervisor.request_stop() if 'worker running' in message else None
        report = supervisor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(worker.calls, [])
        state = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(state['next_round'], 1)
        resumed_worker = self.worker()
        resumed_reviewer = ScriptedProvider('reviewer', [self.done()])
        resumed = self.monitor(resumed_worker, resumed_reviewer, resume=True).run()
        self.assertEqual(resumed.status, 'done', resumed.summary)
        self.assertEqual((len(resumed_worker.calls), len(resumed_reviewer.calls)), (1, 1))

    def test_acceptance_inherits_the_configured_executable_path(self):
        binary = self.root / 'private-bin'
        binary.mkdir()
        tool = binary / 'project-check'
        tool.write_text('#!/bin/sh\nprintf "private executable checked\\n"\n')
        tool.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(binary) + os.pathsep + os.environ['PATH']}):
            report = self.monitor(self.worker(), ScriptedProvider('reviewer', [self.brief(), self.done()]),
                                  checks=['project-check']).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertIn('private executable checked', (self.run / '001-check-01.log').read_text())

    def test_reviewer_write_during_repair_never_accepts(self):
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': {}, 'text': '{}', 'touch': 'illegal.py'}, self.done()])
        report = self.monitor(self.worker(), reviewer).run()
        self.assertEqual(report.status, 'failed')
        self.assertIn('Workspace changed during read-only review', report.summary)
        self.assertEqual(len(reviewer.calls), 2)


class ScopedConclusionTests(unittest.TestCase):
    def plan(self):
        return [{'id': 'A', 'title': 'A', 'acceptance': ['A works'], 'files': ['a.py']},
                {'id': 'B', 'title': 'B', 'acceptance': ['B works'], 'files': ['b.py']},
                {'id': 'FINAL', 'title': 'Final', 'acceptance': ['integration works'], 'depends_on': ['A', 'B']}]

    def test_partial_scope_preserves_unaffected_module_and_closed_audit(self):
        modules = ModuleLedger(self.plan())
        for value in modules.items.values():
            value.update(status='accepted', tree='a'*64)
        audit = AuditLedger()
        audit.record(['A broken'], 1, ['A'])
        audit.resolve([{'id': 'AUD-1', 'summary': 'verified fix', 'evidence': ['a.py']}],
                      2, 'a'*64, lambda _: [])
        affected = modules.affected(['b.py'])
        self.assertEqual(affected, {'B', 'FINAL'})
        self.assertEqual(set(modules.invalidate('b'*64, ['b.py'])), {'B', 'FINAL'})
        self.assertEqual(audit.invalidate('b'*64, affected, ['b.py']), [])
        self.assertEqual(audit.items[0]['status'], 'resolved')
        self.assertEqual(modules.items['A']['status'], 'accepted')
        self.assertEqual(AuditLedger.restore(audit.export()).items[0]['tree'], 'b'*64)

    def test_shared_unknown_changes_and_changed_evidence_require_revalidation(self):
        modules = ModuleLedger(self.plan())
        self.assertEqual(modules.affected(['shared.cfg']), {'A', 'B', 'FINAL'})
        self.assertEqual(modules.affected(['a.py']), {'A', 'FINAL'})
        audit = AuditLedger()
        audit.record(['A broken'], 1, ['A'])
        audit.resolve([{'id': 'AUD-1', 'summary': 'verified fix', 'evidence': ['b.py']}],
                      2, 'a'*64, lambda _: [])
        self.assertEqual(audit.invalidate('b'*64, {'B', 'FINAL'}, ['b.py']), ['AUD-1'])

    def test_delta_review_base_survives_history_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'app.py').write_text('baseline\n')
            run = root / '.auto-moniter/run'
            run.mkdir(parents=True)
            history = ApprovedHistory(root, run)
            base = history.begin_round(1)
            (root / 'app.py').write_text('reviewed\n')
            history.prepare_review(1, base)
            reviewed = history.candidate['commit']
            history.remember_review(1)
            (root / 'new.py').write_text('new behavior\n')
            history = ApprovedHistory(root, run)
            base = history.begin_round(2)
            history.prepare_review(2, base)
            context = history.review_context()
            self.assertIn('Previous completed business review tree: ' + reviewed, context)
            delta = context.split('Delta:\n', 1)[1]
            self.assertIn('new.py', delta)
            self.assertNotIn('diff --git a/app.py', delta)
            self.assertIn('cumulative diff remains available', context)


class ResumeBusinessHeadTests(unittest.TestCase):
    def test_web_resume_refuses_external_commit_before_launch(self):
        from console.recovery import resume_arguments
        from types import SimpleNamespace
        from auto_moniter.providers import resolve_provider
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            def commit(message):
                subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
                                'user.email=fixture@local', 'commit', '--allow-empty', '-qm', message], check=True)
            commit('original')
            identity = {'ref': subprocess.check_output(['git', '-C', str(root), 'symbolic-ref', '-q', 'HEAD'], text=True).strip(),
                        'commit': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()}
            run = root / 'run'
            run.mkdir()
            config = RunConfig('', root, resolve_provider('codex'), reviewer=resolve_provider('codex'))
            state = {'cwd': str(root), 'checks': [],
                     'provider_config': Supervisor._provider_config(SimpleNamespace(config=config)),
                     'git_history': {'approval_only': True, 'business_head': identity}}
            argv = ['run', '--cwd', str(root), '--run-dir', str(run), '--provider', 'codex', '--reviewer', 'codex']
            self.assertIn('--resume', resume_arguments({'argv': argv}, run, state))
            commit('external maintenance')
            with self.assertRaisesRegex(ValueError, '业务 HEAD 已在任务外变更'):
                resume_arguments({'argv': argv}, run, state)
