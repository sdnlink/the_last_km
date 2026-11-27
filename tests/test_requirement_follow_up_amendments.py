"""The next requirement's first worker uses directions seen by its reviewer."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.directions import Directions
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_requirement_follow_up import BRIEF, FOLLOW, NEXT, CitingReviewer, _git
from tests.test_review_first import approve_requirement_coverage
from tests.test_supervisor import ScriptedProvider, _verdict


DIRECTION = 'Use the next cycle to reject negative input. Leave empty input behavior alone.'
AMENDED = {
    **NEXT,
    'refined_requirement': 'Reject negative input in app.py and preserve empty input behavior.',
    'acceptance_criteria': ['negative input is rejected', 'empty input behavior is preserved'],
}


class RequirementFollowUpAmendmentsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run_dir = self.root / 'run'
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        _git(self.root, 'checkout', '-qb', 'main')
        (self.root / 'app.py').write_text('print("base")\n', encoding='utf-8')
        _git(self.root, 'add', 'app.py')
        _git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=test@local', 'commit', '-qm', 'base')
        self.base = _git(self.root, 'rev-parse', 'HEAD')

    def supervisor(self, worker, reviewer, **kwargs):
        return Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='review-first',
            run_dir=self.run_dir, git_history='required', retry_delay_s=0, max_rounds=2,
            checks=['test -f app.py && echo check >> run/check-count'],
            **kwargs,
        ), log=lambda _message: None)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def calls(self):
        events = [json.loads(line) for line in (self.run_dir / 'events.jsonl').read_text().splitlines()]
        return [(event['role'], int(event['round'])) for event in events if event['event'] == 'agent_start']

    def brief(self, value, **kwargs):
        return {'verdict': {}, 'text': json.dumps(value), **kwargs}

    def initial_agents(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
        ])
        reviewer = CitingReviewer([
            self.brief(BRIEF), {'verdict': _verdict('done'), 'cite': True}, self.brief(NEXT),
        ], self.root)
        return worker, reviewer

    def next_worker(self):
        return ScriptedProvider('worker', [{'verdict': _verdict('continue', ['still implementing'])}])

    def stop_after_ready(self, **kwargs):
        worker, reviewer = self.initial_agents()
        monitor = self.supervisor(worker, reviewer, **kwargs)
        monitor.log = lambda message: monitor.request_stop() if 'continuing next requirement elaboration' in message else None
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls(), [('reviewer', 0), ('worker', 1), ('reviewer', 1), ('reviewer', 900001)])
        state = self.state()
        self.assertEqual(state['requirements_review']['status'], 'ready')
        self.assertEqual(state['requirements_review']['refined_requirement'], NEXT['refined_requirement'])
        self.assertNotEqual(_git(self.root, 'rev-parse', 'HEAD'), self.base)
        self.assert_publication(state)
        return state

    def assert_publication(self, original):
        state = self.state()
        self.assertEqual(state['requirement_cycles'], original['requirement_cycles'])
        handoff = state['requirement_cycles'][0]['handoff']
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), handoff['commit'])
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')
        first_worker, first_reviewer = state['rounds'][:2]
        receipt = first_worker['watcher_result']['candidate_checks']
        approval = first_reviewer['watcher_result']['candidate_approval']
        self.assertEqual(receipt['digest'], approval['checks_digest'])
        self.assertEqual(state['rounds'][0]['watcher_result']['candidate_checks'],
                         original['rounds'][0]['watcher_result']['candidate_checks'])
        self.assertEqual(approval, original['rounds'][1]['watcher_result']['candidate_approval'])
        self.assertEqual((self.run_dir / 'check-count').read_text().splitlines(), ['check'])

    def assert_amended_dispatch(self, amendment, worker_call, review, original):
        self.assertTrue(review['read_only'])
        self.assertIn('Accepted deliveries already committed', review['prompt'])
        self.assertIn(original['requirement_cycles'][0]['handoff']['commit'], review['prompt'])
        self.assertIn(amendment['id'], review['prompt'])
        self.assertIn(DIRECTION, review['prompt'])
        self.assertFalse(worker_call['read_only'])
        prompt = worker_call['prompt']
        self.assertIn(AMENDED['refined_requirement'], prompt)
        self.assertNotIn(NEXT['refined_requirement'], prompt)
        self.assertIn(amendment['id'], prompt)
        state = self.state()
        self.assertEqual(state['requirements_review']['refined_requirement'], AMENDED['refined_requirement'])
        revision = state['last_worker_invocation']['requirement_revision']
        self.assertEqual(state['requirements_review']['reviewed_direction_sha256'], revision['sha256'])
        self.assertEqual([item['direction_id'] for item in revision['amendments']], [amendment['id']])
        context = json.loads((self.run_dir / 'requirement-review-context.json').read_text())
        self.assertEqual(context['direction_ids'], [amendment['id']])
        self.assert_publication(original)

    def test_amendment_before_original_worker_requires_updated_reviewed_brief(self):
        amendment = 'Also reject negative input.'
        revised = {**BRIEF, 'refined_requirement': 'Handle empty and negative inputs.',
                   'acceptance_criteria': [*BRIEF['acceptance_criteria'], 'negative input is rejected']}
        worker = self.next_worker()
        reviewer = CitingReviewer([self.brief(BRIEF), self.brief(revised), approve_requirement_coverage()], self.root)
        monitor = self.supervisor(worker, reviewer)
        original_beat = monitor._beat
        added = []

        def amend_at_dispatch(note, *args, **kwargs):
            if note == 'round 1: starting worker' and not added:
                added.append(Directions(self.run_dir).append(amendment))
            return original_beat(note, *args, **kwargs)

        with patch.object(monitor, '_beat', side_effect=amend_at_dispatch), \
                patch.object(monitor, '_invoke', wraps=monitor._invoke) as invoke:
            monitor.run()
        self.assertEqual([call.kwargs['role'] for call in invoke.call_args_list[:4]],
                         ['reviewer', 'reviewer', 'reviewer', 'worker'])
        self.assertIn(revised['refined_requirement'], worker.calls[0]['prompt'])
        self.assertNotIn(amendment, worker.calls[0]['prompt'])
        self.assertEqual(self.state()['requirements_review']['acceptance_criteria'], revised['acceptance_criteria'])

    def test_resume_reviews_amendment_before_first_worker(self):
        original = self.stop_after_ready()
        amendment = Directions(self.run_dir).append(DIRECTION)
        worker = self.next_worker()
        reviewer = CitingReviewer([self.brief(AMENDED)], self.root)
        prior_calls = len(self.calls())
        report = self.supervisor(worker, reviewer, resume=True).run()
        stale = bool(worker.calls and NEXT['refined_requirement'] in worker.calls[0]['prompt'])
        self.assertEqual(self.calls()[prior_calls:], [('reviewer', 900001), ('worker', 2)],
                         f'stale brief reached worker: {stale}')
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(len(worker.calls), 1)
        self.assert_amended_dispatch(amendment, worker.calls[0], reviewer.calls[0], original)

    def test_same_process_amendment_at_dispatch_reopens_ready_brief(self):
        worker, reviewer = self.initial_agents()
        worker.scripts.extend(self.next_worker().scripts)
        reviewer.scripts.append(self.brief(AMENDED))
        monitor = self.supervisor(worker, reviewer)
        saved = []
        amendments = []

        def amend_before_worker(note):
            if note == 'round 2: worker running' and not amendments:
                saved.append(self.state())
                amendments.append(Directions(self.run_dir).append(DIRECTION))

        monitor.log = amend_before_worker
        report = monitor.run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(self.calls()[-3:], [('reviewer', 900001), ('reviewer', 900001), ('worker', 2)])
        self.assertEqual(saved[0]['requirements_review']['refined_requirement'], NEXT['refined_requirement'])
        self.assertEqual(len(worker.calls), 2)
        self.assert_amended_dispatch(amendments[0], worker.calls[1], reviewer.calls[-1], saved[0])

    def test_worker_invocation_uses_validated_snapshot_when_a_later_direction_arrives(self):
        original = self.stop_after_ready()
        worker = self.next_worker()
        reviewer = CitingReviewer([], self.root)
        monitor = self.supervisor(worker, reviewer, resume=True)
        invoke = monitor._invoke
        later = []

        def amend_after_dispatch_boundary(*args, **kwargs):
            later.append(Directions(self.run_dir).append(DIRECTION))
            return invoke(*args, **kwargs)

        with patch.object(monitor, '_invoke', side_effect=amend_after_dispatch_boundary):
            report = monitor.run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertNotIn(DIRECTION, worker.calls[0]['prompt'])
        self.assertNotIn(later[0]['id'], worker.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['last_worker_invocation']['requirement_revision']['amendments'], [])
        self.assertEqual([item['direction_id'] for item in state['requirement_revision']['amendments']], [later[0]['id']])
        self.assertTrue(monitor._pending_directions())
        self.assert_publication(original)

    def test_resume_reuses_ready_brief_without_new_directions(self):
        original = self.stop_after_ready()
        worker = self.next_worker()
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(self.state()['requirements_review'], original['requirements_review'])
        self.assert_publication(original)

    def stop_after_amended_ready(self, original):
        amendment = Directions(self.run_dir).append(DIRECTION)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([self.brief(AMENDED)], self.root)
        monitor = self.supervisor(worker, reviewer, resume=True)

        def stop_at_dispatch(note):
            if note == 'round 2: worker running' and reviewer.calls:
                monitor.request_stop()

        monitor.log = stop_at_dispatch
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        self.assert_publication(original)
        return amendment, reviewer.calls[0]

    def test_resume_reuses_ready_brief_with_bound_amendment(self):
        original = self.stop_after_ready()
        amendment, review = self.stop_after_amended_ready(original)
        worker = self.next_worker()
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(len(worker.calls), 1)
        self.assert_amended_dispatch(amendment, worker.calls[0], review, original)

    def test_legacy_ready_without_binding_reviews_existing_amendment(self):
        original = self.stop_after_ready()
        amendment, _ = self.stop_after_amended_ready(original)
        legacy = self.state()
        legacy['requirements_review'].pop('reviewed_direction_sha256')
        write_json(self.run_dir / 'state.json', legacy, durable=True)
        write_json(self.run_dir / 'reviewed-requirement.json', legacy['requirements_review'], durable=True)
        (self.run_dir / 'requirement-review-context.json').unlink()
        worker = self.next_worker()
        reviewer = CitingReviewer([self.brief(AMENDED)], self.root)
        prior_calls = len(self.calls())
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(self.calls()[prior_calls:], [('reviewer', 900001), ('worker', 2)])
        self.assertEqual(len(worker.calls), 1)
        self.assert_amended_dispatch(amendment, worker.calls[0], reviewer.calls[0], original)

    def assert_review_cannot_dispatch(self, scripts, status):
        original = self.stop_after_ready()
        amendment = Directions(self.run_dir).append(DIRECTION)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer(scripts, self.root)
        prior_calls = len(self.calls())
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(self.calls()[prior_calls:], [('reviewer', 900001)] * len(scripts))
        self.assertEqual(len(reviewer.calls), len(scripts))
        for call in reviewer.calls:
            self.assertTrue(call['read_only'])
            self.assertIn(amendment['id'], call['prompt'])
        self.assertEqual(self.state()['requirements_review']['status'], status)
        self.assertEqual(self.state()['next_round'], 2)
        self.assert_publication(original)

    def test_failed_amendment_review_preserves_delivery_without_dispatching(self):
        self.assert_review_cannot_dispatch([self.brief(AMENDED, exit_code=7)], 'failed')

    def test_needs_input_amendment_review_preserves_delivery_without_dispatching(self):
        blocked = self.brief({**AMENDED, 'status': 'needs_input', 'blockers': ['A domain constraint is missing']})
        self.assert_review_cannot_dispatch([blocked, blocked], 'needs_input')

    def test_stop_before_amendment_review_preserves_delivery(self):
        original = self.stop_after_ready()
        Directions(self.run_dir).append(DIRECTION)
        write_json(self.run_dir / 'control.json', {'action': 'stop'}, durable=True)
        prior_calls = self.calls()
        report = self.supervisor(ScriptedProvider('worker', []), CitingReviewer([], self.root), resume=True).run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(self.calls(), prior_calls)
        self.assertEqual(self.state()['requirements_review'], original['requirements_review'])
        self.assert_publication(original)

    def test_exhausted_call_budget_after_amendment_review_prevents_worker_dispatch(self):
        original = self.stop_after_ready(max_agent_calls=5)
        amendment = Directions(self.run_dir).append(DIRECTION)
        prior_calls = len(self.calls())
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([self.brief(AMENDED)], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(self.calls()[prior_calls:], [('reviewer', 900001)])
        self.assertIn(amendment['id'], reviewer.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['usage']['agent_calls'], 5)
        self.assertEqual(state['requirements_review']['status'], 'ready')
        self.assertEqual(state['requirements_review']['refined_requirement'], AMENDED['refined_requirement'])
        self.assertEqual(state['next_round'], 2)
        self.assert_publication(original)

    def test_pause_at_first_dispatch_blocks_amendment_review_until_control_changes(self):
        original = self.stop_after_ready()
        Directions(self.run_dir).append(DIRECTION)
        prior_calls = self.calls()
        monitor = self.supervisor(ScriptedProvider('worker', []), CitingReviewer([], self.root), resume=True)
        control = self.run_dir / 'control.json'
        monitor.log = lambda note: write_json(control, {'action': 'pause'}, durable=True) if note == 'round 2: worker running' else None
        beat = monitor._beat
        observed = []

        def stop_while_paused(note):
            beat(note)
            if note == 'paused at a turn boundary':
                observed.append(self.calls())
                write_json(control, {'action': 'stop'}, durable=True)

        with patch.object(monitor, '_beat', side_effect=stop_while_paused):
            report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(observed, [prior_calls])
        self.assertEqual(self.calls(), prior_calls)
        self.assert_publication(original)

    def assert_control_during_amendment_review_preparation(self, action):
        original = self.stop_after_ready()
        Directions(self.run_dir).append(DIRECTION)
        prior_calls = self.calls()
        prior_brief = (self.run_dir / 'reviewed-requirement.json').read_bytes()
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([self.brief(AMENDED)], self.root)
        monitor = self.supervisor(worker, reviewer, resume=True)
        control = self.run_dir / 'control.json'
        beat = monitor._beat
        paused = []

        def control_during_preparation(note):
            if note.startswith('requirements: reviewer continuing saved analysis'):
                write_json(control, {'action': action}, durable=True)

        def stop_while_paused(note):
            beat(note)
            if note == 'paused at a turn boundary':
                paused.append((self.calls(), self.state()['usage']['agent_calls']))
                write_json(control, {'action': 'stop'}, durable=True)

        command = reviewer._command

        def stop_if_dispatched(*args):
            # Keep the regression bounded even when the pending pause is ignored.
            write_json(control, {'action': 'stop'}, durable=True)
            return command(*args)

        monitor.log = control_during_preparation
        with (patch.object(monitor, '_beat', side_effect=stop_while_paused),
              patch.object(reviewer, '_command', side_effect=stop_if_dispatched)):
            report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual((len(reviewer.calls), len(worker.calls)), (0, 0))
        self.assertEqual(self.calls(), prior_calls)
        state = self.state()
        prior_usage = original['usage']['agent_calls']
        self.assertEqual(paused, [(prior_calls, prior_usage)] if action == 'pause' else [])
        self.assertEqual(state['usage']['agent_calls'], prior_usage)
        self.assertEqual(state['last_invocation'], original['last_invocation'])
        self.assertEqual(state['next_round'], 2)
        self.assertEqual((self.run_dir / 'reviewed-requirement.json').read_bytes(), prior_brief)
        self.assert_publication(original)

    def test_pause_before_reviewer_launch_waits_without_charging_a_call(self):
        self.assert_control_during_amendment_review_preparation('pause')

    def test_stop_before_reviewer_launch_does_not_charge_a_call(self):
        self.assert_control_during_amendment_review_preparation('stop')

    def test_exhausted_call_budget_does_not_start_amendment_review(self):
        original = self.stop_after_ready(max_agent_calls=4)
        Directions(self.run_dir).append(DIRECTION)
        prior_calls = self.calls()
        report = self.supervisor(ScriptedProvider('worker', []), CitingReviewer([], self.root), resume=True).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertIn('Agent call budget exhausted', report.summary)
        self.assertEqual(self.calls(), prior_calls)
        self.assertEqual(self.state()['usage']['agent_calls'], 4)
        self.assertEqual(self.state()['requirements_review'], original['requirements_review'])
        self.assert_publication(original)
