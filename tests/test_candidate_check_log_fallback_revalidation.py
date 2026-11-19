"""Final review can reuse checks while historical modules await revalidation."""

import json
import shlex
import sys
import unittest
from unittest.mock import patch

from auto_moniter import candidate_checks
from tests import test_requirement_follow_up as follow_up
from tests.test_module_revalidation_progress import module_report
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider


class CandidateCheckRevalidationTests(unittest.TestCase):
    def test_final_review_reuses_checks_and_revalidates_pending_module(self):
        fixture = follow_up.RequirementFollowUpTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = fixture.root
        run = root / 'run'
        counter = run / 'check-count'
        command = shlex.join([sys.executable, '-c', (
            'from pathlib import Path\n'
            'counter = Path("run/check-count")\n'
            'count = int(counter.read_text()) if counter.exists() else 0\n'
            'counter.write_text(str(count + 1))\n'
            'print(f"check execution {count + 1}")\n'
        )])
        ids = ('seed', 'integration')
        modules = [
            {'id': key, 'title': key, 'acceptance': [f'Verify {key}'],
             'files': ['app.py', 'shared.py']}
            for key in ids
        ]
        worker = ScriptedProvider('worker', [
            module_report(*ids, touch='app.py'),
            module_report('seed', touch='shared.py'),
            module_report(*ids, scope='requirement'),
        ])
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            module_report(*ids, cite=True),
            module_report('seed', cite=True),
            module_report(*ids, scope='requirement', cite=True),
        ], root)
        review_inputs = {}
        prepare_review = reviewer._command

        def capture_review(session, prompt_file, read_only):
            if prompt_file.name == '003-reviewer-prompt.md':
                review_inputs['state'] = json.loads((run / 'state.json').read_text())
                review_inputs['head'] = follow_up._git(root, 'rev-parse', 'HEAD')
                review_inputs['count'] = counter.read_text()
            return prepare_review(session, prompt_file, read_only)

        with patch.object(reviewer, '_command', side_effect=capture_review):
            report = fixture.loop(worker, reviewer, requirement='Implement and verify the modules.',
                                  checks=[command], modules=modules, max_rounds=3)

        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (3, 4))
        self.assertTrue(reviewer.calls[3]['read_only'])
        before = {item['id']: item for item in review_inputs['state']['modules']}
        self.assertEqual((before['seed']['status'], before['seed']['round']), ('accepted', 2))
        self.assertEqual((before['integration']['status'], before['integration']['round']), ('pending', 1))
        self.assertEqual(review_inputs['count'], '2')  # One execution on each distinct source tree.
        self.assertEqual(counter.read_text(), '2')

        candidates = {row.index: row for row in report.records if row.role == 'worker'}
        approvals = {row.index: row for row in report.records if row.role == 'reviewer'}
        self.assertNotEqual(candidates[1].tree, candidates[2].tree)
        self.assertEqual(candidates[2].tree, candidates[3].tree)
        prior = candidates[2].watcher_result['candidate_checks']
        final = candidates[3].watcher_result['candidate_checks']
        self.assertEqual(final['reused_from'], {'round': 2, 'checks_digest': prior['digest']})
        self.assertEqual(final['context']['round'], 3)
        self.assertTrue(final['context']['final'])
        self.assertEqual(final['context']['module_ids'], sorted(ids))
        self.assertEqual(final['context']['commands'], [command])
        self.assertTrue(final['passed'])
        self.assertTrue(candidate_checks.valid(final, run, final['context']))
        self.assertEqual((run / '002-check-01.log').read_bytes(), (run / '003-check-01.log').read_bytes())
        self.assertEqual(approvals[3].watcher_result['candidate_approval']['checks_digest'], final['digest'])
        self.assertEqual(approvals[3].delivery['status'], 'accepted')

        state = json.loads((run / 'state.json').read_text())
        after = {item['id']: item for item in state['modules']}
        self.assertEqual((after['integration']['status'], after['integration']['round']), ('accepted', 3))
        module_publication = approvals[2].git_checkpoint['business_commit']
        final_publication = approvals[3].git_checkpoint['business_commit']
        self.assertTrue(module_publication['created'])
        self.assertFalse(final_publication['created'])
        self.assertEqual(final_publication['round'], 3)
        self.assertEqual(final_publication['commit'], module_publication['commit'])
        self.assertEqual(review_inputs['head'], final_publication['commit'])
        self.assertEqual(follow_up._git(root, 'rev-parse', 'HEAD'), final_publication['commit'])
