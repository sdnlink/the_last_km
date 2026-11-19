"""Resume a requirement after publication and before its handoff is saved."""
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter import candidate_checks, requirements_review
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

    def supervisor(self, worker, reviewer, *, resume=False, max_rounds=0,
                   requirement=FOLLOW, max_agent_calls=0):
        return Supervisor(RunConfig(
            requirement, self.root, worker, reviewer=reviewer,
            workflow='auto' if resume else 'review-first', resume=resume,
            run_dir=self.run_dir, git_history='required', retry_delay_s=0, max_rounds=max_rounds,
            max_agent_calls=max_agent_calls,
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

    def publish_new_commit(self, *, requirement=FOLLOW, scope='requirement', max_agent_calls=0):
        base = _git(self.root, 'rev-parse', 'HEAD')
        worker = ScriptedProvider('worker', [
            {'verdict': outcome(['A'], scope=scope, plan=PLAN[:1]), 'touch': 'app.py'},
        ])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': outcome(['A'], scope=scope), 'cite': True},
        ], self.root)
        monitor = self.supervisor(worker, reviewer, requirement=requirement,
                                  max_rounds=1, max_agent_calls=max_agent_calls)
        if scope == 'requirement':
            with patch.object(monitor, '_begin_follow_up', side_effect=ProcessExit):
                with self.assertRaises(ProcessExit):
                    report = monitor.run()
                    self.fail(report.summary)
        else:
            monitor.log = lambda message: monitor.request_stop() if 'module accepted' in message else None
            report = monitor.run()
            self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual([call['read_only'] for call in reviewer.calls], [True, True])
        self.assertIn('Worker checkpoint:', reviewer.calls[-1]['prompt'])
        saved = self.state()
        self.assertEqual(saved['requirement_cycles'], [])
        submitted, reviewed = saved['rounds']
        publication = reviewed['git_checkpoint']['business_commit']
        self.assertTrue(publication['created'])
        self.assertEqual(publication['parent'], base)
        self.assertEqual(publication['changed_files'], ['app.py'])
        self.assertEqual(publication['commit'], _git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual(_git(self.root, 'show', 'HEAD:app.py'), 'changed')
        receipt = submitted['watcher_result']['candidate_checks']
        self.assertEqual(receipt['context']['tree'], submitted['tree'])
        self.assertEqual(receipt['context']['final'], scope == 'requirement')
        self.assertEqual(receipt['context']['commands'], saved['checks'])
        self.assertTrue(candidate_checks.valid(receipt, self.run_dir, receipt['context']))
        self.assertEqual(reviewed['watcher_result']['candidate_approval']['checks_digest'], receipt['digest'])
        self.assertEqual(self.counter.read_text().splitlines(), ['check'])
        return saved

    def publication_evidence(self, saved):
        receipt = saved['rounds'][0]['watcher_result']['candidate_checks']
        files = ['git-history.json', *receipt['logs']]
        return {name: (self.run_dir / name).read_bytes() for name in files}

    def assert_publication_preserved(self, saved, evidence, *, next_worker=False):
        publication = saved['rounds'][1]['git_checkpoint']['business_commit']
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), publication['commit'])
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(_git(self.root, 'rev-parse', saved['git_history']['ref']),
                         saved['git_history']['head'])
        current = self.state()['rounds']
        for original, retained in zip(saved['rounds'], current[:2], strict=True):
            self.assertEqual(retained['git_checkpoint'], original['git_checkpoint'])
            self.assertEqual(retained['watcher_result'], original['watcher_result'])
        retained = self.publication_evidence(saved)
        expected = dict(evidence)
        if next_worker:
            # A new draft updates the candidate and work ref, while the
            # published Git records and check logs still describe round 1.
            original_history = json.loads(expected.pop('git-history.json'))
            current_history = json.loads(retained.pop('git-history.json'))
            self.assertEqual(current_history['candidate']['round'], 2)
            for key in ('candidate', 'work_head'):
                current_history[key] = original_history[key]
            self.assertEqual(current_history, original_history)
        self.assertEqual(retained, expected)
        self.assertEqual(self.counter.read_text().splitlines(), ['check'])

    def test_new_commit_handoff_precedes_next_worker(self):
        saved = self.publish_new_commit()
        evidence = self.publication_evidence(saved)
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', ['next requirement needs work'])},
        ])
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(NEXT)}], self.root)
        report = self.supervisor(worker, reviewer, resume=True, max_rounds=2).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual([call['read_only'] for call in reviewer.calls], [True])
        publication = saved['rounds'][1]['git_checkpoint']['business_commit']
        self.assertIn('Accepted deliveries already committed', reviewer.calls[0]['prompt'])
        self.assertIn(publication['commit'], reviewer.calls[0]['prompt'])
        state = self.state()
        self.assertEqual(state['requirements_review']['status'], 'ready')
        self.assertEqual(state['requirements_review']['refined_requirement'], NEXT['refined_requirement'])
        self.assertEqual(len(state['requirement_cycles']), 1)
        handoff = state['requirement_cycles'][0]['handoff']
        receipt = saved['rounds'][0]['watcher_result']['candidate_checks']
        self.assertEqual((handoff['cycle'], handoff['round']), (1, 1))
        self.assertEqual(handoff['tree'], receipt['context']['tree'])
        self.assertEqual(handoff['checks_digest'], receipt['digest'])
        self.assertEqual(handoff['commit'], publication['commit'])
        self.assertEqual(handoff['ref'], publication['ref'])
        self.assertEqual([item['id'] for item in handoff['modules']], ['A'])
        self.assert_publication_preserved(saved, evidence, next_worker=True)

    def test_module_publication_resumes_original_requirement(self):
        saved = self.publish_new_commit(scope='module')
        evidence = self.publication_evidence(saved)
        remaining = _verdict('continue', ['original requirement still needs work'])
        remaining.update(modules=[], module_ids=[])
        worker = ScriptedProvider('worker', [{'verdict': remaining}])
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True, max_rounds=2).run()
        self.assertEqual(report.status, 'max_rounds', report.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertIn(FOLLOW, worker.calls[0]['prompt'])
        self.assertEqual(reviewer.calls, [])
        state = self.state()
        self.assertEqual(state['requirement_cycles'], [])
        self.assertEqual(state['requirements_review'], saved['requirements_review'])
        module, = state['modules']
        for key in ('id', 'status', 'round', 'tree', 'evidence'):
            self.assertEqual(module[key], saved['modules'][0][key])
        self.assert_publication_preserved(saved, evidence, next_worker=True)

    def test_published_ordinary_requirement_finishes_without_follow_up(self):
        requirement = '实现当前需求并退出'
        saved = self.publish_new_commit(requirement=requirement)
        evidence = self.publication_evidence(saved)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True, requirement=requirement).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((worker.calls, reviewer.calls), ([], []))
        self.assertEqual(self.state()['requirement_cycles'], [])
        self.assert_publication_preserved(saved, evidence)

    def assert_handoff_refused(self, saved, expected_message):
        evidence = self.publication_evidence(saved)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'recovery_failed', report.summary)
        self.assertIn(expected_message, report.summary)
        self.assertEqual((worker.calls, reviewer.calls), ([], []))
        self.assertEqual(self.state()['requirement_cycles'], [])
        self.assert_publication_preserved(saved, evidence)

    def test_changed_workspace_cannot_authorize_handoff(self):
        saved = self.publish_new_commit()
        (self.root / 'app.py').write_text('change after publication\n')
        self.assert_handoff_refused(saved, 'saved candidate, approval or checks')
        self.assertEqual((self.root / 'app.py').read_text(), 'change after publication\n')

    def test_mismatched_publication_record_cannot_authorize_handoff(self):
        saved = self.publish_new_commit()
        publication = saved['rounds'][1]['git_checkpoint']['business_commit']
        wrong_tree = _git(self.root, 'rev-parse', 'HEAD~1^{tree}')
        self.assertNotEqual(publication['candidate_tree'], wrong_tree)
        publication['candidate_tree'] = wrong_tree
        write_json(self.run_dir / 'state.json', saved, durable=True)
        self.assert_handoff_refused(saved, 'saved Git checkpoints')

    def test_mismatched_independent_approval_cannot_authorize_handoff(self):
        saved = self.publish_new_commit()
        approval = saved['rounds'][1]['watcher_result']['candidate_approval']
        approval['review']['summary'] += ' (modified after publication)'
        write_json(self.run_dir / 'state.json', saved, durable=True)
        self.assert_handoff_refused(saved, 'immutable review record')

    def test_mismatched_check_log_cannot_authorize_handoff(self):
        saved = self.publish_new_commit()
        receipt = saved['rounds'][0]['watcher_result']['candidate_checks']
        log, = receipt['logs']
        with (self.run_dir / log).open('ab') as stream:
            stream.write(b'changed after publication\n')
        self.assert_handoff_refused(saved, 'saved candidate, approval or checks')

    def test_mismatched_check_receipt_cannot_authorize_handoff(self):
        saved = self.publish_new_commit()
        receipt = saved['rounds'][0]['watcher_result']['candidate_checks']
        receipt['digest'] = '0' * 64
        write_json(self.run_dir / 'state.json', saved, durable=True)
        self.assert_handoff_refused(saved, 'saved candidate, approval or checks')

    def test_stop_during_recovery_preserves_publication(self):
        saved = self.publish_new_commit()
        evidence = self.publication_evidence(saved)
        write_json(self.run_dir / 'control.json', {'action': 'stop'}, durable=True)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertIn('stop command', report.summary)
        self.assertEqual((worker.calls, reviewer.calls), ([], []))
        self.assertEqual(len(self.state()['requirement_cycles']), 1)
        self.assert_publication_preserved(saved, evidence)

    def test_pause_during_recovery_waits_before_model_dispatch(self):
        saved = self.publish_new_commit()
        evidence = self.publication_evidence(saved)
        control = self.run_dir / 'control.json'
        write_json(control, {'action': 'pause'}, durable=True)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        resumed = self.supervisor(worker, reviewer, resume=True)
        paused = []
        beat = resumed._beat

        def stop_after_observing_pause(note):
            beat(note)
            if note == 'paused at a turn boundary':
                paused.append((len(worker.calls), len(reviewer.calls)))
                write_json(control, {'action': 'stop'}, durable=True)

        with patch.object(resumed, '_beat', side_effect=stop_after_observing_pause):
            report = resumed.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(paused, [(0, 0)])
        self.assertEqual((worker.calls, reviewer.calls), ([], []))
        self.assertEqual(len(self.state()['requirement_cycles']), 1)
        self.assert_publication_preserved(saved, evidence)

    def test_exhausted_call_budget_survives_publication_recovery(self):
        saved = self.publish_new_commit(max_agent_calls=3)
        evidence = self.publication_evidence(saved)
        self.assertEqual(saved['usage']['agent_calls'], 3)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        report = self.supervisor(worker, reviewer, resume=True).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertIn('Agent call budget exhausted', report.summary)
        self.assertEqual((worker.calls, reviewer.calls), ([], []))
        usage = self.state()['usage']
        self.assertEqual((usage['agent_calls'], usage['max_agent_calls']), (3, 3))
        self.assertEqual(len(self.state()['requirement_cycles']), 1)
        self.assert_publication_preserved(saved, evidence)

    def test_cycle_limit_blocks_follow_up_after_recovering_last_handoff(self):
        saved = self.publish_new_commit()
        evidence = self.publication_evidence(saved)
        worker = ScriptedProvider('worker', [])
        reviewer = CitingReviewer([], self.root)
        self.assertEqual(requirements_review.MAX_CYCLES, 32)
        # Reach the same boundary with one real publication instead of 32.
        with patch.object(requirements_review, 'MAX_CYCLES', 1):
            report = self.supervisor(worker, reviewer, resume=True).run()
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('Requirement cycle limit reached', report.summary)
            cycles = self.state()['requirement_cycles']
            self.assertEqual(len(cycles), 1)
            report = self.supervisor(worker, reviewer, resume=True).run()
            self.assertEqual(report.status, 'action_required', report.summary)
            self.assertIn('Requirement cycle limit reached', report.summary)
            self.assertEqual(self.state()['requirement_cycles'], cycles)
        self.assertEqual((worker.calls, reviewer.calls), ([], []))
        self.assert_publication_preserved(saved, evidence)
