"""Follow-up reviewers get delivery references and the full current module ledger."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from auto_moniter import requirements_review
from auto_moniter.directions import Directions
from auto_moniter.requirement_queue import RequirementQueue
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_requirement_follow_up import BRIEF, FOLLOW, NEXT, CitingReviewer, _git
from tests.test_supervisor import ScriptedProvider, _verdict


HISTORY = 'Accepted deliveries already committed in this run:\n'
MODULE_CRITERION = 'All original behavior and checks pass'


def block(prompt, heading):
    return prompt.split(heading, 1)[1].split('\n\n', 1)[0]


def brief(value):
    return {'verdict': {}, 'text': json.dumps(value)}


def delivered(*, touch=False, cite=False):
    script = {'verdict': _verdict('done', evidence=['app.py']), 'cite': cite}
    if touch:
        script['touch'] = 'app.py'
    return script


class CapturingReviewer(CitingReviewer):
    def __init__(self, scripts, root, run_dir):
        super().__init__(scripts, root)
        self.state_path = run_dir / 'state.json'
        self.review_states = []

    def _command(self, session, prompt_file, read_only):
        if read_only and prompt_file.name.startswith('900'):
            self.review_states.append(json.loads(self.state_path.read_text()))
        return super()._command(session, prompt_file, read_only)


class RequirementFollowUpHistoryPromptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.root / 'run'
        _git(self.root, 'init', '-q')
        _git(self.root, 'checkout', '-qb', 'main')
        (self.root / 'app.py').write_text('base\n')
        _git(self.root, 'add', 'app.py')
        _git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
             'commit', '-qm', 'base')

    def supervisor(self, worker, reviewer, *, requirement=FOLLOW, resume=False, max_rounds=0):
        return Supervisor(RunConfig(
            requirement, self.root, worker, reviewer=reviewer,
            workflow='auto' if resume else 'review-first', resume=resume,
            run_dir=self.run_dir, max_rounds=max_rounds, git_history='required', retry_delay_s=0,
            checks=['test -f app.py'], acceptance='Preserve accepted behavior.',
        ), log=lambda _: None)

    def reviewer(self, scripts):
        return CapturingReviewer(scripts, self.root, self.run_dir)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def assert_delivery_history(self, call, state):
        self.assertTrue(call['read_only'])
        prompt = call['prompt']
        history = block(prompt, HISTORY)
        handoffs = [cycle['handoff'] for cycle in state['requirement_cycles']]
        full_size = len(json.dumps(handoffs, ensure_ascii=False).encode('utf-8'))
        rendered_size = len(history.encode('utf-8'))
        self.assertNotIn(MODULE_CRITERION, history,
                         f'Historical module definitions repeated in {rendered_size} UTF-8 bytes '
                         f'(serialized handoffs: {full_size} bytes)')
        self.assertLess(rendered_size, full_size)
        entries = json.loads(history)
        self.assertEqual(len(entries), len(handoffs))
        for entry, handoff in zip(entries, handoffs):
            expected = {key: handoff[key] for key in ('cycle', 'round', 'tree', 'commit', 'ref', 'summary')}
            expected['module_ids'] = [module['id'] for module in handoff['modules']
                                      if module['status'] == 'accepted']
            if 'checks_digest' in handoff:
                expected['checks_digest'] = handoff['checks_digest']
            self.assertEqual(entry, expected)
        self.assertIn(str(self.run_dir / 'state.json'), prompt)
        self.assertIn('requirement_cycles', prompt)
        self.assertEqual(json.loads(block(prompt, 'Immutable modules:\n')), state['modules'])
        self.assertIn('Preserve accepted behavior.', prompt)
        self.assertIn('Immutable commands:\n["test -f app.py"]', prompt)

    def test_automatic_follow_up_after_six_deliveries_keeps_current_ledger(self):
        worker = ScriptedProvider('worker', [delivered(touch=True) for _ in range(6)])
        scripts = [brief(BRIEF)]
        for _ in range(6):
            scripts.extend((delivered(cite=True), brief(NEXT)))
        reviewer = self.reviewer(scripts)
        monitor = self.supervisor(worker, reviewer)
        monitor.log = lambda message: (
            monitor.request_stop() if message == 'round 7: worker running' else None
        )

        result = monitor.run()

        self.assertEqual(result.status, 'stopped', result.summary)
        self.assertEqual(len(worker.calls), 6)
        self.assertEqual(len(reviewer.review_states), 6)
        self.assertNotIn(HISTORY, reviewer.calls[0]['prompt'])
        last_state = reviewer.review_states[-1]
        self.assertEqual(len(last_state['requirement_cycles']), 6)
        self.assertTrue(all(entry['handoff'].get('checks_digest')
                            for entry in last_state['requirement_cycles']))
        self.assert_delivery_history(reviewer.calls[-1], last_state)
        self.assertIn(FOLLOW, reviewer.calls[-1]['prompt'])
        self.assertEqual(self.state()['requirement_cycles'], last_state['requirement_cycles'])

    def test_queued_follow_up_keeps_selected_text_and_waits_for_valid_ready(self):
        queue = RequirementQueue(self.run_dir)
        queued = queue.append('排队需求：为空值补充检查。')
        waiting = queue.append('保留在下一位置。')
        worker = ScriptedProvider('worker', [delivered(touch=True)])
        blocked = brief({**NEXT, 'status': 'needs_input', 'blockers': ['Select the input boundary']})
        reviewer = self.reviewer([brief(BRIEF), delivered(cite=True), blocked, blocked])

        result = self.supervisor(worker, reviewer, requirement='Implement the first request.').run()

        self.assertEqual(result.status, 'action_required', result.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.review_states), 2)
        call = reviewer.calls[2]
        self.assert_delivery_history(call, reviewer.review_states[0])
        self.assertIn(json.dumps(queued, ensure_ascii=False), call['prompt'])
        self.assertNotIn(waiting['text'], call['prompt'])
        self.assertEqual(self.state()['requirement_cycles'], reviewer.review_states[0]['requirement_cycles'])
        self.assertEqual(queue.snapshot()['active']['id'], queued['id'])

    def test_resume_from_saved_handoff_preserves_legacy_record_and_publishes_once(self):
        worker = ScriptedProvider('worker', [delivered(touch=True)])
        reviewer = self.reviewer([brief(BRIEF), delivered(cite=True)])
        monitor = self.supervisor(worker, reviewer)
        monitor.log = lambda message: (
            monitor.request_stop() if 'elaborating next requirement' in message else None
        )
        self.assertEqual(monitor.run().status, 'stopped')
        state = self.state()
        handoff = state['requirement_cycles'][0]['handoff']
        self.assertTrue(handoff.pop('checks_digest'))
        # Reproduce a pre-digest handoff in the temporary run, including its original evidence.
        (self.run_dir / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        amendment = Directions(self.run_dir).append('下一项须覆盖空输入。')
        head = _git(self.root, 'rev-parse', 'HEAD')
        count = _git(self.root, 'rev-list', '--count', 'HEAD')

        reviewer = self.reviewer([brief(NEXT), {'verdict': _verdict('continue', ['more work'])}])
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue', ['more work'])}])
        result = self.supervisor(worker, reviewer, resume=True, max_rounds=2).run()

        self.assertEqual(result.status, 'max_rounds', result.summary)
        self.assertEqual(len(reviewer.review_states), 1)
        self.assert_delivery_history(reviewer.calls[0], reviewer.review_states[0])
        self.assertIn(amendment['id'], reviewer.calls[0]['prompt'])
        self.assertIn(amendment['text'], reviewer.calls[0]['prompt'])
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(self.state()['requirement_cycles'], state['requirement_cycles'])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), count)

    def test_history_bytes_do_not_scale_with_historical_module_evidence(self):
        current = [{'id': 'main', 'title': 'Current module', 'acceptance': [MODULE_CRITERION]}]
        config = SimpleNamespace(requirement=FOLLOW, acceptance='Use the fixed checks.', checks=['test -f app.py'])
        history = [
            {'cycle': cycle, 'round': cycle * 2, 'tree': f'{cycle:064x}', 'commit': f'{cycle:040x}',
             'ref': 'refs/heads/main', 'summary': '本地交付通过', 'checks_digest': f'{cycle + 1:064x}',
             'modules': [{'id': 'main', 'status': 'accepted', 'evidence': ['小证据']},
                         {'id': 'pending-module', 'status': 'pending'}],
             'evidence': ['Historical delivery evidence'], 'acceptance': ''}
            for cycle in range(1, requirements_review.MAX_CYCLES)
        ]
        large = copy.deepcopy(history)
        for item in large:
            item['modules'][0]['evidence'] = ['历史检查证据' * 2_000] * 4
            item['modules'][0]['acceptance'] = ['Historical acceptance criteria']
            item['modules'][0]['review_files'] = ['app.py', 'tests/*']
        original = copy.deepcopy(large)

        base = block(requirements_review.prompt(config, current, follow_up=history), HISTORY)
        expanded = block(requirements_review.prompt(config, current, follow_up=large), HISTORY)
        base_size = len(base.encode('utf-8'))
        expanded_size = len(expanded.encode('utf-8'))
        self.assertEqual(expanded_size, base_size, f'UTF-8 history grew from {base_size} to {expanded_size} bytes')
        self.assertEqual(expanded, base)
        entries = json.loads(expanded)
        self.assertEqual([entry['cycle'] for entry in entries], list(range(1, requirements_review.MAX_CYCLES)))
        self.assertTrue(all(entry['module_ids'] == ['main'] for entry in entries))
        self.assertEqual(large, original)
