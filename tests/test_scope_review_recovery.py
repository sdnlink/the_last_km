"""An existing approval survives repair of the monitor's function locator."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result
from tests.test_supervisor import ScriptedProvider


class ScopeReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        (self.root/'app.py').write_text('def parse_input():\n    return 0\n')

    def monitor(self,worker,reviewer,**kwargs):
        return Supervisor(RunConfig('Implement the original request',self.root,worker,reviewer=reviewer,
            workflow='review-first',function_dispatch=True,git_history='off',run_dir=self.root/'run',
            retry_delay_s=0,max_rounds=1,checks=[CHECK],**kwargs),log=lambda _:None)

    def stopped_scope_review(self):
        worker=FunctionWorker('worker',[{**result('F1','done',status='done'),
            'source':'def parse_input():\n    return 42\n'}])
        reviewer=ScriptedProvider('reviewer',[plan(final_task()),result('F1','accepted',status='done')])
        monitor=self.monitor(worker,reviewer)
        emit=monitor._event
        def event(kind,**data):
            emit(kind,**data)
            if kind=='function_gate_rejected':monitor.request_stop()
        monitor._event=event
        with patch.object(Supervisor,'_function_scope_problems',return_value=[
                'Implement or change the assigned function: app.py::parse_input']):
            report=monitor.run()
        self.assertEqual(report.status,'stopped',report.summary)
        return monitor,report

    def test_repaired_locator_resumes_watcher_checks_without_worker_or_reviewer_calls(self):
        self.stopped_scope_review()
        worker=FunctionWorker('worker',[])
        reviewer=ScriptedProvider('reviewer',[])
        report=self.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertFalse(worker.calls or reviewer.calls)
        self.assertEqual((self.root/'app.py').read_text(),'def parse_input():\n    return 42\n')
        self.assertEqual(report.records[-1].verdict.task_status,'accepted')

    def test_changed_candidate_cannot_reuse_prior_approval(self):
        monitor,report=self.stopped_scope_review()
        (self.root/'app.py').write_text('def parse_input():\n    return 99\n')
        self.assertIsNone(monitor._scope_retry_review(report.records[-1]))

    def test_real_failed_check_is_not_treated_as_a_locator_repair(self):
        monitor,report=self.stopped_scope_review()
        record=report.records[-1]
        record.watcher_result['reasons']=['Behavioral regression check failed']
        self.assertIsNone(monitor._scope_retry_review(record))


if __name__=='__main__':unittest.main()
