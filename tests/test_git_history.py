import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.git_history import GitHistory, GitHistoryError
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import evidence_paths, file_snapshot
from tests.test_supervisor import ScriptedProvider, _verdict


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True).stdout


class GitProjectFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()
        git(self.project, 'init', '-q')
        (self.project / 'app.py').write_text('original\n')
        git(self.project, 'add', '.')
        git(self.project, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'original')

    def tearDown(self):
        self.temp.cleanup()


class GitHistoryTests(GitProjectFixture):
    def test_dirty_baseline_and_real_index_are_preserved(self):
        (self.project / 'app.py').write_text('staged input\n')
        git(self.project, 'add', 'app.py')
        (self.project / 'app.py').write_text('unstaged input\n')
        original_head = git(self.project, 'rev-parse', 'HEAD')
        original_index = git(self.project, 'show', ':app.py')
        baseline = file_snapshot(self.project)
        history = GitHistory.open(self.project, self.run, 'required')
        before = history.begin_round(1)
        (self.project / 'app.py').write_text('worker implementation\n')
        worker = history.record(1, 'worker', {'status': 'continue', 'summary': 'first conclusion'}, before=before)
        diff = git(self.project, 'diff', before, worker['commit'], '--', 'app.py').decode()
        self.assertIn('-unstaged input', diff)
        self.assertIn('+worker implementation', diff)
        self.assertEqual(git(self.project, 'rev-parse', 'HEAD'), original_head)
        self.assertEqual(git(self.project, 'show', ':app.py'), original_index)
        self.assertEqual(worker['changed_files'], ['app.py'])
        self.assertEqual(evidence_paths(self.project, [worker['record']], baseline=baseline), [])
        data = json.loads(git(self.project, 'show', worker['commit'] + ':' + worker['record']))
        self.assertEqual(data['result']['summary'], 'first conclusion')
        review = history.record(1, 'reviewer', {'status': 'continue'}, before=worker['commit'])
        review_data = json.loads(git(self.project, 'show', review['commit'] + ':' + review['record']))
        self.assertEqual(review_data['reviewed_worker']['commit'], worker['commit'])

    def test_added_deleted_binary_and_symlink_changes_are_traced(self):
        history = GitHistory.open(self.project, self.run)
        before = history.begin_round(1)
        (self.project / 'app.py').unlink()
        (self.project / 'new [file].bin').write_bytes(b'\0\xffbinary')
        (self.project / 'link').symlink_to('new [file].bin')
        worker = history.record(1, 'worker', {'status': 'continue'}, before=before)
        self.assertEqual(set(worker['changed_files']), {'app.py', 'new [file].bin', 'link'})
        self.assertEqual(git(self.project, 'show', worker['commit'] + ':new [file].bin'), b'\0\xffbinary')
        self.assertIn(b'120000 blob', git(self.project, 'ls-tree', worker['commit'], 'link'))
        self.assertIn('deleted file', history.review_context())

    def test_unchanged_rounds_and_resume_keep_immutable_past_records(self):
        history = GitHistory.open(self.project, self.run)
        first = history.record(1, 'worker', {'status': 'continue', 'summary': 'original result'},
                               before=history.begin_round(1), raw='timeout detail')
        (self.project / first['record']).write_text('tampered physical record')
        restored = GitHistory.open(self.project, self.run)
        second = restored.record(2, 'worker', {'status': 'continue'}, before=restored.begin_round(2))
        self.assertNotEqual(first['commit'], second['commit'])
        self.assertEqual(second['changed_files'], [])
        old = json.loads(git(self.project, 'show', second['commit'] + ':' + first['record']))
        self.assertEqual(old['result']['summary'], 'original result')
        self.assertEqual(old['raw_conclusion'], 'timeout detail')
        with self.assertRaises(GitHistoryError):
            restored.record(1, 'worker', {}, before=restored.head)
        with self.assertRaises(GitHistoryError):
            GitHistory.open(self.project, self.run, 'off')
        git(self.project, 'update-ref', restored.ref, restored.initial)
        with self.assertRaises(GitHistoryError):
            restored.begin_round(3)

    def test_subdirectory_projects_keep_history_paths_and_scope(self):
        sub = self.project / 'sub [project]'
        sub.mkdir()
        (sub / 'feature.py').write_text('before\n')
        history = GitHistory.open(sub, self.run)
        before = history.begin_round(1)
        (sub / 'feature.py').write_text('after\n')
        (self.project / 'app.py').write_text('outside project\n')
        first = history.record(1, 'worker', {'status': 'continue'}, before=before)
        second = history.record(2, 'worker', {'status': 'continue'}, before=history.begin_round(2))
        self.assertEqual(first['changed_files'], ['feature.py'])
        prefix = sub.relative_to(self.project).as_posix() + '/'
        self.assertIn(b'"status": "continue"', git(self.project, 'show', second['commit'] + ':' + prefix + first['record']))
        self.assertNotIn(b'app.py', git(self.project, 'ls-tree', '-r', '--name-only', second['commit']))

    def test_unborn_repository_and_plain_project_modes(self):
        plain = self.root / 'plain'
        plain.mkdir()
        self.assertIsNone(GitHistory.open(plain, self.run))
        with self.assertRaises(GitHistoryError):
            GitHistory.open(plain, self.run, 'required')
        git(plain, 'init', '-q')
        history = GitHistory.open(plain, self.run)
        self.assertEqual(git(plain, 'rev-parse', history.ref).decode().strip(), history.initial)
        git(plain, 'update-ref', '-d', history.ref)
        with self.assertRaises(GitHistoryError):
            history.verify()

    def test_linked_worktree_has_independent_audit_and_original_head(self):
        linked = self.root / 'linked'
        git(self.project, 'worktree', 'add', '-qb', 'linked', str(linked))
        self.assertTrue((linked / '.git').is_file())
        head = git(linked, 'rev-parse', 'HEAD')
        history = GitHistory.open(linked, self.run)
        before = history.begin_round(1)
        (linked / 'app.py').write_text('linked implementation\n')
        record = history.record(1, 'worker', {'status': 'continue'}, before=before)
        self.assertEqual(record['changed_files'], ['app.py'])
        self.assertEqual(git(linked, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.project / 'app.py').read_text(), 'original\n')


class DiffReviewer(ScriptedProvider):
    def _command(self, session_id, prompt_file, read_only):
        prompt = prompt_file.read_text()
        match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt)
        if not match:
            raise AssertionError('Missing exact Git worker checkpoint')
        checkpoint = match.group(1)
        base = re.search(r'Round baseline: ([0-9a-f]{40,64})', prompt).group(1)
        cwd = Path(json.loads((prompt_file.parent / 'git-history.json').read_text())['project'])
        self.actual_diff = git(cwd, 'diff', base, checkpoint, '--', 'app.py').decode()
        self.scripts[0]['verdict']['evidence'].append('git diff checkpoint ' + checkpoint)
        return super()._command(session_id, prompt_file, read_only)


class GitSupervisorTests(GitProjectFixture):
    # Integration uses actual Git and local scripted processes; no model is called.
    def test_supervisor_reviews_actual_project_diff_and_exports_git_records(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = DiffReviewer('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
        # ScriptedProvider writes next to its run folder: put the test run in the project.
        run = self.project / 'run'
        config = RunConfig('Implement feature', self.project, worker, reviewer=reviewer, run_dir=run, max_rounds=1,
                           git_history='required', retry_delay_s=0)
        report = Supervisor(config, log=lambda _: None).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertIn('+changed', reviewer.actual_diff)
        self.assertTrue(reviewer.calls[0]['read_only'])
        state = json.loads((run / 'state.json').read_text())
        self.assertTrue(state['git_history']['enabled'])
        self.assertEqual(len(report.records), 2)
        for record in report.records:
            self.assertTrue(record.git_checkpoint['commit'])
            data = json.loads((self.project / record.git_checkpoint['record']).read_text())
            self.assertEqual(data['result']['requirement'], 'Implement feature')
        saved = json.loads((run / 'report.json').read_text())
        self.assertEqual(saved['git_history']['head'], state['git_history']['head'])

    def test_review_without_checkpoint_evidence_cannot_accept(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [{'verdict': _verdict('done', evidence=['app.py'])}])
        report = Supervisor(RunConfig('Implement', self.project, worker, reviewer=reviewer,
                                      run_dir=self.project / 'run', max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'max_rounds')
        self.assertIn('worker checkpoint hash', ' '.join(report.records[-1].verdict.gaps))
        self.assertTrue(report.records[-1].git_checkpoint)

    def test_failed_worker_conclusion_is_committed_before_stopping(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue'), 'touch': 'app.py', 'exit_code': 1}])
        report = Supervisor(RunConfig('Implement', self.project, worker, no_review=True, failure_limit=1,
                                      run_dir=self.project / 'run', retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'failed')
        checkpoint = report.records[0].git_checkpoint
        data = json.loads(git(self.project, 'show', checkpoint['commit'] + ':' + checkpoint['record']))
        self.assertEqual(data['result']['exit_code'], 1)
        self.assertEqual(checkpoint['changed_files'], ['app.py'])

    def test_checkpoint_failure_prevents_review_and_delivery(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [])
        with patch.object(GitHistory, 'record', side_effect=GitHistoryError('Cannot persist worker checkpoint')):
            report = Supervisor(RunConfig('Implement', self.project, worker, reviewer=reviewer,
                                          run_dir=self.project / 'run', retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'failed')
        self.assertIn('Cannot persist worker checkpoint', report.summary)
        self.assertEqual(reviewer.calls, [])

    def test_invalid_module_update_still_preserves_worker_conclusion(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue'), 'touch': 'app.py'}])
        with patch.object(Supervisor, '_merge_modules', side_effect=ValueError('invalid module update')):
            report = Supervisor(RunConfig('Implement', self.project, worker, no_review=True,
                                          run_dir=self.project / 'run', retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'failed')
        self.assertIn('invalid module update', report.summary)
        self.assertTrue(report.records[0].git_checkpoint['commit'])

    def test_retention_does_not_delete_git_conclusions(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue', gaps=['unfinished'])} for _ in range(3)])
        run = self.project / 'run'
        report = Supervisor(RunConfig('Implement', self.project, worker, no_review=True, max_rounds=3,
                                      keep_rounds=1, run_dir=run, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'max_rounds')
        history = GitHistory.open(self.project, run)
        records = git(self.project, 'ls-tree', '-r', '--name-only', history.ref, '--', '.auto-moniter/history').decode()
        for index in range(1, 4):
            self.assertIn(f'{index:03d}-worker.json', records)
        self.assertFalse((run / '001-worker.log').exists())

    def test_stop_resume_extends_same_branch_and_round_numbers(self):
        run = self.project / 'run'
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue'), 'touch': 'app.py'}])
        monitor = Supervisor(RunConfig('Implement', self.project, worker, no_review=True,
                                       run_dir=run, retry_delay_s=0), log=lambda _: None)

        def stop_after_conclusion(message):
            if message.startswith('round 1: worker status='):
                monitor.request_stop()

        monitor.log = stop_after_conclusion
        stopped = monitor.run()
        self.assertEqual(stopped.status, 'stopped')
        first = stopped.records[0].git_checkpoint
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue')}])
        resumed = Supervisor(RunConfig('', self.project, worker, no_review=True, resume=True,
                                       max_rounds=2, run_dir=run, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(resumed.status, 'max_rounds', resumed.summary)
        second = resumed.records[-1].git_checkpoint
        self.assertEqual(second['round'], 2)
        self.assertEqual(second['ref'], first['ref'])
        self.assertEqual(second['base'], first['commit'])
        self.assertEqual(git(self.project, 'merge-base', first['commit'], second['commit']).decode().strip(), first['commit'])

    def test_stopping_interrupted_run_preserves_git_checkpoint_state(self):
        run = self.project / 'run'
        monitor = Supervisor(RunConfig('Implement', self.project, ScriptedProvider('worker', []),
                                       no_review=True, run_dir=run), log=lambda _: None)
        monitor.request_stop()
        self.assertEqual(monitor.run().status, 'stopped')
        initial = json.loads((run / 'state.json').read_text())['git_history']
        cleanup = Supervisor(RunConfig('', self.project, ScriptedProvider('worker', []), no_review=True,
                                       resume=True, run_dir=run), log=lambda _: None)
        report = cleanup.stop_interrupted('cleanup requested')
        self.assertEqual(report.status, 'stopped')
        self.assertEqual(json.loads((run / 'state.json').read_text())['git_history'], initial)

    def test_console_exposes_checkpoints_for_running_and_finished_tasks(self):
        from console.tasks import TaskStore

        run = self.project / '.auto-moniter/runs/example'
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue')}])
        report = Supervisor(RunConfig('Implement', self.project, worker, no_review=True, run_dir=run,
                                      max_rounds=1, retry_delay_s=0), log=lambda _: None).run()
        self.assertEqual(report.status, 'max_rounds')
        store = TaskStore(self.root / 'console', self.project, [run.parent])
        task = next(task for task in store.overview()['tasks'] if task['run_dir'] == str(run))
        self.assertTrue(task['git_history']['enabled'])
        self.assertEqual(task['git_checkpoints'][0]['commit'], report.records[0].git_checkpoint['commit'])
        self.assertEqual(task['git_checkpoints'][0]['summary'], report.records[0].verdict.summary)
        (run / 'report.json').unlink()
        self.assertEqual(store.detail(task['id'])['git_checkpoints'][0]['commit'], task['git_checkpoints'][0]['commit'])

    def test_interrupted_cleanup_with_changed_git_ref_returns_recovery_failure(self):
        run = self.project / 'run'
        monitor = Supervisor(RunConfig('Implement', self.project, ScriptedProvider('worker', []),
                                       no_review=True, run_dir=run), log=lambda _: None)
        monitor.request_stop()
        self.assertEqual(monitor.run().status, 'stopped')
        saved_state = (run / 'state.json').read_bytes()
        history = json.loads((run / 'git-history.json').read_text())
        git(self.project, 'update-ref', history['ref'], git(self.project, 'rev-parse', 'HEAD').decode().strip())
        cleanup = Supervisor(RunConfig('', self.project, ScriptedProvider('worker', []), no_review=True,
                                       resume=True, run_dir=run), log=lambda _: None)
        report = cleanup.stop_interrupted('cleanup requested')
        self.assertEqual(report.status, 'recovery_failed')
        self.assertIn('Git audit branch changed', report.summary)
        self.assertEqual((run / 'state.json').read_bytes(), saved_state)
