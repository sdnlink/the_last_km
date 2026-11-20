"""Business commit messages come from the candidate that Watcher publishes."""
import json
import shutil
import unittest
from unittest.mock import patch

from auto_moniter import candidate_checks, publication
from auto_moniter.approved_history import ApprovedHistory
from auto_moniter.supervisor import Supervisor
from auto_moniter.workspace import workspace_fingerprint
from tests import test_requirement_follow_up as follow_up
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class BusinessCommitMessageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = follow_up.RequirementFollowUpTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def test_published_message_describes_behavior_and_final_approval(self):
        title = 'Write the completion marker to app.py'
        change = 'Replace the initial state with the changed marker.'
        finding = 'app.py still needs an explicit completion marker test.'
        conclusion = 'Verified the exact completion marker and approved the candidate.'
        resolution = 'The exact-line match verifies the marker.'
        commands = ['test -f app.py', 'grep -qx changed app.py']
        submitted = {**_verdict('done', evidence=['app.py']), 'summary': title + '\nWorker submission',
                     'done': [change]}
        approved = {**_verdict('done', closed=['AUD-1']), 'summary': conclusion}
        approved['issue_resolutions'][0]['summary'] = resolution
        worker = ScriptedProvider('worker', [
            {'verdict': submitted, 'touch': 'app.py'},
            {'verdict': submitted},
        ])
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': _verdict('continue', [finding]), 'cite': True},
            {'verdict': approved, 'cite': True},
        ], self.root)

        report = self.fixture.loop(worker, reviewer, requirement='Publish the completion marker.',
                                   checks=commands, max_rounds=2)

        self.assertEqual(report.status, 'done', report.summary)
        message = follow_up._git(self.root, 'log', '-1', '--format=%s%n%b')
        subject, body = message.split('\n', 1)
        self.assertEqual(subject, title)
        for expected in [change, finding, 'AUD-1', resolution, conclusion, 'Approved (requirement)',
                         'Executed for round 2', *[command + ' — passed (exit 0)' for command in commands]]:
            with self.subTest(expected=expected):
                self.assertIn(expected, body)
        self.assertLess(body.index(commands[0]), body.index(commands[1]))
        self.assertEqual(follow_up._git(self.root, 'rev-list', '--count', 'HEAD'), '2')
        self.assertTrue(reviewer.calls[-1]['read_only'])

    def test_requirement_commit_omits_historical_audit_reconfirmation(self):
        self.assert_follow_up_message('requirement')

    def test_module_commit_omits_historical_audit_reconfirmation(self):
        self.assert_follow_up_message('module')

    def assert_follow_up_message(self, scope):
        old_finding = 'main/Initial marker setup needs independent verification.'
        new_finding = 'main/Follow-up marker needs an exact-line check.'
        problem = 'The follow-up marker is missing from next.py.'
        change = 'Write the follow-up marker to next.py.'
        old_resolution = 'Initial marker reconfirmed at the new tree.'
        new_resolution = 'Follow-up marker independently verified.'
        old_approval = _verdict('done', closed=['AUD-1'])
        new_approval = _verdict('done', scope=scope, closed=['AUD-1', 'AUD-2'])
        new_approval.update(summary='Approved the follow-up marker.', modules=[])
        for item, summary in zip(new_approval['issue_resolutions'], [old_resolution, new_resolution]):
            item['summary'] = summary
        initial_worker = _verdict('done', evidence=['app.py'])
        follow_up_worker = _verdict('done', scope=scope, evidence=['next.py'])
        follow_up_worker.update(summary='Implement reviewed task round 53', done=[change], modules=[])
        worker = ScriptedProvider('worker', [
            {'verdict': initial_worker, 'touch': 'app.py'},
            {'verdict': initial_worker},
            {'verdict': follow_up_worker, 'touch': 'next.py'},
            {'verdict': follow_up_worker},
        ])
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': _verdict('continue', [old_finding]), 'cite': True},
            {'verdict': old_approval, 'cite': True},
            {'verdict': {}, 'text': json.dumps({**BRIEF, 'refined_requirement': problem})},
            {'verdict': _verdict('continue', [new_finding]), 'cite': True},
            {'verdict': new_approval, 'cite': True},
            *([{'verdict': {}, 'text': json.dumps(BRIEF)}] if scope == 'requirement' else []),
        ], self.root)

        report = self.fixture.loop(worker, reviewer, checks=['test -f app.py'], max_rounds=4)

        published = report.git_history['last_business_commit']
        self.assertEqual(published['round'], 4, report.summary)
        self.assertTrue(published['created'])
        subject, body = follow_up._git(self.root, 'log', '-1', '--format=%s%n%b').split('\n', 1)
        with self.subTest(field='subject'):
            self.assertEqual(subject, change)
        for expected in [problem, change, f'Approved ({scope})', 'Approved the follow-up marker.',
                         'Modules: main', 'AUD-2', new_finding, new_resolution]:
            self.assertIn(expected, body)
        for excluded in ['AUD-1', old_finding, old_resolution, 'Implement reviewed task round 53']:
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, body)
        state = json.loads((self.root / 'run' / 'state.json').read_text())
        issues = {item['id']: item for item in state['audit_issues']['items']}
        self.assertEqual(issues['AUD-1']['resolved_round'], 4)
        self.assertEqual(issues['AUD-1']['tree'], issues['AUD-2']['tree'])
        self.assertEqual(issues['AUD-1']['module_ids'], issues['AUD-2']['module_ids'])

    def test_reused_checks_are_attributed_and_same_tree_preserves_git_message(self):
        commands = ['printf "ran\\n" >> run/check-count', 'grep -qx changed app.py']
        module = {**_verdict('done', evidence=['app.py'], scope='module'),
                  'summary': 'Write completion marker'}
        final = {**_verdict('done', evidence=['app.py']), 'summary': 'Verify the complete delivery'}
        worker = ScriptedProvider('worker', [{'verdict': module, 'touch': 'app.py'}, {'verdict': final}])
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': {**module, 'summary': 'Approved the marker module.'}, 'cite': True},
            {'verdict': {**final, 'summary': 'Approved the complete delivery.'}, 'cite': True},
        ], self.root)
        prior = {}
        command = reviewer._command

        def observe(session, prompt, read_only):
            if prompt.name == '002-reviewer-prompt.md':
                prior.update(commit=self.git('rev-parse', 'refs/heads/main'),
                             count=self.git('rev-list', '--count', 'refs/heads/main'),
                             message=self.git('log', '-1', '--format=%s%n%b', 'refs/heads/main'))
            return command(session, prompt, read_only)

        with (patch.object(reviewer, '_command', side_effect=observe),
              patch.object(publication, '_business_commit_message',
                           wraps=publication._business_commit_message) as render):
            report = self.fixture.loop(worker, reviewer, requirement='Write and verify the marker.',
                                       checks=commands, max_rounds=2)

        self.assertEqual(report.status, 'done', report.summary)
        original, reused = [row.watcher_result['candidate_checks'] for row in report.records if row.role == 'worker']
        first, final_review = [row for row in report.records if row.role == 'reviewer']
        self.assertEqual(reused['reused_from'], {'round': 1, 'checks_digest': original['digest']})
        self.assertTrue(candidate_checks.valid(reused, self.root / 'run', reused['context']))
        self.assertEqual(final_review.watcher_result['candidate_approval']['checks_digest'], reused['digest'])
        self.assertEqual((self.root / 'run' / 'check-count').read_text(), 'ran\n')
        self.assertTrue(first.git_checkpoint['business_commit']['created'])
        same_tree = final_review.git_checkpoint['business_commit']
        self.assertEqual((same_tree['round'], same_tree['created'], same_tree['commit']), (2, False, prior['commit']))
        self.assertEqual(self.git('rev-parse', 'refs/heads/main'), prior['commit'])
        self.assertEqual(self.git('rev-list', '--count', 'refs/heads/main'), prior['count'])
        self.assertEqual(prior['count'], '2')
        self.assertEqual(self.git('log', '-1', '--format=%s%n%b', 'refs/heads/main'), prior['message'])
        self.assertTrue(prior['message'].startswith('Write completion marker\n'))
        self.assertIn('Approved (module) in round 1.\nModules: main\nApproved the marker module.', prior['message'])
        self.assertIn('Executed for round 1.', prior['message'])
        render.assert_called_once()

        outcome = next(row for row in report.to_dict()['records'] if row['role'] == 'worker' and row['index'] == 2)
        approval = next(row for row in report.to_dict()['records'] if row['role'] == 'reviewer' and row['index'] == 2)
        self.assert_reused_receipt_in_new_git_message(outcome, approval, reused, commands)

    def assert_reused_receipt_in_new_git_message(self, worker, reviewer, receipt, commands):
        # The normal run above reaches the no-op branch on reuse. Exercise the
        # writer on a separate fixture still at the baseline, with the same
        # candidate bytes and the real receipts and approval from that run.
        fixture = follow_up.RequirementFollowUpTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture.root
        run = root / 'run'
        run.mkdir()
        for log in receipt['logs']:
            shutil.copyfile(self.root / 'run' / log, run / log)
        history = ApprovedHistory(root, run)
        before = history.begin_round(2)
        shutil.copyfile(self.root / 'app.py', root / 'app.py')
        history.prepare_review(2, before)
        self.assertEqual(workspace_fingerprint(root, ignore_dir=run), receipt['context']['tree'])
        self.assertTrue(candidate_checks.valid(receipt, run, receipt['context']))
        self.assertEqual(history.candidate['commit'], self.git('rev-parse', 'HEAD^{tree}'))
        _, published_review = publication.publish(history, 2, worker, reviewer, '', '', receipt=receipt)
        self.assertTrue(published_review['business_commit']['created'])
        subject, body = follow_up._git(root, 'log', '-1', '--format=%s%n%b').split('\n', 1)
        self.assertEqual(subject, 'Verify the complete delivery')
        self.assertIn('Approved the complete delivery.', body)
        self.assertEqual(body.split('Pre-review checks\n', 1)[1].splitlines(),
                         ['Reused from round 1.', *[f'- {check} — passed (exit 0)' for check in commands]])
        without_source = {**receipt, 'reused_from': {'checks_digest': receipt['reused_from']['checks_digest']}}
        self.assertEqual(publication._reviewed_checks(without_source, 2)[0],
                         'Reused checks. Source round unavailable.')

    def test_published_message_handles_absent_optional_check_detail_and_audits(self):
        record_result = Supervisor._record_check_result

        def omit_exit_detail(monitor, index, number, command, result, before, final, checks, failures, verdict):
            path = record_result(monitor, index, number, command, result, before, final, checks, failures, verdict)
            checks[-1].pop('exit_code')
            return path

        with patch.object(Supervisor, '_record_check_result', omit_exit_detail):
            report = self.run_single_publication('test -f app.py')

        self.assertEqual(report.status, 'done', report.summary)
        subject, body = self.git('log', '-1', '--format=%s%n%b').split('\n', 1)
        self.assertEqual(subject, 'Update app.py')
        self.assertIn('Independently verified the marker.', body)
        self.assertIn('test -f app.py — passed', body)
        self.assertNotIn('(exit ', body)
        self.assertNotIn('AUD-', body)

    def test_failed_check_leaves_business_ref_unchanged(self):
        report = self.run_single_publication('exit 7')
        receipt = self.assert_unpublished(report, 'Acceptance check failed')
        self.assertFalse(receipt['passed'])
        self.assertEqual(receipt['checks'][0]['exit_code'], 7)

    def test_mismatched_approval_leaves_business_ref_unchanged(self):
        save_approval = Supervisor._save_candidate_approval

        def mismatch(monitor, worker, reviewer):
            save_approval(monitor, worker, reviewer)
            reviewer.watcher_result['candidate_approval'] = {
                **reviewer.watcher_result['candidate_approval'], 'checks_digest': '0' * 64,
            }

        with patch.object(Supervisor, '_save_candidate_approval', mismatch):
            report = self.run_single_publication('test -f app.py')
        receipt = self.assert_unpublished(report, 'Reviewer approval does not match')
        self.assertTrue(receipt['passed'])

    def test_check_log_changed_after_approval_leaves_business_ref_unchanged(self):
        save_approval = Supervisor._save_candidate_approval

        def corrupt_log(monitor, worker, reviewer):
            save_approval(monitor, worker, reviewer)
            (monitor.run_dir / '001-check-01.log').write_text('changed check evidence\n')

        with patch.object(Supervisor, '_save_candidate_approval', corrupt_log):
            report = self.run_single_publication('test -f app.py')
        receipt = self.assert_unpublished(report, 'approval does not match the saved candidate and check receipt', valid=False)
        self.assertTrue(receipt['passed'])

    def run_single_publication(self, check):
        worker = ScriptedProvider('worker', [self.fixture.done(touch='app.py')])
        approved = {**_verdict('done', evidence=['app.py']), 'summary': 'Independently verified the marker.'}
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)}, {'verdict': approved, 'cite': True},
        ], self.root)
        return self.fixture.loop(worker, reviewer, requirement='Write the marker.', checks=[check], max_rounds=1)

    def assert_unpublished(self, report, reason, *, valid=True):
        self.assertNotEqual(report.status, 'done')
        self.assertIn(reason, report.summary + ' ' + ' '.join(report.records[-1].verdict.gaps))
        self.assertEqual(self.git('rev-parse', 'refs/heads/main'), self.fixture.base)
        self.assertEqual(self.git('rev-list', '--count', 'refs/heads/main'), '1')
        self.assertEqual(self.git('log', '-1', '--format=%s%n%b'), 'base')
        self.assertFalse(report.git_history['last_business_commit'])
        self.assertEqual((self.root / 'app.py').read_text(), 'changed\n')
        worker = next(row for row in report.records if row.role == 'worker')
        receipt = worker.watcher_result['candidate_checks']
        self.assertEqual(candidate_checks.valid(receipt, self.root / 'run', receipt['context']), valid)
        return receipt

    def git(self, *args):
        return follow_up._git(self.root, *args)
