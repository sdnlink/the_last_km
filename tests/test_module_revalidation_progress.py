"""A verified module transition advances scheduling, even after invalidation."""

import json
import shlex
import sys
import unittest
from unittest.mock import patch

from auto_moniter import candidate_checks, publication
from auto_moniter.supervisor import RunConfig, Supervisor
from tests import test_requirement_follow_up as follow_up
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


MODULE_IDS = ('seed', 'main', 'batch-a', 'batch-b')
MODULES = [
    {'id': key, 'title': key, 'acceptance': [f'Verify {key}']}
    for key in MODULE_IDS
]


def module_report(*ids, scope='module', status='done', gap=None, **script):
    verdict = _verdict(status, scope=scope, evidence=['app.py'], gaps=[gap] if gap else [])
    verdict.update(modules=[], module_ids=list(ids))
    return {'verdict': verdict, **script}


class ModuleRevalidationProgressTests(unittest.TestCase):
    def setUp(self):
        fixture = follow_up.RequirementFollowUpTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.root = fixture.root
        self.base = fixture.base
        self.run_dir = self.root / 'run'
        self.counter = self.run_dir / 'check-count'
        self.command = shlex.join([sys.executable, '-c', (
            'from pathlib import Path\n'
            'counter = Path("run/check-count")\n'
            'count = int(counter.read_text()) if counter.exists() else 0\n'
            'counter.write_text(str(count + 1))\n'
            'print(f"check execution {count + 1}")\n'
        )])
        self.pauses = {}
        self.retry_states = {}
        self.dispatch = {}

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def events(self, kind):
        events = (self.run_dir / 'events.jsonl').read_text().splitlines()
        return [event for event in map(json.loads, events) if event['event'] == kind]

    def run_loop(self, worker_scripts, reviews, *, no_progress_limit, restart_after_stall=3,
                 max_agent_calls=0, damage=None):
        self.worker = ScriptedProvider('worker', worker_scripts)
        self.reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)}, *reviews,
        ], self.root)

        def log(message):
            if message.startswith('round ') and message.endswith(': worker running'):
                index = int(message.split()[1].rstrip(':'))
                self.dispatch[index] = self.state()

        def retry(count, reason):
            index = monitor._records[-1].index
            self.pauses[index] = (count, reason)
            self.retry_states[index] = self.state()

        monitor = Supervisor(RunConfig(
            'Implement and verify the modules.', self.root, self.worker, reviewer=self.reviewer,
            modules=MODULES, workflow='review-first', git_history='required', run_dir=self.run_dir,
            review_every=0, retry_delay_s=0, checks=[self.command], max_agent_calls=max_agent_calls,
            quality_policy={'no_progress_limit': no_progress_limit, 'review_interval_s': 0},
            restart_after_stall=restart_after_stall,
        ), log=log)
        save_approval = monitor._save_candidate_approval

        def damage_after_approval(worker, reviewer):
            save_approval(worker, reviewer)
            if worker.index == 2:
                if damage == 'receipt':
                    (self.run_dir / '002-check-01.log').write_text('changed check bytes\n')
                elif damage == 'approval':
                    reviewer.watcher_result['candidate_approval']['checks_digest'] = 'invalid'

        with (
            patch.object(monitor, '_retry_pause', side_effect=retry),
            patch.object(monitor, '_save_candidate_approval', side_effect=damage_after_approval),
            patch.object(publication, 'publish', wraps=publication.publish) as publish,
        ):
            report = monitor.run()
        self.published_rounds = [call.args[1] for call in publish.call_args_list]
        return report

    def assert_review_receipt(self, report, index, ids, reused_from=None):
        worker = next(row for row in report.records if row.role == 'worker' and row.index == index)
        review = next(row for row in report.records if row.role == 'reviewer' and row.index == index)
        self.assertEqual(review.delivery['status'], 'accepted')
        receipt = worker.watcher_result['candidate_checks']
        approval = review.watcher_result['candidate_approval']
        context = receipt['context']
        self.assertEqual((context['round'], context['tree'], context['commands']),
                         (index, worker.tree, [self.command]))
        self.assertTrue(candidate_checks.valid(receipt, self.run_dir, context))
        self.assertEqual(context['module_ids'], sorted(ids))
        self.assertEqual(context['final'], review.verdict.scope == 'requirement')
        self.assertTrue(receipt['passed'])
        self.assertEqual(approval['checks_digest'], receipt['digest'])
        self.assertEqual(approval['context'], context)
        self.assertEqual(approval['review']['status'], 'done')
        self.assertEqual(approval['review']['module_ids'], list(ids))
        self.assertEqual(review.tree, worker.tree)
        if reused_from is not None:
            previous = next(row for row in report.records
                            if row.role == 'worker' and row.index == reused_from)
            self.assertEqual(receipt['reused_from'], {
                'round': reused_from,
                'checks_digest': previous.watcher_result['candidate_checks']['digest'],
            })
        return worker.tree

    def test_same_candidate_revalidations_advance_without_stall_or_backoff(self):
        unfinished = module_report('main', status='continue', gap='Still verifying the new input')
        report = self.run_loop([
            module_report(*MODULE_IDS, touch='app.py'),
            {**unfinished, 'touch': 'input.py'},
            module_report('seed'),
            unfinished, unfinished,
            module_report('main'),
            unfinished, unfinished,
            module_report(*MODULE_IDS),
            module_report(*MODULE_IDS, scope='requirement'),
        ], [
            module_report(*MODULE_IDS, cite=True),
            module_report('seed', cite=True),
            module_report('main', cite=True),
            module_report(*MODULE_IDS, cite=True),
            module_report(*MODULE_IDS, scope='requirement', cite=True),
        ], no_progress_limit=3)

        self.assertNotIn(3, self.pauses, self.pauses)
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(self.published_rounds, [1, 3, 6, 9, 10])
        self.assertEqual(self.counter.read_text(), '2')
        self.assertEqual((len(self.worker.calls), len(self.reviewer.calls)), (10, 6))
        self.assertTrue(all(call['read_only'] for call in self.reviewer.calls))
        self.assertEqual({index: reason for index, (_, reason) in self.pauses.items()},
                         {5: 'unchanged worker gaps', 8: 'unchanged worker gaps'})
        self.assertEqual(self.events('stagnation_recovery'), [])

        old_tree = self.assert_review_receipt(report, 1, MODULE_IDS)
        tree = self.assert_review_receipt(report, 3, ('seed',))
        self.assertNotEqual(tree, old_tree)
        invalidation, = self.events('modules_invalidated')
        self.assertEqual(invalidation['tree'], tree)
        self.assertEqual(invalidation['modules'], str(list(MODULE_IDS)))
        for index, ids, source in [(6, ('main',), 3), (9, MODULE_IDS, 6), (10, MODULE_IDS, 9)]:
            self.assertEqual(self.assert_review_receipt(report, index, ids, source), tree)

        transitions = self.events('module_accepted')
        self.assertEqual([(int(event['round']), event['modules'], event['tree']) for event in transitions], [
            (1, str(sorted(MODULE_IDS)), old_tree), (3, "['seed']", tree),
            (6, "['main']", tree), (9, "['batch-a', 'batch-b']", tree),
        ])
        for round_index, newly_accepted in [(6, ('main',)), (9, ('batch-a', 'batch-b'))]:
            before, after = self.dispatch[round_index], self.dispatch[round_index + 1]
            self.assertEqual((before['stall'], before['no_progress_turns']), (1, 2))
            self.assertEqual((after['stall'], after['no_progress_turns'], after['gap_key']), (0, 0, ''))
            self.assertFalse(after['progress_pending'])
            self.assertFalse(after['session_id'] or after['reviewer_session_id'])
            previous = {row['id']: row for row in before['modules']}
            accepted = {row['id']: row for row in after['modules']}
            for key in newly_accepted:
                self.assertEqual(previous[key]['status'], 'pending')
                self.assertEqual((accepted[key]['status'], accepted[key]['round'], accepted[key]['tree']),
                                 ('accepted', round_index, tree))
            for key in accepted.keys() - set(newly_accepted):
                self.assertEqual(accepted[key], previous[key])
        milestones = self.state()['quality_state']['milestones']
        self.assertEqual([m for m in milestones if m.startswith('module:')],
                         [f'module:{key}' for key in sorted(MODULE_IDS)])
        self.assertEqual(follow_up._git(self.root, 'rev-list', '--count', f'{self.base}..HEAD'), '2')

    def test_only_existing_module_and_repeated_rejection_cannot_reset_progress_or_extend_budget(self):
        self.assert_repeated_rejection(no_progress_limit=12, max_agent_calls=9, outcome='budget_exceeded')

    def test_repeated_rejection_reaches_the_verified_progress_limit(self):
        self.assert_repeated_rejection(no_progress_limit=3, max_agent_calls=10, outcome='action_required')

    def assert_repeated_rejection(self, *, no_progress_limit, max_agent_calls, outcome):
        rejected = module_report('main', status='continue', gap='app.py: missing failure handling', cite=True)
        report = self.run_loop([
            module_report('seed', touch='app.py'),
            module_report('seed', 'main'),
            module_report('seed', 'main'),
            module_report('seed', 'main'),
        ], [
            module_report('seed', cite=True),
            module_report('seed', cite=True),
            rejected, rejected,
        ], no_progress_limit=no_progress_limit, restart_after_stall=2, max_agent_calls=max_agent_calls)

        self.assertEqual(report.status, outcome, report.summary)
        if outcome == 'action_required':
            self.assertIn('Verified progress limit reached', report.summary)
        self.assertEqual((len(self.worker.calls), len(self.reviewer.calls)), (4, 5))
        state = self.state()
        self.assertEqual((state['usage']['agent_calls'], state['no_progress_turns']), (9, 3))
        self.assertEqual([self.retry_states[i]['no_progress_turns'] for i in (2, 3, 4)], [1, 2, 3])
        self.assertEqual(self.retry_states[4]['stall'], 1)
        self.assertEqual({index: reason for index, (_, reason) in self.pauses.items()},
                         {2: 'unfinished review', 3: 'unfinished review', 4: 'unfinished review'})
        self.assertEqual([event['modules'] for event in self.events('module_accepted')], ["['seed']"])
        self.assertEqual(len(self.events('stagnation_recovery')), 1)
        self.assertEqual(self.published_rounds, [1])
        self.assertEqual(follow_up._git(self.root, 'rev-list', '--count', f'{self.base}..HEAD'), '1')
        rows = {row['id']: row for row in state['modules']}
        self.assertEqual((rows['seed']['status'], rows['seed']['round']), ('accepted', 1))
        self.assertEqual(rows['main']['status'], 'pending')
        worker1 = next(row for row in report.records if row.role == 'worker' and row.index == 1)
        self.assertEqual({row.tree for row in report.records}, {worker1.tree})
        self.assert_review_receipt(report, 1, ('seed',))
        reviews = {row.index: row for row in report.records if row.role == 'reviewer'}
        self.assertIn('The named modules were already delivered', reviews[2].verdict.gaps[0])
        self.assertEqual(reviews[3].verdict.gaps, reviews[4].verdict.gaps)
        self.assertTrue(all(row.delivery.get('status') != 'accepted' for i, row in reviews.items() if i > 1))
        self.assertEqual((self.root / 'app.py').read_text(), 'changed\n')
        self.assertTrue(state['git_history']['enabled'])
        self.assertTrue((self.run_dir / '003-check-01.log').is_file())
        last_worker = next(row for row in report.records if row.role == 'worker' and row.index == 4)
        self.assertEqual(last_worker.watcher_result['repair_preflight']['status'], 'changes_required')
        self.assertTrue((self.run_dir / '004-reviewer-preflight.log').is_file())
        self.assertTrue(json.loads((self.run_dir / 'handoff.json').read_text())['candidate'])

    def test_changed_receipt_blocks_module_transition(self):
        self.assert_invalid_candidate_remains_pending('receipt', 'saved acceptance is stale')

    def test_mismatched_approval_blocks_module_transition(self):
        self.assert_invalid_candidate_remains_pending('approval', 'approval does not match')

    def assert_invalid_candidate_remains_pending(self, damage, reason):
        report = self.run_loop([
            module_report('seed', touch='app.py'),
            module_report('main'),
        ], [
            module_report('seed', cite=True),
            module_report('main', cite=True),
        ], no_progress_limit=12, damage=damage)

        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual((len(self.worker.calls), len(self.reviewer.calls)), (2, 3))
        self.assertEqual(self.published_rounds, [1])
        self.assertEqual([event['modules'] for event in self.events('module_accepted')], ["['seed']"])
        review = report.records[-1]
        self.assertEqual(review.delivery['status'], 'rejected')
        self.assertTrue(any(reason in gap for gap in review.verdict.gaps), review.verdict.gaps)
        state = self.state()
        self.assertEqual(next(row for row in state['modules'] if row['id'] == 'main')['status'], 'pending')
        self.assertNotIn('module:main', state['quality_state']['milestones'])
        self.assertEqual(follow_up._git(self.root, 'rev-list', '--count', f'{self.base}..HEAD'), '1')
