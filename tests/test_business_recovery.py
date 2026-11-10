"""New branch input must continue work while invalidating old approval and checks."""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.approved_history import ApprovedHistory
from auto_moniter.git_history import GitHistoryError
from auto_moniter.quality import Quality
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import file_snapshot
from auto_moniter.business_recovery import update_baseline
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class SnapshotReviewer(ScriptedProvider):
    def _command(self, session, prompt, read_only):
        match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt.read_text())
        if match:
            self.scripts[0]['verdict']['evidence'].append(match[1])
        return super()._command(session, prompt, read_only)


class BusinessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        self.run = self.root/'run'
        self.run.mkdir()
        self.git('init', '-q')
        (self.project/'app.py').write_text('original\n')
        (self.project/'.gitignore').write_text('.auto-moniter/\n')
        (self.project/'.github/workflows').mkdir(parents=True)
        (self.project/'.github/workflows/check.yml').write_text('original check\n')
        self.git('add', '.')
        self.commit('original')
        self.original = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.project), *args]).decode().strip()

    def commit(self, message):
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@local', 'commit', '--allow-empty', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def external(self, name='external.py', content='external input\n'):
        (self.project/name).write_text(content)
        self.git('add', '--', name)
        return self.commit('external input')

    def monitor(self, worker, reviewer, **options):
        return Supervisor(RunConfig('Original requirement', self.project, worker, reviewer=reviewer,
            workflow='review-first', git_history='required', run_dir=self.run, retry_delay_s=0,
            quality_policy={'protect_tests':True}, max_rounds=5, max_agent_calls=12,
            checks=['git diff --check'], **options), log=lambda _:None)

    def pause_before_work(self):
        monitor = self.monitor(ScriptedProvider('worker', []), ScriptedProvider('reviewer', []))
        monitor.request_stop()
        self.assertEqual(monitor.run().status, 'stopped')
        return json.loads((self.run/'state.json').read_text())

    def delivery(self, **options):
        worker = ScriptedProvider('worker', [{'verdict':_verdict('done', evidence=['app.py']), 'touch':'project/app.py'}])
        reviewer = SnapshotReviewer('reviewer', [{'verdict':{}, 'text':json.dumps(BRIEF)},
            {'verdict':_verdict('done', evidence=['app.py'])}])
        return self.monitor(worker, reviewer, **options), worker, reviewer

    def test_resume_on_new_commit_preserves_work_and_runs_fresh_checks_and_review(self):
        self.pause_before_work()
        (self.project/'untracked.txt').write_text('human work')
        new_head = self.external('.github/workflows/check.yml', 'new committed check\n')
        monitor, worker, reviewer = self.delivery(resume=True)
        result = monitor.run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual((self.project/'untracked.txt').read_text(), 'human work')
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(new_head, worker.calls[0]['prompt'])
        self.assertEqual(len(reviewer.calls), 2)
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['usage']['agent_calls'], 3)
        self.assertEqual(state['business_update_count'], 1)
        self.assertEqual(state['git_history']['business_updates'][0]['after']['commit'], new_head)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), new_head)
        self.assertTrue(json.loads((self.run/'acceptance-results.json').read_text())['checks'])

    def test_commit_during_worker_restarts_from_current_source_without_old_review(self):
        monitor, worker, reviewer = self.delivery()
        worker.scripts.append({'verdict':_verdict('done', evidence=['app.py'])})
        original = worker._command
        commits = []
        def command(*args):
            prepared = original(*args)
            if not commits:
                commits.append(self.external())
            return prepared
        worker._command = command
        result = monitor.run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(json.loads((self.run/'state.json').read_text())['usage']['agent_calls'], 4)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), commits[0])
        self.assertEqual(worker.calls[1]['session_id'], '')

    def test_commit_during_review_discards_the_stale_approval(self):
        monitor, worker, reviewer = self.delivery()
        worker.scripts.append({'verdict':_verdict('done', evidence=['app.py'])})
        reviewer.scripts.append({'verdict':_verdict('done', evidence=['app.py'])})
        original = reviewer._command
        commits = []
        def command(*args):
            prepared = original(*args)
            if len(reviewer.calls) == 2:
                commits.append(self.external())
            return prepared
        reviewer._command = command
        result = monitor.run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 3)
        stale = next(record for record in result.records if record.role == 'reviewer' and record.index == 1)
        self.assertIn('requires revalidation', stale.failure)
        self.assertFalse(stale.git_checkpoint)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), commits[0])

    def test_pending_candidate_and_saved_approval_are_not_reused_after_an_empty_commit(self):
        self.pause_before_work()
        saved = json.loads((self.run/'state.json').read_text())
        history = ApprovedHistory(self.project, self.run)
        history.prepare_review(1, history.begin_round(1))
        (self.run/'approval-ready.json').write_text('{}')
        (self.run/'acceptance-results.json').write_text(json.dumps({'checks':[], 'complete':True}))
        saved['git_history'] = history.export()
        (self.run/'state.json').write_text(json.dumps(saved))
        self.commit('external empty commit')
        monitor, _, _ = self.delivery(resume=True)
        original = monitor._invoke
        def invoke(*args, **kwargs):
            self.assertFalse((self.run/'approval-ready.json').exists())
            self.assertTrue(json.loads((self.run/'acceptance-results.json').read_text())['invalidated'])
            return original(*args, **kwargs)
        with patch.object(monitor, '_invoke', invoke):
            self.assertEqual(monitor.run().status, 'done')

    def test_committed_assets_refresh_but_uncommitted_protected_edits_still_stop(self):
        self.pause_before_work()
        self.external('.github/workflows/check.yml', 'new committed check\n')
        (self.project/'.github/workflows/check.yml').write_text('uncommitted weakened check\n')
        monitor, worker, _ = self.delivery(resume=True)
        result = monitor.run()
        self.assertEqual(result.status, 'action_required', result.summary)
        self.assertIn('Protected acceptance baseline', result.summary)
        self.assertEqual(worker.calls, [])

    def test_adoption_is_replayed_after_git_state_was_saved_before_monitor_state(self):
        self.pause_before_work()
        self.external()
        history = ApprovedHistory(self.project, self.run)
        self.assertTrue(history.reconcile_business_head())
        self.assertFalse(history.reconcile_business_head())
        monitor, worker, _ = self.delivery(resume=True)
        self.assertEqual(monitor.run().status, 'done')
        self.assertIn('Prior completion, review and check results need revalidation', worker.calls[0]['prompt'])
        self.assertEqual(json.loads((self.run/'state.json').read_text())['business_update_count'], 1)

    def test_branch_switch_and_rewritten_history_are_rejected_without_changing_history(self):
        history = ApprovedHistory(self.project, self.run)
        before = (self.run/'git-history.json').read_bytes()
        self.git('checkout', '-qb', 'another')
        with self.assertRaisesRegex(GitHistoryError, 'branch changed'):
            history.reconcile_business_head()
        self.assertEqual((self.run/'git-history.json').read_bytes(), before)
        self.git('checkout', '-q', history.business_head['ref'].removeprefix('refs/heads/'))
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=f@local', 'commit', '--amend', '-qm', 'rewrite')
        with self.assertRaisesRegex(GitHistoryError, 'history was rewritten'):
            history.reconcile_business_head()
        self.assertEqual((self.run/'git-history.json').read_bytes(), before)

    def test_task_ref_tampering_remains_an_error(self):
        history = ApprovedHistory(self.project, self.run)
        self.external()
        self.git('update-ref', history.work_ref, self.git('rev-parse', 'HEAD'))
        with self.assertRaisesRegex(GitHistoryError, 'Task work ref changed'):
            history.reconcile_business_head()

    def test_new_and_deleted_committed_acceptance_assets_follow_the_current_commit(self):
        history = ApprovedHistory(self.project, self.run)
        baseline = file_snapshot(self.project)
        (self.project/'.github/workflows/check.yml').unlink()
        (self.project/'test_new.py').write_text('new committed check\n')
        self.git('add', '--', '.github/workflows/check.yml', 'test_new.py')
        self.commit('replace check')
        before = dict(history.business_head)
        history.reconcile_business_head()
        current = file_snapshot(self.project)
        result = update_baseline(history, Quality({'protect_tests':True}, baseline), before, current)
        self.assertNotIn(str(self.project/'.github/workflows/check.yml'), result)
        self.assertEqual(result[str(self.project/'test_new.py')], current[str(self.project/'test_new.py')])

    def test_resume_preserves_cumulative_calls_and_reopens_accepted_modules_and_audit_ids(self):
        worker = ScriptedProvider('worker', [{'verdict':_verdict('done', evidence=['app.py']), 'touch':'project/app.py'}])
        reviewer = SnapshotReviewer('reviewer', [{'verdict':{}, 'text':json.dumps(BRIEF)},
            {'verdict':_verdict('continue', gaps=['missing behavior'], evidence=['app.py'])}])
        monitor = self.monitor(worker, reviewer)
        monitor.log = lambda message: monitor.request_stop() if 'reviewer status=continue' in message else None
        self.assertEqual(monitor.run().status, 'stopped')
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['usage']['agent_calls'], 3)
        state['modules'][0].update(status='accepted', tree='a'*64)
        state['audit_issues']['items'][0].update(status='resolved', tree='a'*64, resolved_round=1,
            resolution={'summary':'previously checked','evidence':['app.py']})
        (self.run/'state.json').write_text(json.dumps(state))
        self.external()
        worker = ScriptedProvider('worker', [{'verdict':_verdict('done', evidence=['app.py'])}])
        reviewer = SnapshotReviewer('reviewer', [{'verdict':_verdict('done', evidence=['app.py'], closed=['AUD-1'])}])
        monitor = self.monitor(worker, reviewer, resume=True)
        result = monitor.run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertIn('AUD-1', worker.calls[0]['prompt'])
        self.assertIn('"status": "pending"', worker.calls[0]['prompt'])
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['usage']['agent_calls'], 5)
        self.assertEqual(state['audit_issues']['items'][0]['id'], 'AUD-1')
        self.assertEqual(state['audit_issues']['items'][0]['status'], 'resolved')

    def test_unmerged_paths_are_not_adopted(self):
        history = ApprovedHistory(self.project, self.run)
        self.external()
        blob = self.git('rev-parse', 'HEAD:app.py')
        subprocess.run(['git', '-C', str(self.project), 'update-index', '--index-info'],
            input=f'0 {"0"*40}\tapp.py\n100644 {blob} 1\tapp.py\n100644 {blob} 2\tapp.py\n100644 {blob} 3\tapp.py\n',
            text=True, check=True)
        with self.assertRaisesRegex(GitHistoryError, 'unresolved merge conflicts'):
            history.reconcile_business_head()

    def test_commit_at_publication_entry_restarts_verification_without_resetting_calls(self):
        from auto_moniter.publication import publish
        monitor, worker, reviewer = self.delivery()
        worker.scripts.append({'verdict':_verdict('done', evidence=['app.py'])})
        reviewer.scripts.append({'verdict':_verdict('done', evidence=['app.py'])})
        commits = []
        def publication(*args, **kwargs):
            if not commits:
                commits.append(self.external())
            return publish(*args, **kwargs)
        with patch('auto_moniter.publication.publish', publication):
            result = monitor.run()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 3)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), commits[0])
        self.assertEqual(json.loads((self.run/'state.json').read_text())['usage']['agent_calls'], 5)
