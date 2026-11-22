"""Cancelled dependencies cannot consume the dependent task's entire budget."""
import unittest

from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from tests import test_project_workspace as projects
from tests.test_supervisor import ScriptedProvider


class CancelledDependencyWaitTests(unittest.TestCase):
    def test_cancelled_dependency_stops_before_any_model_call_and_stays_blocked_on_resume(self):
        fixture = projects.ProjectWorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prerequisite = fixture.workspace()
        dependent = fixture.workspace(['other.py'], [prerequisite.manifest['id']])
        write_json(prerequisite.run / 'project-lifecycle.json', {'status': 'deleted'})
        for resume in (False, True):
            with self.subTest(resume=resume):
                worker = ScriptedProvider('worker', [])
                reviewer = ScriptedProvider('reviewer', [])
                monitor = Supervisor(RunConfig('Implement dependent work', dependent.cwd, worker,
                    reviewer=reviewer, workflow='review-first', git_history='required',
                    run_dir=dependent.run, resume=resume, max_runtime_s=30), log=lambda _: None)
                report = monitor.run()
                self.assertEqual(report.status, 'action_required', report.summary)
                self.assertIn(prerequisite.manifest['id'], report.summary)
                self.assertEqual(worker.calls, [])
                self.assertEqual(reviewer.calls, [])
                self.assertLess(monitor._elapsed(), 10)

    def test_stop_request_takes_precedence_over_a_cancelled_dependency(self):
        fixture = projects.ProjectWorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prerequisite = fixture.workspace()
        dependent = fixture.workspace(['other.py'], [prerequisite.manifest['id']])
        write_json(prerequisite.run / 'project-lifecycle.json', {'status': 'deleted'})
        write_json(dependent.run / 'control.json', {'action': 'stop'})
        monitor = Supervisor(RunConfig('Implement dependent work', dependent.cwd,
            ScriptedProvider('worker', []), reviewer=ScriptedProvider('reviewer', []),
            workflow='review-first', git_history='required', run_dir=dependent.run,
            max_runtime_s=30), log=lambda _: None)
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertIn('stop command', report.summary)
