"""Resume a requirement after publication and before its handoff is saved."""
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.directions import Directions
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_modules import PLAN, outcome
from tests.test_requirement_follow_up import BRIEF, FOLLOW, NEXT, CitingReviewer, _git
from tests.test_supervisor import ScriptedProvider, _verdict


class ProcessExit(BaseException):
    """Leave the actual durable checkpoint without running exception cleanup."""


class RequirementHandoffRecoveryTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(root)
        _git(self.root, 'init', '-q')
        _git(self.root, 'checkout', '-qb', 'main')
        (self.root / 'app.py').write_text('base\n')
        _git(self.root, 'add', 'app.py')
        _git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
             'commit', '-qm', 'base')
        self.run_dir = self.root / 'run'
        self.counter = self.run_dir / 'check-count'

    def supervisor(self, worker, reviewer, *, resume=False, max_rounds=0):
        return Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer,
            workflow='auto' if resume else 'review-first', resume=resume,
            run_dir=self.run_dir, git_history='required', retry_delay_s=0, max_rounds=max_rounds,
            checks=[] if resume else [f'echo check >> {shlex.quote(str(self.counter))} && test -f app.py'],
        ), log=lambda _: None)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def publish_then_interrupt(self):
        worker = ScriptedProvider('worker', [
            {'verdict': outcome(['A'], plan=PLAN), 'touch': 'app.py'},
            {'verdict': outcome(['B'])},
            {'verdict': outcome(['A', 'B'], scope='requirement')},
        ])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': outcome(['A']), 'cite': True},
            {'verdict': outcome(['B']), 'cite': True},
            {'verdict': outcome(['A', 'B'], scope='requirement'), 'cite': True},
        ], self.root)
        monitor = self.supervisor(worker, reviewer)
        with patch.object(monitor, '_begin_follow_up', side_effect=ProcessExit):
            with self.assertRaises(ProcessExit):
                report = monitor.run()
                self.fail(report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 4))
        return self.state()

    def test_resume_published_final_after_all_modules_accepted(self):
        before = self.publish_then_interrupt()
        self.assertEqual(before['requirement_cycles'], [])
        self.assertEqual([item['status'] for item in before['modules']], ['accepted', 'accepted'])
        receipt = before['rounds'][-2]['watcher_result']['candidate_checks']
        approved = before['rounds'][-1]
        publication = approved['git_checkpoint']['business_commit']
        git_history = (self.run_dir / 'git-history.json').read_bytes()
        self.assertFalse(publication['created'])
        head = _git(self.root, 'rev-parse', 'HEAD')
        self.assertEqual(head, publication['commit'])
        self.assertEqual(self.counter.read_text().splitlines(), ['check'])

        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', ['next requirement needs work']), 'touch': 'next.py'},
        ])
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(NEXT)}], self.root)
        resumed = self.supervisor(worker, reviewer, resume=True)
        resumed.log = lambda message: resumed.request_stop() if 'worker running' in message else None
        report = resumed.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertTrue(reviewer.calls[0]['read_only'])
        self.assertIn('Accepted deliveries already committed', reviewer.calls[0]['prompt'])
        self.assertIn(head, reviewer.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['requirements_review']['refined_requirement'], NEXT['refined_requirement'])
        self.assertEqual(len(state['requirement_cycles']), 1)
        handoff = state['requirement_cycles'][0]['handoff']
        self.assertEqual(handoff['cycle'], 1)
        self.assertEqual(handoff['round'], 3)
        self.assertEqual(handoff['tree'], receipt['context']['tree'])
        self.assertEqual(handoff['commit'], head)
        self.assertEqual(handoff['ref'], 'refs/heads/main')
        self.assertEqual(handoff['checks_digest'], receipt['digest'])
        self.assertEqual([item['id'] for item in handoff['modules']], ['A', 'B'])
        self.assertEqual(state['rounds'][-1]['watcher_result']['candidate_approval'],
                         approved['watcher_result']['candidate_approval'])
        self.assertEqual((self.run_dir / 'git-history.json').read_bytes(), git_history)

        reviewer = CitingReviewer([], self.root)
        resumed = self.supervisor(worker, reviewer, resume=True, max_rounds=4)
        report = resumed.run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.state()['requirement_cycles'], state['requirement_cycles'])
        self.assertEqual(self.counter.read_text().splitlines(), ['check'])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def assert_revalidation(self, original, head, worker, reviewer):
        resumed = self.supervisor(worker, reviewer, resume=True, max_rounds=4)
        report = resumed.run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertTrue(reviewer.calls[0]['read_only'])
        self.assertIn('Worker checkpoint:', reviewer.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['requirement_cycles'], [])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), head)
        approved = original['rounds'][-1]
        self.assertEqual(state['git_history']['last_business_commit'],
                         approved['git_checkpoint']['business_commit'])
        self.assertEqual(state['git_history']['head'], original['git_history']['head'])
        old_receipt = original['rounds'][-2]['watcher_result']['candidate_checks']
        worker_rounds = [row for row in state['rounds'] if row['role'] == 'worker']
        self.assertEqual(worker_rounds[-2]['watcher_result']['candidate_checks'], old_receipt)
        self.assertEqual(next(row for row in state['rounds'] if row['role'] == 'reviewer' and row['index'] == 3)
                         ['watcher_result']['candidate_approval'], approved['watcher_result']['candidate_approval'])
        new_receipt = worker_rounds[-1]['watcher_result']['candidate_checks']
        self.assertNotEqual(new_receipt['digest'], old_receipt['digest'])
        self.assertNotIn('reused_from', new_receipt)
        self.assertEqual(self.counter.read_text().splitlines(), ['check', 'check'])
        return new_receipt

    def revalidation_agents(self):
        worker = ScriptedProvider('worker', [{'verdict': outcome(['A', 'B'], scope='requirement')}])
        reviewer = CitingReviewer([{'verdict': _verdict('continue', ['verify the current inputs'])}], self.root)
        return worker, reviewer

    def test_direction_after_publication_is_dispatched_in_original_requirement(self):
        original = self.publish_then_interrupt()
        head = _git(self.root, 'rev-parse', 'HEAD')
        direction = 'Verify the existing behavior with the amended input constraints.'
        message = Directions(self.run_dir).append(direction)
        worker, reviewer = self.revalidation_agents()
        receipt = self.assert_revalidation(original, head, worker, reviewer)
        self.assertIn(direction, worker.calls[0]['prompt'])
        self.assertEqual(receipt['context']['direction_ids'], [message['id']])

    def test_adopted_business_fast_forward_revalidates_after_another_resume(self):
        original = self.publish_then_interrupt()
        (self.root / 'external.py').write_text('new business input\n')
        _git(self.root, 'add', 'external.py')
        _git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
             'commit', '-qm', 'external update')
        head = _git(self.root, 'rev-parse', 'HEAD')
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        resumed = self.supervisor(worker, reviewer, resume=True)
        resumed.log = lambda message: resumed.request_stop() if 'worker running' in message else None
        report = resumed.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.state()['git_history']['business_update_count'], 1)

        worker, reviewer = self.revalidation_agents()
        receipt = self.assert_revalidation(original, head, worker, reviewer)
        self.assertEqual(receipt['context']['business_update_count'], 1)
        self.assertNotEqual(receipt['context']['tree'], original['rounds'][-2]['tree'])

    def test_authorized_replacement_reviewer_starts_the_next_requirement(self):
        original = self.publish_then_interrupt()
        git_history = (self.run_dir / 'git-history.json').read_bytes()
        head = _git(self.root, 'rev-parse', 'HEAD')
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('replacement-reviewer', [{'verdict': {}, 'text': json.dumps(NEXT)}])
        resumed = self.supervisor(worker, reviewer, resume=True)
        assignment = {'id': 'fixture-reviewer-reassignment', 'at': original['updated_at'],
                      'from': original['provider_config'], 'to': resumed._provider_config(),
                      'models': {'worker': '', 'reviewer': ''}}
        write_json(self.run_dir / 'role-change.json', assignment, durable=True)
        resumed.log = lambda message: resumed.request_stop() if 'worker running' in message else None
        report = resumed.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertTrue(reviewer.calls[0]['read_only'])
        self.assertIn('Accepted deliveries already committed', reviewer.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['role_assignment_id'], assignment['id'])
        self.assertEqual(len(state['requirement_cycles']), 1)
        receipt = original['rounds'][-2]['watcher_result']['candidate_checks']
        self.assertEqual(state['requirement_cycles'][0]['handoff']['checks_digest'], receipt['digest'])
        self.assertEqual(state['rounds'][-2]['watcher_result']['candidate_checks'], receipt)
        self.assertEqual(state['rounds'][-1]['watcher_result'], original['rounds'][-1]['watcher_result'])
        self.assertEqual(self.counter.read_text().splitlines(), ['check'])
        self.assertEqual((self.run_dir / 'git-history.json').read_bytes(), git_history)
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), head)

    def test_added_acceptance_command_requires_fresh_checks_and_review(self):
        original = self.publish_then_interrupt()
        head = _git(self.root, 'rev-parse', 'HEAD')
        extra_counter = self.run_dir / 'extra-check-count'
        command = f'echo added >> {shlex.quote(str(extra_counter))}'
        # The console appends commands and their provenance to the saved run.
        updated = {**original, 'checks': [*original['checks'], command],
                   'check_changes': [{'at': original['updated_at'], 'added': [command]}]}
        write_json(self.run_dir / 'state.json', updated, durable=True)
        worker, reviewer = self.revalidation_agents()
        receipt = self.assert_revalidation(original, head, worker, reviewer)
        self.assertEqual(receipt['context']['commands'], updated['checks'])
        self.assertEqual(receipt['context']['configured_checks'], updated['checks'])
        self.assertEqual(extra_counter.read_text().splitlines(), ['added'])
