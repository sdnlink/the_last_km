"""Exercise the source reviewed before a follow-up's first Worker dispatch."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import candidate_checks
from auto_moniter.directions import Directions
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import workspace_fingerprint
from tests.test_requirement_follow_up import BRIEF, FOLLOW, NEXT, CitingReviewer, _git
from tests.test_supervisor import ScriptedProvider, _verdict


CHECKS = ['test -f app.py', 'echo checked >> run/check-count']
ACCEPTANCE = 'Keep the accepted boundary behavior.'
AMENDMENT = 'Preserve the original delivery while extending validation.'
SOURCE = 'limit = 27\n'
REFRESHED = {**NEXT, 'refined_requirement': 'Cover the current source limit of 27.'}


def brief(value, **kwargs):
    return {'verdict': {}, 'text': json.dumps(value), **kwargs}


class ProcessExit(BaseException):
    pass


class FollowUpSourceBindingTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = directory / 'workspace'
        self.root.mkdir()
        self.run_dir = self.root / 'run'
        self.enterContext(patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(directory / 'state')}))
        _git(self.root, 'init', '-q')
        _git(self.root, 'checkout', '-qb', 'main')
        _git(self.root, 'config', 'user.name', 'Fixture')
        _git(self.root, 'config', 'user.email', 'fixture@local')
        (self.root / 'app.py').write_text('limit = 3\n')
        _git(self.root, 'add', 'app.py')
        _git(self.root, 'commit', '-qm', 'base')

    def monitor(self, worker, reviewer, *, resume=False, max_agent_calls=0):
        return Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='review-first',
            resume=resume, run_dir=self.run_dir, git_history='required', retry_delay_s=0,
            max_rounds=2, max_agent_calls=max_agent_calls, checks=[] if resume else CHECKS[:],
            acceptance=ACCEPTANCE,
        ), log=lambda _: None)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def context(self):
        return json.loads((self.run_dir / 'requirement-review-context.json').read_text())

    def calls(self):
        events = (json.loads(line) for line in (self.run_dir / 'events.jsonl').read_text().splitlines())
        return [(event['role'], int(event['round'])) for event in events if event['event'] == 'agent_start']

    def stop_after_ready(self, *, max_agent_calls=0):
        delivered = _verdict('done', evidence=['app.py'])
        worker = ScriptedProvider('worker', [{'verdict': delivered, 'touch': 'app.py'}])
        reviewer = CitingReviewer([brief(BRIEF), {'verdict': delivered, 'cite': True}, brief(NEXT)], self.root)
        monitor = self.monitor(worker, reviewer, max_agent_calls=max_agent_calls)

        def stop_after_publication(note):
            if note.startswith('requirements: reviewer elaborating next requirement'):
                Directions(self.run_dir).append(AMENDMENT)
            if 'continuing next requirement elaboration' in note:
                monitor.request_stop()

        monitor.log = stop_after_publication
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls(), [('reviewer', 0), ('worker', 1), ('reviewer', 1), ('reviewer', 900001)])
        state = self.state()
        self.assertEqual(state['requirements_review']['status'], 'ready')
        self.assertTrue(state['rounds'][1]['git_checkpoint']['business_commit']['created'])
        return state

    def fast_forward(self):
        (self.root / 'app.py').write_text(SOURCE)
        _git(self.root, 'add', 'app.py')
        _git(self.root, 'commit', '-qm', 'operator updates the input')
        return _git(self.root, 'rev-parse', 'HEAD')

    def assert_delivery(self, original, head, *, commits=3):
        current = self.state()
        self.assertEqual(current['requirement_cycles'], original['requirement_cycles'])
        self.assertEqual(current['rounds'][:2], original['rounds'])
        self.assertEqual(current['requirement_revision'], original['requirement_revision'])
        self.assertEqual(current['checks'], CHECKS)
        receipt = original['rounds'][0]['watcher_result']['candidate_checks']
        self.assertTrue(candidate_checks.valid(receipt, self.run_dir, receipt['context']))
        approval = current['rounds'][1]['watcher_result']['candidate_approval']
        self.assertEqual(approval['checks_digest'], receipt['digest'])
        self.assertEqual((self.run_dir / 'check-count').read_text().splitlines(), ['checked'])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), head)
        published = original['requirement_cycles'][0]['handoff']['commit']
        _git(self.root, 'merge-base', '--is-ancestor', published, head)
        self.assertEqual(int(_git(self.root, 'rev-list', '--count', 'HEAD')), commits)

    def worker(self):
        return ScriptedProvider('worker', [{'verdict': _verdict('continue', ['work remains'])}])

    def run_one_worker(self, monitor):
        invoke = monitor._invoke

        def stop_after_worker(*args, **kwargs):
            turn = invoke(*args, **kwargs)
            if kwargs['role'] == 'worker':
                monitor.request_stop()
            return turn

        with patch.object(monitor, '_invoke', side_effect=stop_after_worker):
            return monitor.run()

    def test_adopted_fast_forward_refreshes_ready_before_first_worker(self):
        original = self.stop_after_ready()
        old_context = self.context()
        head = self.fast_forward()
        tree = workspace_fingerprint(self.root, ignore_dir=self.run_dir)
        worker = self.worker()
        reviewer = CitingReviewer([brief(REFRESHED)], self.root)
        monitor = self.monitor(worker, reviewer, resume=True)
        observed_source = []
        reviewer_command = reviewer._command

        def observe_review(session, prompt_file, read_only):
            observed_source.append((self.root / 'app.py').read_text())
            return reviewer_command(session, prompt_file, read_only)

        start = len(self.calls())
        with patch.object(reviewer, '_command', side_effect=observe_review):
            report = self.run_one_worker(monitor)
        stale = bool(worker.calls and NEXT['refined_requirement'] in worker.calls[0]['prompt'])
        self.assertEqual(self.calls()[start:], [('reviewer', 900001), ('worker', 2)],
                         f'Old brief reached the first Worker: {stale}')
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(observed_source, [SOURCE])
        self.assertTrue(reviewer.calls[0]['read_only'])
        for value in (FOLLOW, ACCEPTANCE, AMENDMENT, json.dumps(CHECKS),
                      original['requirement_cycles'][0]['handoff']['commit']):
            self.assertIn(value, reviewer.calls[0]['prompt'])
        self.assertIn(REFRESHED['refined_requirement'], worker.calls[0]['prompt'])
        self.assertNotIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertNotEqual(tree, old_context['tree'])
        self.assertEqual(self.context()['tree'], tree)
        self.assertEqual(self.context()['response'], REFRESHED)
        self.assertEqual(self.state()['git_history']['business_update_count'], 1)
        self.assert_delivery(original, head)

    def test_resume_after_refreshed_ready_reuses_review(self):
        original = self.stop_after_ready()
        head = self.fast_forward()
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([brief(REFRESHED)], self.root)
        monitor = self.monitor(worker, reviewer, resume=True)
        monitor.log = lambda note: (
            monitor.request_stop() if note == 'round 2: worker running' and reviewer.calls else None
        )
        start = len(self.calls())
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls()[start:], [('reviewer', 900001)])
        refreshed = self.state()['requirements_review']
        self.assertEqual(refreshed['refined_requirement'], REFRESHED['refined_requirement'])
        self.assertEqual(self.context()['tree'], workspace_fingerprint(self.root, ignore_dir=self.run_dir))

        worker = self.worker()
        reviewer = CitingReviewer([], self.root)
        start = len(self.calls())
        report = self.run_one_worker(self.monitor(worker, reviewer, resume=True))
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls()[start:], [('worker', 2)])
        self.assertIn(REFRESHED['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(self.state()['requirements_review'], refreshed)
        self.assert_delivery(original, head)

    def test_empty_fast_forward_reuses_matching_legacy_context(self):
        original = self.stop_after_ready()
        checkpoint = original['requirements_review'].copy()
        del checkpoint['reviewed_checks_sha256']
        write_json(self.run_dir / 'state.json', {**original, 'requirements_review': checkpoint}, durable=True)
        write_json(self.run_dir / 'reviewed-requirement.json', checkpoint, durable=True)
        _git(self.root, 'commit', '--allow-empty', '-qm', 'same source')
        head = _git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(self.context()['tree'], workspace_fingerprint(self.root, ignore_dir=self.run_dir))

        worker = self.worker()
        reviewer = CitingReviewer([], self.root)
        start = len(self.calls())
        report = self.run_one_worker(self.monitor(worker, reviewer, resume=True))
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls()[start:], [('worker', 2)])
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(self.state()['git_history']['business_update_count'], 1)
        self.assert_delivery(original, head)

    def test_missing_corrupt_or_mismatched_context_requires_review(self):
        original = self.stop_after_ready()
        saved = self.context()
        path = self.run_dir / 'requirement-review-context.json'
        head = _git(self.root, 'rev-parse', 'HEAD')
        invalid = {
            'missing file': None,
            'invalid JSON': '{',
            'excessively nested JSON': '[' * 2000 + ']' * 2000,
            'missing tree': json.dumps({key: value for key, value in saved.items() if key != 'tree'}),
            'other source': json.dumps({**saved, 'tree': '0' * 64}),
            'other request': json.dumps({**saved, 'original_sha256': '0' * 64}),
            'other response': json.dumps({**saved, 'response': REFRESHED}),
        }
        for case, contents in invalid.items():
            with self.subTest(case=case):
                write_json(self.run_dir / 'state.json', original, durable=True)
                if contents is None:
                    path.unlink()
                else:
                    path.write_text(contents)
                worker = ScriptedProvider('worker', [])
                reviewer = CitingReviewer([brief(REFRESHED)], self.root)
                monitor = self.monitor(worker, reviewer, resume=True)
                monitor.log = lambda note: (
                    monitor.request_stop() if note == 'round 2: worker running' and reviewer.calls else None
                )
                start = len(self.calls())
                report = monitor.run()
                self.assertEqual(report.status, 'stopped', report.summary)
                self.assertEqual(self.calls()[start:], [('reviewer', 900001)])
                self.assertEqual(self.context()['response'], REFRESHED)
                self.assert_delivery(original, head, commits=2)

    def assert_damaged_artifact_refreshes_ready(self, original, *, saved_analysis=False, restart=False):
        head = _git(self.root, 'rev-parse', 'HEAD')
        worker = self.worker()
        reviewer = CitingReviewer([brief(REFRESHED)], self.root)
        command = reviewer._command
        start = len(self.calls())

        def observe_reviewer(session, prompt_file, read_only):
            self.assertEqual(worker.calls, [])
            self.assertTrue(read_only)
            self.assertEqual(self.calls()[start:], [('reviewer', 900001)])
            if saved_analysis:
                self.assertIn(NEXT['refined_requirement'], prompt_file.read_text())
            return command(session, prompt_file, read_only)

        monitor = self.monitor(worker, reviewer, resume=True)
        with patch.object(reviewer, '_command', side_effect=observe_reviewer):
            if restart:
                monitor.log = lambda note: (
                    monitor.request_stop() if note == 'round 2: worker running' and reviewer.calls else None
                )
                report = monitor.run()
                self.assertEqual(report.status, 'stopped', report.summary)
                self.assertEqual(worker.calls, [])
                self.assertEqual(self.calls()[start:], [('reviewer', 900001)])
                saved_ready = self.state()['requirements_review']
                self.assertEqual(saved_ready['status'], 'ready')
                report = self.run_one_worker(self.monitor(worker, reviewer, resume=True))
                self.assertEqual(self.state()['requirements_review'], saved_ready)
            else:
                report = self.run_one_worker(monitor)
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls()[start:], [('reviewer', 900001), ('worker', 2)])
        self.assertIn(REFRESHED['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(self.context()['response'], REFRESHED)
        self.assert_delivery(original, head, commits=2)

    def test_nested_legacy_response_without_context_refreshes_ready(self):
        original = self.stop_after_ready()
        (self.run_dir / 'requirement-review-context.json').unlink()
        artifact = self.run_dir / '900001-reviewer-text.md'
        artifact.write_text('[' * 20000 + '0' + ']' * 20000)
        self.assert_damaged_artifact_refreshes_ready(original)

    def test_nested_rejected_response_keeps_valid_saved_analysis(self):
        original = self.stop_after_ready()
        artifact = self.run_dir / '900001-reviewer-repair-4-text.md'
        artifact.write_text('[' * 20000 + '0' + ']' * 20000)
        failed = {**original['requirements_review'], 'status': 'failed', 'output_file': artifact.name}
        write_json(self.run_dir / 'state.json', {**original, 'requirements_review': failed}, durable=True)
        self.assert_damaged_artifact_refreshes_ready(original, saved_analysis=True)

    def assert_fifo_replaced_on_refresh(self, name):
        original = self.stop_after_ready()
        (self.run_dir / 'requirement-review-context.json').unlink()
        artifact = self.run_dir / name
        artifact.unlink(missing_ok=True)
        os.mkfifo(artifact)
        read_text, write_text = Path.read_text, Path.write_text

        def guard_fifo(method):
            def guarded(path, *args, **kwargs):
                if path == artifact and stat.S_ISFIFO(path.lstat().st_mode):
                    self.fail(f'Blocking text I/O attempted on {path.name}')
                return method(path, *args, **kwargs)
            return guarded

        # Use a real pipe without a peer. Fail promptly if a regression
        # attempts blocking I/O through Path.read_text or Path.write_text.
        with patch.object(Path, 'read_text', guard_fifo(read_text)), patch.object(
                Path, 'write_text', guard_fifo(write_text)):
            self.assert_damaged_artifact_refreshes_ready(original)
        self.assertTrue(stat.S_ISREG(artifact.lstat().st_mode))

    def test_fifo_context_is_rejected_and_replaced(self):
        self.assert_fifo_replaced_on_refresh('requirement-review-context.json')

    def test_fifo_legacy_response_is_rejected_and_replaced(self):
        self.assert_fifo_replaced_on_refresh('900001-reviewer-text.md')

    def assert_directory_preserved_on_refresh(self, name):
        original = self.stop_after_ready()
        (self.run_dir / 'requirement-review-context.json').unlink()
        artifact = self.run_dir / name
        artifact.unlink(missing_ok=True)
        artifact.mkdir()
        evidence = artifact / 'evidence.txt'
        evidence.write_bytes(b'original directory contents\n')
        self.assert_damaged_artifact_refreshes_ready(original, restart=True)
        self.assertTrue(artifact.is_file())
        preserved = list(self.run_dir.rglob(evidence.name))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), b'original directory contents\n')

    def test_context_directory_is_preserved_on_refresh(self):
        self.assert_directory_preserved_on_refresh('requirement-review-context.json')

    def test_legacy_response_directory_is_preserved_on_refresh(self):
        self.assert_directory_preserved_on_refresh('900001-reviewer-text.md')

    def test_unencodable_legacy_response_is_ignored(self):
        original = self.stop_after_ready()
        (self.run_dir / 'requirement-review-context.json').unlink()
        artifact = self.run_dir / '900001-reviewer-text.md'
        artifact.write_text(json.dumps({**NEXT, 'summary': chr(0xD800)}))
        self.assert_damaged_artifact_refreshes_ready(original)

    def test_unencodable_rejected_response_keeps_valid_saved_analysis(self):
        original = self.stop_after_ready()
        artifact = self.run_dir / '900001-reviewer-repair-4-text.md'
        artifact.write_text(json.dumps({**NEXT, 'summary': chr(0xD800)}))
        failed = {**original['requirements_review'], 'status': 'failed', 'output_file': artifact.name}
        write_json(self.run_dir / 'state.json', {**original, 'requirements_review': failed}, durable=True)
        self.assert_damaged_artifact_refreshes_ready(original, saved_analysis=True)

    def assert_review_blocks_dispatch(self, scripts, status, review_status, *, budget=0):
        original = self.stop_after_ready(max_agent_calls=budget)
        head = self.fast_forward()
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer(scripts, self.root)
        start = len(self.calls())
        report = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, status, report.summary)
        self.assertEqual(self.calls()[start:], [('reviewer', 900001)] * len(scripts))
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertEqual(self.state()['requirements_review']['status'], review_status)
        self.assertEqual(self.state()['next_round'], 2)
        self.assertEqual(self.state()['usage']['agent_calls'], original['usage']['agent_calls'] + len(scripts))
        self.assert_delivery(original, head)

    def test_needs_input_after_source_change_blocks_worker(self):
        blocked = brief({**REFRESHED, 'status': 'needs_input', 'blockers': ['Supply the expected limit.']})
        self.assert_review_blocks_dispatch([blocked, blocked], 'action_required', 'needs_input')

    def test_failed_reviewer_after_source_change_blocks_worker(self):
        self.assert_review_blocks_dispatch([brief(REFRESHED, exit_code=7)], 'action_required', 'failed')

    def test_exhausted_budget_blocks_review_and_worker(self):
        self.assert_review_blocks_dispatch([], 'budget_exceeded', 'ready', budget=4)

    def test_last_available_call_can_review_but_cannot_dispatch_worker(self):
        self.assert_review_blocks_dispatch([brief(REFRESHED)], 'budget_exceeded', 'ready', budget=5)

    def test_pause_at_first_dispatch_waits_and_reads_source_after_resuming(self):
        original = self.stop_after_ready()
        worker = self.worker()
        reviewer = CitingReviewer([brief(REFRESHED)], self.root)
        monitor = self.monitor(worker, reviewer, resume=True)
        control = self.run_dir / 'control.json'
        heads = []
        paused_calls = []
        start = len(self.calls())
        beat = monitor._beat

        def pause_before_worker(note):
            if note == 'round 2: worker running' and not heads:
                write_json(control, {'action': 'pause'}, durable=True)

        def update_source_during_pause(note):
            beat(note)
            if note == 'paused at a turn boundary':
                paused_calls.append(self.calls()[start:])
                heads.append(self.fast_forward())
                write_json(control, {'action': 'continue'}, durable=True)

        monitor.log = pause_before_worker
        with patch.object(monitor, '_beat', side_effect=update_source_during_pause):
            report = self.run_one_worker(monitor)
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(paused_calls, [[]])
        self.assertEqual(self.calls()[start:], [('reviewer', 900001), ('worker', 2)])
        self.assertIn(REFRESHED['refined_requirement'], worker.calls[0]['prompt'])
        self.assert_delivery(original, heads[0])

    def test_stop_before_refresh_preserves_existing_delivery(self):
        original = self.stop_after_ready()
        head = self.fast_forward()
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        monitor = self.monitor(worker, reviewer, resume=True)
        start = self.calls()
        monitor.log = lambda note: (
            write_json(self.run_dir / 'control.json', {'action': 'stop'}, durable=True)
            if note.startswith('requirements: reviewer continuing saved analysis') else None
        )
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls(), start)
        self.assert_delivery(original, head)

    def test_resume_of_already_invoked_worker_keeps_existing_dispatch_path(self):
        original = self.stop_after_ready()
        worker = self.worker()
        reviewer = CitingReviewer([], self.root)
        monitor = self.monitor(worker, reviewer, resume=True)
        invoke = monitor._invoke

        def interrupt_after_worker(*args, **kwargs):
            invoke(*args, **kwargs)
            self.assertEqual(kwargs['role'], 'worker')
            raise ProcessExit()

        with patch.object(monitor, '_invoke', side_effect=interrupt_after_worker):
            with self.assertRaises(ProcessExit):
                monitor.run()
        self.assertEqual(self.state()['next_round'], 2)
        self.assertEqual(self.state()['last_worker_invocation']['round'], 2)
        head = self.fast_forward()
        worker = self.worker()
        start = len(self.calls())
        report = self.run_one_worker(self.monitor(worker, reviewer, resume=True))
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls()[start:], [('worker', 2)])
        self.assert_delivery(original, head)

    def test_rejected_branch_switch_stops_before_model_dispatch(self):
        original = self.stop_after_ready()
        head = _git(self.root, 'rev-parse', 'HEAD')
        history_before = (self.run_dir / 'git-history.json').read_bytes()
        _git(self.root, 'checkout', '-qb', 'other-branch')
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        start = self.calls()
        report = self.monitor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'recovery_failed', report.summary)
        self.assertIn('Business branch changed', report.summary)
        self.assertEqual(self.calls(), start)
        self.assertEqual((self.run_dir / 'git-history.json').read_bytes(), history_before)
        self.assert_delivery(original, head, commits=2)
