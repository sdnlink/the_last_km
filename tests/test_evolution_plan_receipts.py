"""Read-only evolution plans validate the selected cycle's stage receipts."""

import json
import unittest
from unittest.mock import patch

from auto_moniter.evolution import git
from auto_moniter.storage import write_json
from tests import test_evolution_phase_execution as phase_fixtures


class EvolutionPlanReceiptTests(unittest.TestCase):
    def test_plan_refuses_dirty_publication_source_and_detached_implementation(self):
        for phase in ('ready', 'activating', 'implementing'):
            with self.subTest(phase=phase):
                phases = phase_fixtures.EvolutionPhaseExecutionTests()
                try:
                    fixture = phases.fixture()
                    path = phases.interrupt(fixture, phase)
                    engine, _ = phases.resumer(fixture)
                    if phase == 'implementing':
                        git(path.parent / 'workspace', 'checkout', '--detach', 'HEAD')
                        reason = 'implementation branch changed'
                    else:
                        tracked = fixture.source / 'auto_moniter' / 'supervisor.py'
                        tracked.write_text(tracked.read_text() + '\n# Local source change\n')
                        reason = 'publication source has local changes'

                    def snapshot():
                        return {entry.relative_to(fixture.root):
                                (entry.stat().st_mtime_ns, entry.read_bytes() if entry.is_file() else None)
                                for entry in fixture.root.rglob('*')}

                    before = snapshot()
                    with patch.object(engine, 'recover', side_effect=AssertionError('plan started recovery')), \
                         patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('plan dispatched')):
                        plan = engine.run(plan_only=True)
                    self.assertEqual(plan['phase'], 'deferred', plan)
                    self.assertIn(reason, plan['summary'])
                    self.assertEqual(snapshot(), before)
                finally:
                    phases.doCleanups()

    def test_implementation_plan_refuses_incomplete_monitor_or_changed_source(self):
        for damage in ('monitor', 'source'):
            with self.subTest(damage=damage):
                phases = phase_fixtures.EvolutionPhaseExecutionTests()
                try:
                    fixture = phases.fixture()
                    path = phases.interrupt(fixture, 'implementing')
                    engine, _ = phases.resumer(fixture)
                    if damage == 'monitor':
                        monitor = path.parent / 'monitor'
                        monitor.mkdir(exist_ok=True)
                        (monitor / 'requirement.md').write_text('interrupted monitor creation')
                        reason = 'no resumable checkpoint'
                    else:
                        source = fixture.source / 'auto_moniter' / 'supervisor.py'
                        source.write_text(source.read_text() + '\n# Local edit\n')
                        reason = 'recovery source changed'

                    def snapshot():
                        return {entry.relative_to(fixture.root):
                                (entry.stat().st_mtime_ns, entry.read_bytes() if entry.is_file() else None)
                                for entry in fixture.root.rglob('*')}

                    before = snapshot()
                    with patch.object(engine, 'recover', side_effect=AssertionError('plan started recovery')), \
                         patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('plan dispatched')):
                        plan = engine.run(plan_only=True)
                    self.assertEqual(plan['phase'], 'deferred', plan)
                    self.assertIn(reason, plan['summary'])
                    self.assertEqual(snapshot(), before)
                finally:
                    phases.doCleanups()

    def test_damaged_stage_receipts_are_refused_without_writes(self):
        phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(phases.doCleanups)
        for phase, stage in (('implementing', 'baseline'), ('ready', 'candidate')):
            with self.subTest(stage=stage):
                fixture = phases.fixture()
                path = phases.interrupt(fixture, phase)
                engine, _ = phases.resumer(fixture)

                def snapshot():
                    return {
                        file.relative_to(fixture.root): (file.stat().st_mtime_ns,
                                                        file.read_bytes() if file.is_file() else None)
                        for file in fixture.root.rglob('*')
                    }

                with patch.object(engine, 'recover', side_effect=AssertionError('plan started recovery')), \
                     patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('plan dispatched')):
                    before = snapshot()
                    plan = engine.run(plan_only=True)
                    self.assertEqual(plan['action'], 'recover')
                    self.assertEqual(snapshot(), before)

                    receipt = path.parent / f'{stage}-01.log'
                    receipt.write_text('changed after the check passed')
                    before = snapshot()
                    plan = engine.run(plan_only=True)
                    self.assertEqual(plan['phase'], 'deferred', plan)
                    self.assertIn('check evidence changed: ' + stage, plan['summary'])
                    self.assertNotIn('recovery_directory', plan)
                    self.assertEqual(snapshot(), before)
                    self.assertEqual(engine.record, {})
                    self.assertIsNone(engine.path)

    def test_ready_plan_refuses_missing_publication_receipts_without_writes(self):
        phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(phases.doCleanups)
        fixture = phases.fixture()
        path = phases.interrupt(fixture, 'ready')
        engine, _ = phases.resumer(fixture)
        record = json.loads(path.read_text())
        del record['candidate_checks'], record['candidate_checks_complete']
        write_json(path, record)

        def snapshot():
            return {
                entry.relative_to(fixture.root): (entry.stat().st_mtime_ns,
                                                 entry.read_bytes() if entry.is_file() else None)
                for entry in fixture.root.rglob('*')
            }

        before = snapshot()
        with patch.object(engine, 'recover', side_effect=AssertionError('plan started recovery')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('plan dispatched')):
            plan = engine.run(plan_only=True)
        self.assertEqual(plan['phase'], 'deferred', plan)
        self.assertIn('lacks complete candidate checks', plan['summary'])
        self.assertNotIn('recovery_directory', plan)
        self.assertEqual(snapshot(), before)
        self.assertEqual(engine.record, {})
        self.assertIsNone(engine.path)

    def test_ready_plan_refuses_dirty_candidate_worktree_without_writes(self):
        phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(phases.doCleanups)
        fixture = phases.fixture()
        path = phases.interrupt(fixture, 'ready')
        engine, _ = phases.resumer(fixture)
        tracked = path.parent / 'workspace' / 'auto_moniter' / 'supervisor.py'
        tracked.write_text(tracked.read_text() + '\n# Local edit after independent review\n')

        def snapshot():
            return {
                entry.relative_to(fixture.root): (entry.stat().st_mtime_ns,
                                                 entry.read_bytes() if entry.is_file() else None)
                for entry in fixture.root.rglob('*')
            }

        before = snapshot()
        with patch.object(engine, 'recover', side_effect=AssertionError('plan started recovery')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('plan dispatched')):
            plan = engine.run(plan_only=True)
        self.assertEqual(plan['phase'], 'deferred', plan)
        self.assertIn('candidate is not a clean continuation', plan['summary'])
        self.assertNotIn('recovery_directory', plan)
        self.assertEqual(snapshot(), before)
        self.assertEqual(engine.record, {})
        self.assertIsNone(engine.path)

    def test_rollback_plan_refuses_changed_rollback_commit_without_writes(self):
        phases = phase_fixtures.EvolutionPhaseExecutionTests()
        self.addCleanup(phases.doCleanups)
        fixture = phases.fixture()
        path = phases.interrupt(fixture, 'ready')
        engine, _ = phases.resumer(fixture)
        record = json.loads(path.read_text())
        record.update(phase='rolling_back', resume_phase='rolling_back', rollback_commit=record['candidate'])
        write_json(path, record)

        def snapshot():
            return {
                entry.relative_to(fixture.root): (entry.stat().st_mtime_ns,
                                                 entry.read_bytes() if entry.is_file() else None)
                for entry in fixture.root.rglob('*')
            }

        before = snapshot()
        with patch.object(engine, 'recover', side_effect=AssertionError('plan started recovery')), \
             patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('plan dispatched')):
            plan = engine.run(plan_only=True)
        self.assertEqual(plan['phase'], 'deferred', plan)
        self.assertIn('rollback evidence changed', plan['summary'])
        self.assertNotIn('recovery_directory', plan)
        self.assertEqual(snapshot(), before)
