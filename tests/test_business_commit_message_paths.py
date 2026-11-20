"""Portable commit display must not alter Watcher's executable evidence (version 6)."""
import copy
import json
from pathlib import Path
import shlex
import shutil
import sys
import unittest

from auto_moniter import candidate_checks, publication
from auto_moniter.approved_history import ApprovedHistory
from tests import test_requirement_follow_up as follow_up
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class BusinessCommitMessagePathTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.new_fixture()
        self.root = self.fixture.root

    def new_fixture(self):
        fixture = follow_up.RequirementFollowUpTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def run_publication(self, *, reuse=False, chinese=False):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the actual publication regression')
        commands = [
            shlex.join([sys.executable, '-c', 'print("python check executed")']),
            shlex.join([node, '-e', 'console.log("node check executed")']),
        ]
        title = f'Fix {self.root}/app.py using /home/local-user/private/tool'
        change = ('Keep docs/guide.md and ./app.py; inspect "/tmp/private directory/detail.txt" '
                  'and trace:/home/local-user/private/report.txt; file:///tmp/private-file/report.txt.')
        problem = f'Update {self.root}/app.py; see https://example.org/docs/reference?q=/tmp/public.'
        finding = f'main/Inspect /home/local-user/private/audit.txt and {self.root}/app.py.'
        resolution = f'Verified {self.root}/app.py using /tmp/private-audit/result.txt.'
        if chinese:
            title = f'修复{self.root}/app.py中的问题 using /home/local-user/private/tool'
            change += f' 修复{self.root}/app.py中的问题，读取/tmp/private/report.txt后重试。'
            problem += f' 读取/tmp/private/problem.txt后修复{self.root}/app.py。'
            finding += f' 检查/tmp/private/finding.txt后审阅{self.root}/app.py。'
            resolution += f' 核验/tmp/private/resolution.txt后批准{self.root}/app.py。'
        submitted = {**_verdict('done', evidence=['app.py']), 'summary': title, 'done': [change]}
        approved = {**_verdict('done', closed=['AUD-1']),
                    'summary': f'Approved {self.root}/app.py; see https://example.org/results.'}
        if chinese:
            approved['summary'] += f' 核验{self.root}/app.py并读取/tmp/private/review.txt后批准。'
        approved['issue_resolutions'][0]['summary'] = resolution
        if reuse:
            submitted['scope'] = 'module'
            approved = {**approved, 'issue_resolutions': []}
        final = {**submitted, 'scope': 'requirement'}
        worker = ScriptedProvider('worker', [
            {'verdict': submitted, 'touch': 'app.py'}, {'verdict': final},
        ])
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps({**BRIEF, 'refined_requirement': problem})},
            {'verdict': {**approved, 'scope': 'module'} if reuse else _verdict('continue', [finding]), 'cite': True},
            {'verdict': approved, 'cite': True},
        ], self.root)
        report = self.fixture.loop(worker, reviewer, requirement=problem, checks=commands, max_rounds=2)
        self.assertEqual(report.status, 'done', report.summary)
        return report, commands, title

    def assert_portable(self, message, commands, *, reused=False):
        for private in [str(self.root), '/home/local-user', '/tmp/private', sys.executable,
                        str(Path(commands[1].split()[0]).parent)]:
            self.assertNotIn(private, message)
        self.assertIn('using <local-path>', message.splitlines()[0])
        self.assertIn('docs/guide.md', message)
        self.assertIn('./app.py', message)
        self.assertIn('https://example.org/docs/reference?q=/tmp/public.', message)
        self.assertIn('https://example.org/results.', message)
        if not reused:
            self.assertIn('AUD-1', message)
        self.assertIn('python -c', message)
        self.assertIn('node -e', message)
        self.assertEqual(message.count('— passed (exit 0)'), 2)

    def test_real_git_message_is_portable_and_raw_evidence_stays_exact(self):
        report, commands, title = self.run_publication()
        self.assert_portable(follow_up._git(self.root, 'log', '-1', '--format=%s%n%b'), commands)
        worker = next(r for r in report.records if r.role == 'worker' and r.index == 2)
        reviewer = next(r for r in report.records if r.role == 'reviewer' and r.index == 2)
        receipt = worker.watcher_result['candidate_checks']
        self.assertEqual(worker.verdict.summary, title)
        self.assertEqual(receipt['context']['commands'], commands)
        self.assertEqual([c['command'] for c in receipt['checks']], commands)
        self.assertTrue(candidate_checks.valid(receipt, self.root/'run', receipt['context']))
        self.assertEqual(reviewer.watcher_result['candidate_approval']['checks_digest'], receipt['digest'])
        for check, marker in zip(receipt['checks'], ['python check executed', 'node check executed']):
            log = (self.root/'run'/check['log']).read_text()
            self.assertIn(check['command'], log)
            self.assertIn(marker, log)
        state = json.loads((self.root/'run'/'state.json').read_text())
        self.assertEqual(state['checks'], commands)
        self.assertEqual(follow_up._git(self.root, 'log', '--format=%s', '--reverse').splitlines()[0], 'base')

    def test_reused_receipt_in_real_publication_keeps_source_and_raw_commands(self):
        report, commands, _ = self.run_publication(reuse=True)
        rows = report.to_dict()['records']
        worker = next(r for r in rows if r['role'] == 'worker' and r['index'] == 2)
        reviewer = next(r for r in rows if r['role'] == 'reviewer' and r['index'] == 2)
        receipt = worker['watcher_result']['candidate_checks']
        self.assertIn('reused_from', receipt)
        before = copy.deepcopy(receipt)
        fixture = self.new_fixture()
        run = fixture.root/'run'
        run.mkdir()
        for log in receipt['logs']:
            shutil.copyfile(self.root/'run'/log, run/log)
        history = ApprovedHistory(fixture.root, run)
        baseline = history.begin_round(2)
        shutil.copyfile(self.root/'app.py', fixture.root/'app.py')
        history.prepare_review(2, baseline)
        _, published = publication.publish(history, 2, worker, reviewer, '', '',
                                           problem=f'Inspect {self.root}/app.py; see https://example.org/docs/reference?q=/tmp/public.',
                                           receipt=receipt)
        self.assertTrue(published['business_commit']['created'])
        message = follow_up._git(fixture.root, 'log', '-1', '--format=%s%n%b')
        self.assert_portable(message, commands, reused=True)
        self.assertIn('Reused from round 1.', message)
        self.assertEqual(receipt, before)
        self.assertTrue(candidate_checks.valid(receipt, run, receipt['context']))

    def test_chinese_adjacent_paths_are_hidden_in_real_publication(self):
        report, commands, title = self.run_publication(chinese=True)
        message = follow_up._git(self.root, 'log', '-1', '--format=%s%n%b')
        self.assert_portable(message, commands)
        self.assertTrue(message.startswith('修复app.py中的问题 using <local-path>'))
        worker = next(r for r in report.records if r.role == 'worker' and r.index == 2)
        reviewer = next(r for r in report.records if r.role == 'reviewer' and r.index == 2)
        receipt = worker.watcher_result['candidate_checks']
        self.assertEqual(worker.verdict.summary, title)
        self.assertEqual(receipt['context']['commands'], commands)
        self.assertEqual([c['command'] for c in receipt['checks']], commands)
        self.assertTrue(candidate_checks.valid(receipt, self.root/'run', receipt['context']))
        self.assertEqual(reviewer.watcher_result['candidate_approval']['checks_digest'], receipt['digest'])
        for check in receipt['checks']:
            self.assertIn(check['command'], (self.root/'run'/check['log']).read_text())
