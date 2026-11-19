"""Fresh checks after a reuse failure still gate business publication."""

import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from auto_moniter import candidate_checks, publication
from auto_moniter.supervisor import RunConfig, Supervisor
from tests import test_requirement_follow_up as follow_up
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class CandidateCheckLogFallbackDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = follow_up.RequirementFollowUpTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.root = fixture.root
        self.base = fixture.base
        self.run_dir = self.root / 'run'

    def run_handoff(self, *, stop):
        def done(*module_ids, **script):
            verdict = _verdict('done', scope='module', evidence=['app.py'])
            verdict['module_ids'] = list(module_ids)
            return {'verdict': verdict, **script}

        modules = [
            done('main')['verdict']['modules'][0],
            {'id': 'integration', 'title': 'Integration',
             'acceptance': ['Verify integration'], 'depends_on': ['main']},
        ]
        worker = ScriptedProvider('worker', [
            done('main', touch='app.py'), done('main', 'integration'),
        ])
        reviewer = follow_up.CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            done('main', cite=True), done('main', 'integration', cite=True),
        ], self.root)

        counter = self.run_dir / 'check-count'
        stop_marker = self.run_dir / 'check-running'
        count_command = shlex.join([sys.executable, '-c', (
            'from pathlib import Path\n'
            'counter = Path("run/check-count")\n'
            'count = int(counter.read_text()) if counter.exists() else 0\n'
            'counter.write_text(str(count + 1))\n'
            'print(f"check execution {count + 1}")\n'
        )])
        second_action = (
            '    Path("run/check-running").touch()\n'
            '    time.sleep(60)\n'
            if stop else
            '    raise SystemExit(7)\n'
        )
        outcome_command = shlex.join([sys.executable, '-u', '-c', (
            'from pathlib import Path\n'
            'import time\n'
            'if Path("run/check-count").read_text() == "2":\n'
            '    print("fresh second check")\n'
        ) + second_action])
        commands = [count_command, outcome_command]
        monitor = Supervisor(RunConfig(
            follow_up.FOLLOW, self.root, worker, reviewer=reviewer, modules=modules,
            workflow='review-first', git_history='required', run_dir=self.run_dir,
            checks=commands, max_rounds=2, retry_delay_s=0, check_timeout_s=10,
        ), log=lambda _: None)

        historical_log = self.run_dir / '001-check-02.log'
        read_bytes = Path.read_bytes
        prepare = monitor._prepare_candidate_checks
        should_stop = monitor._should_stop
        historical_reads = 0

        def read_with_removal(path):
            nonlocal historical_reads
            if path == historical_log:
                historical_reads += 1
                if historical_reads == 3:
                    path.unlink()
                    self.assertFalse((self.run_dir / '002-check-01.log').exists())
            return read_bytes(path)

        def prepare_with_removal(record):
            if record.index != 2:
                return prepare(record)
            with patch.object(Path, 'read_bytes', read_with_removal):
                return prepare(record)

        def stop_running_check():
            if stop_marker.exists() and not monitor._stop_requested:
                monitor.request_stop()
            return should_stop()

        with (
            patch.object(monitor, '_prepare_candidate_checks', side_effect=prepare_with_removal),
            patch.object(monitor, '_should_stop', side_effect=stop_running_check),
            patch.object(publication, 'publish', wraps=publication.publish) as publish,
        ):
            report = monitor.run()

        self.assertEqual(report.status, 'stopped' if stop else 'action_required', report.summary)
        self.assertEqual(historical_reads, 3)
        self.assertEqual(counter.read_text(), '2')
        self.assertEqual([call.args[1] for call in publish.call_args_list], [1])
        self.assertEqual(follow_up._git(self.root, 'rev-list', '--count', f'{self.base}..HEAD'), '1')
        self.assertEqual(follow_up._git(self.root, 'show', 'HEAD:app.py'), 'changed')
        self.assertEqual((self.root / 'app.py').read_text(), 'changed\n')

        state = json.loads((self.run_dir / 'state.json').read_text())
        candidate = next(r for r in state['rounds'] if r['index'] == 2 and r['role'] == 'worker')
        receipt = candidate['watcher_result']['candidate_checks']
        context = receipt['context']
        self.assertEqual((context['round'], context['tree'], context['commands']),
                         (2, candidate['tree'], commands))
        self.assertEqual(set(context['module_ids']), {'main', 'integration'})
        self.assertFalse(context['final'])
        self.assertFalse(receipt['passed'])
        self.assertFalse(receipt['source_changed'])
        self.assertNotIn('reused_from', receipt)
        self.assertTrue(candidate_checks.valid(receipt, self.run_dir, context))
        first, second = receipt['checks']
        self.assertEqual((first['log'], second['log']), ('002-check-01.log', '002-check-02.log'))
        self.assertEqual(receipt['passed_commands'], {count_command: first['log']})
        self.assertIn('check execution 2', (self.run_dir / first['log']).read_text())
        self.assertIn('fresh second check', (self.run_dir / second['log']).read_text())
        self.assertFalse(second['timed_out'])
        self.assertFalse(state.get('requirement_cycles'))
        return report, reviewer, second

    def test_failed_fallback_retains_evidence_and_blocks_publication(self):
        report, reviewer, check = self.run_handoff(stop=False)
        self.assertEqual((check['status'], check['exit_code']), ('check_failed', 7))
        self.assertEqual(len(reviewer.calls), 3)
        review = next(r for r in report.records if r.index == 2 and r.role == 'reviewer')
        self.assertEqual(review.delivery['status'], 'rejected')
        self.assertTrue(any('002-check-02.log' in reason for reason in review.delivery['reasons']))

    def test_stopped_fallback_retains_evidence_and_blocks_publication(self):
        report, reviewer, check = self.run_handoff(stop=True)
        self.assertEqual(check['status'], 'interrupted')
        self.assertTrue(check['interruption'])
        self.assertEqual(len(reviewer.calls), 2)
        self.assertFalse(any(r.index == 2 and r.role == 'reviewer' for r in report.records))
