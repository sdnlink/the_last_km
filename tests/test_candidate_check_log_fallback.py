"""Rebuild acceptance evidence when a historical log disappears during reuse."""

from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_repair_convergence as convergence
from tests.test_supervisor import ScriptedProvider


class CandidateCheckLogFallbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = convergence.RepairConvergenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_log_removed_after_reuse_approval_runs_fresh_final_checks(self):
        fixture = self.fixture
        worker = ScriptedProvider('worker', [
            {**fixture.done(scope='module'), 'touch':'app.py'}, fixture.done()])
        reviewer = fixture.reviewer(fixture.done(scope='module'), fixture.done())
        monitor = fixture.monitor(worker, reviewer)
        monitor.config.checks.append('true')
        source = fixture.run/'001-check-02.log'
        prepare = monitor._prepare_candidate_checks
        read_bytes = Path.read_bytes
        source_reads = 0

        def read_with_removal(path):
            nonlocal source_reads
            if path == source:
                source_reads += 1
                # Receipt and reviewer approval both verified the log.
                if source_reads == 3:
                    source.unlink()
                    self.assertFalse((fixture.run/'002-check-01.log').exists())
            return read_bytes(path)

        def prepare_with_removal(record):
            if record.index != 2:
                return prepare(record)
            with patch.object(Path, 'read_bytes', read_with_removal):
                return prepare(record)

        with patch.object(monitor, '_prepare_candidate_checks', side_effect=prepare_with_removal):
            report = monitor.run()

        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(source_reads, 3)
        self.assertEqual(fixture.counter.read_text(), '2')
        self.assertEqual(len(reviewer.calls), 3)
        final = next(r for r in report.records if r.index == 2 and r.role == 'worker')
        approval = next(r for r in report.records if r.index == 2 and r.role == 'reviewer')
        receipt = final.watcher_result['candidate_checks']
        self.assertNotIn('reused_from', receipt)
        self.assertEqual(receipt['context']['round'], 2)
        self.assertEqual(receipt['context']['tree'], final.tree)
        self.assertEqual(receipt['context']['commands'], [fixture.check, 'true'])
        self.assertEqual(receipt['passed_commands'],
                         {fixture.check: '002-check-01.log', 'true': '002-check-02.log'})
        self.assertEqual(approval.watcher_result['candidate_approval']['checks_digest'], receipt['digest'])
        self.assertEqual(approval.delivery['status'], 'accepted')
