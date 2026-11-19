"""Bind the next requirement's first dispatch to the reviewed commands."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import candidate_checks
from auto_moniter.directions import Directions
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.tasks import TaskStore
from tests.test_requirement_follow_up import BRIEF, FOLLOW, NEXT, CitingReviewer, _git
from tests.test_supervisor import ScriptedProvider, _verdict


CHECKS = ['test -f app.py', 'echo fixed >> run/check-count']
ADDED = 'echo appended >> run/check-count'
DIRECTION = 'Keep the earlier delivery and exercise the new check.'
UPDATED = {
    **NEXT,
    'refined_requirement': 'Reject empty input and verify the behavior with the additional check.',
}


def brief(value, **kwargs):
    return {'verdict': {}, 'text': json.dumps(value), **kwargs}


def delivered(**kwargs):
    return {'verdict': _verdict('done', evidence=['app.py']), **kwargs}


class FollowUpCheckBindingTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = directory / 'workspace'
        self.root.mkdir()
        self.run_dir = self.root / 'run'
        self.enterContext(patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(directory / 'state')}))
        _git(self.root, 'init', '-q')
        _git(self.root, 'checkout', '-qb', 'main')
        (self.root / 'app.py').write_text('base\n')
        _git(self.root, 'add', 'app.py')
        _git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
             'commit', '-qm', 'base')
        self.store = TaskStore(directory / 'console', self.root)
        task = self.store.create({'title': 'Fixture', 'requirement': FOLLOW, 'checks': CHECKS,
                                  'workflow': 'review-first', 'function_dispatch': False})
        self.task_id = task['id']
        self.store._save({**self.store._get(self.task_id), 'run_dir': str(self.run_dir)})

    def supervisor(self, worker, reviewer, *, resume=False, **kwargs):
        return Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='review-first',
            resume=resume, run_dir=self.run_dir, git_history='required', retry_delay_s=0,
            max_rounds=2, checks=[] if resume else list(CHECKS), **kwargs,
        ), log=lambda _: None)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def calls(self):
        events = (json.loads(line) for line in (self.run_dir / 'events.jsonl').read_text().splitlines())
        return [(event['role'], int(event['round'])) for event in events if event['event'] == 'agent_start']

    def context(self):
        return json.loads((self.run_dir / 'requirement-review-context.json').read_text())

    def stop_after_ready(self, *, amend=False, **kwargs):
        worker = ScriptedProvider('worker', [delivered(touch='app.py')])
        reviewer = CitingReviewer([brief(BRIEF), delivered(cite=True), brief(NEXT)], self.root)
        monitor = self.supervisor(worker, reviewer, **kwargs)
        def stop_before_worker(note):
            if amend and note.startswith('requirements: reviewer elaborating next requirement'):
                Directions(self.run_dir).append(DIRECTION)
            if 'continuing next requirement elaboration' in note:
                monitor.request_stop()

        monitor.log = stop_before_worker
        result = monitor.run()
        self.assertEqual(result.status, 'stopped', result.summary)
        state = self.state()
        self.assertEqual(state['requirements_review']['status'], 'ready')
        self.assertEqual(self.calls(), [('reviewer', 0), ('worker', 1), ('reviewer', 1), ('reviewer', 900001)])
        self.assertEqual((self.run_dir / 'check-count').read_text().splitlines(), ['fixed'])
        return state

    def append_check(self, command):
        # Use the actual console action and inactive-run lock. The fixture
        # resumes via Supervisor.run with scripted providers, not the CLI.
        with patch.object(self.store, '_resume_args', return_value=['run', '--resume']):
            self.store.action(self.task_id, 'checks', {'additional_checks': [command]})

    def assert_prior_delivery(self, original):
        current = self.state()
        self.assertEqual(current['requirement_cycles'][0], original['requirement_cycles'][0])
        self.assertEqual(current['rounds'][:2], original['rounds'][:2])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'),
                         original['requirement_cycles'][0]['handoff']['commit'])
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def assert_ready_binding(self, state, checks):
        reviewed = state['requirements_review']
        self.assertEqual(reviewed['status'], 'ready')
        self.assertEqual(reviewed['cycle'], 2)
        self.assertEqual(reviewed['reviewed_direction_sha256'], state['requirement_revision']['sha256'])
        self.assertEqual(reviewed['reviewed_checks_sha256'],
                         hashlib.sha256(json.dumps(checks, ensure_ascii=False).encode()).hexdigest())
        context = self.context()
        self.assertEqual(context['checks'], reviewed['reviewed_checks_sha256'])
        self.assertEqual(context['cycle'], 2)
        self.assertEqual(context['response']['refined_requirement'], reviewed['refined_requirement'])
        self.assertEqual(context['direction_ids'],
                         [amendment['direction_id'] for amendment in state['requirement_revision']['amendments']])

    def test_resume_reviews_appended_check_before_dispatch_and_checks_candidate(self):
        original = self.stop_after_ready(amend=True)
        self.append_check(ADDED)
        self.append_check(' ' + ADDED + ' ')
        self.assertEqual(self.state()['checks'], [*CHECKS, ADDED])
        self.assertEqual([change['added'] for change in self.state()['check_changes']], [[ADDED]])
        worker = ScriptedProvider('worker', [delivered()])
        reviewer = CitingReviewer([brief(UPDATED), delivered(cite=True)], self.root)
        monitor = self.supervisor(worker, reviewer, resume=True)
        ready = []

        def observe_dispatch(note):
            if note == 'round 2: worker running' and reviewer.calls:
                saved = self.state()
                self.assert_ready_binding(saved, [*CHECKS, ADDED])
                self.assertEqual(saved['requirements_review'],
                                 json.loads((self.run_dir / 'reviewed-requirement.json').read_text()))
                ready.append(saved['requirements_review'])
            if note.startswith('requirements: reviewer elaborating next requirement') and len(reviewer.calls) == 2:
                monitor.request_stop()

        monitor.log = observe_dispatch
        start = len(self.calls())
        result = monitor.run()
        stale = bool(worker.calls and NEXT['refined_requirement'] in worker.calls[0]['prompt'])
        self.assertEqual(self.calls()[start:], [('reviewer', 900001), ('worker', 2), ('reviewer', 2)],
                         f'stale brief reached worker: {stale}')
        self.assertEqual(result.status, 'stopped', result.summary)
        self.assertEqual(len(ready), 1)
        review = reviewer.calls[0]
        self.assertTrue(review['read_only'])
        self.assertIn('Immutable commands:\n' + json.dumps([*CHECKS, ADDED]), review['prompt'])
        self.assertIn(FOLLOW, review['prompt'])
        self.assertIn(DIRECTION, review['prompt'])
        self.assertIn(original['requirement_cycles'][0]['handoff']['commit'], review['prompt'])
        self.assertIn(UPDATED['refined_requirement'], worker.calls[0]['prompt'])
        self.assertNotIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['requirement_cycles'][1]['requirements_review'], ready[0])
        current_worker, current_reviewer = state['rounds'][2:4]
        receipt = current_worker['watcher_result']['candidate_checks']
        self.assertTrue(candidate_checks.valid(receipt, self.run_dir, receipt['context']))
        self.assertEqual(receipt['context']['configured_checks'], [*CHECKS, ADDED])
        self.assertEqual(receipt['context']['commands'], [*CHECKS, ADDED])
        old_receipt = original['rounds'][0]['watcher_result']['candidate_checks']
        self.assertEqual(old_receipt['context']['tree'], receipt['context']['tree'])
        self.assertTrue(candidate_checks.valid(old_receipt, self.run_dir, old_receipt['context']))
        self.assertFalse(candidate_checks.valid(old_receipt, self.run_dir, receipt['context']))
        self.assertNotIn('reused_from', receipt)
        self.assertEqual(current_reviewer['watcher_result']['candidate_approval']['checks_digest'], receipt['digest'])
        self.assertEqual((self.run_dir / 'check-count').read_text().splitlines(), ['fixed', 'fixed', 'appended'])
        self.assert_prior_delivery(original)

    def resume_work_in_progress(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue', ['work remains'])}])
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        return worker.calls[0]['prompt'], reviewer

    def test_resume_reuses_ready_with_matching_checks_when_console_gets_duplicate(self):
        original = self.stop_after_ready()
        self.assert_ready_binding(original, CHECKS)
        self.append_check(CHECKS[-1])
        self.assertEqual(self.state()['checks'], CHECKS)
        self.assertEqual(self.state()['check_changes'], [])
        prompt, reviewer = self.resume_work_in_progress()
        self.assertIn(NEXT['refined_requirement'], prompt)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.state()['requirements_review'], original['requirements_review'])
        self.assert_prior_delivery(original)

    def legacy_checkpoint(self, state):
        brief = dict(state['requirements_review'])
        del brief['reviewed_checks_sha256']
        write_json(self.run_dir / 'state.json', {**state, 'requirements_review': brief}, durable=True)
        write_json(self.run_dir / 'reviewed-requirement.json', brief, durable=True)

    def test_resume_reuses_legacy_ready_with_matching_context(self):
        original = self.stop_after_ready(amend=True)
        self.legacy_checkpoint(original)
        prompt, reviewer = self.resume_work_in_progress()
        self.assertEqual(reviewer.calls, [])
        self.assertIn(NEXT['refined_requirement'], prompt)
        self.assertIn(DIRECTION, prompt)
        self.assert_prior_delivery(original)

    def test_legacy_ready_without_matching_context_reopens_review(self):
        original = self.stop_after_ready()
        context = self.context()
        context_file = self.run_dir / 'requirement-review-context.json'
        replacements = {
            'missing': None,
            'corrupt': '}',
            'other cycle': json.dumps({**context, 'cycle': 1}),
            'other response': json.dumps({**context, 'response': UPDATED}),
            'other revision': json.dumps({**context, 'direction_ids': ['previous-direction']}),
            'other workspace': json.dumps({**context, 'cwd': str(self.root.parent)}),
            'other check order': json.dumps({
                **context, 'checks': hashlib.sha256(json.dumps(CHECKS[::-1]).encode()).hexdigest(),
            }),
        }
        # Stop just before dispatch so every case starts from the same
        # checkpoint before its first Worker.
        for case, replacement in replacements.items():
            with self.subTest(case=case):
                self.legacy_checkpoint(original)
                if replacement is None:
                    context_file.unlink()
                else:
                    context_file.write_text(replacement)
                reviewer = CitingReviewer([brief(UPDATED)], self.root)
                worker = ScriptedProvider('worker', [])
                monitor = self.supervisor(worker, reviewer, resume=True)
                monitor.log = lambda note: (
                    monitor.request_stop() if note == 'round 2: worker running' and reviewer.calls else None
                )
                start = len(self.calls())
                report = monitor.run()
                self.assertEqual(report.status, 'stopped', report.summary)
                self.assertEqual(self.calls()[start:], [('reviewer', 900001)])
                self.assertTrue(reviewer.calls[0]['read_only'])
                self.assert_ready_binding(self.state(), CHECKS)
                self.assert_prior_delivery(original)

    def test_legacy_ready_with_pre_addition_context_reopens_review(self):
        original = self.stop_after_ready()
        self.legacy_checkpoint(original)
        self.append_check(ADDED)
        reviewer = CitingReviewer([brief(UPDATED)], self.root)
        worker = ScriptedProvider('worker', [])
        monitor = self.supervisor(worker, reviewer, resume=True)
        monitor.log = lambda note: (
            monitor.request_stop() if note == 'round 2: worker running' and reviewer.calls else None
        )
        start = len(self.calls())
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls()[start:], [('reviewer', 900001)])
        self.assertTrue(reviewer.calls[0]['read_only'])
        refreshed = self.state()
        self.assert_ready_binding(refreshed, [*CHECKS, ADDED])

        self.append_check(ADDED)
        prompt, reviewer = self.resume_work_in_progress()
        self.assertIn(UPDATED['refined_requirement'], prompt)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.state()['requirements_review'], refreshed['requirements_review'])
        self.assert_prior_delivery(original)

    def assert_resume_blocks_worker(self, scripts, report_status, review_status, call_limit, review_calls):
        original = self.stop_after_ready(max_agent_calls=call_limit)
        self.append_check(ADDED)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer(scripts, self.root)
        start = len(self.calls())
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, report_status, report.summary)
        self.assertEqual(self.calls()[start:], [('reviewer', 900001)] * review_calls)
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertEqual(worker.calls, [])
        state = self.state()
        self.assertEqual(state['requirements_review']['status'], review_status)
        self.assertEqual(state['next_round'], 2)
        self.assertEqual(state['usage']['agent_calls'], original['usage']['agent_calls'] + review_calls)
        self.assertEqual((self.run_dir / 'check-count').read_text().splitlines(), ['fixed'])
        self.assert_prior_delivery(original)
        return state

    def test_needs_input_blocks_worker(self):
        blocked = brief({**UPDATED, 'status': 'needs_input', 'blockers': ['Missing expected input']})
        self.assert_resume_blocks_worker([blocked, blocked], 'action_required', 'needs_input', 0, 2)

    def test_reviewer_failure_blocks_worker(self):
        self.assert_resume_blocks_worker([brief(UPDATED, exit_code=7)], 'action_required', 'failed', 0, 1)

    def test_no_budget_for_new_review(self):
        self.assert_resume_blocks_worker([], 'budget_exceeded', 'ready', 4, 0)

    def test_review_uses_last_call_without_dispatching_worker(self):
        state = self.assert_resume_blocks_worker([brief(UPDATED)], 'budget_exceeded', 'ready', 5, 1)
        self.assert_ready_binding(state, [*CHECKS, ADDED])

    def assert_control_before_review_blocks_dispatch(self, action):
        original = self.stop_after_ready()
        self.append_check(ADDED)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        monitor = self.supervisor(worker, reviewer, resume=True)
        control = self.run_dir / 'control.json'
        start = self.calls()
        paused = []
        beat = monitor._beat

        def before_review(note):
            if note.startswith('requirements: reviewer continuing saved analysis'):
                write_json(control, {'action': action}, durable=True)

        def stop_during_pause(note):
            beat(note)
            if note == 'paused at a turn boundary':
                paused.append(self.calls())
                write_json(control, {'action': 'stop'}, durable=True)

        monitor.log = before_review
        with patch.object(monitor, '_beat', side_effect=stop_during_pause):
            report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(paused, [start] if action == 'pause' else [])
        self.assertEqual(self.calls(), start)
        self.assertEqual(self.state()['next_round'], 2)
        self.assertEqual(self.state()['usage']['agent_calls'], original['usage']['agent_calls'])
        self.assertEqual((self.run_dir / 'check-count').read_text().splitlines(), ['fixed'])
        self.assert_prior_delivery(original)

    def test_stop_before_review_dispatch(self):
        self.assert_control_before_review_blocks_dispatch('stop')

    def test_pause_before_review_dispatch(self):
        self.assert_control_before_review_blocks_dispatch('pause')
