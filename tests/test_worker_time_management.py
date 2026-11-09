"""Review checkpoints yield at turn boundaries without truncating active coding."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RunConfig, Supervisor, run_command
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class WorkerTimeManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def monitor(self, worker, reviewer, **options):
        return Supervisor(RunConfig('Implement only reviewed requirements', self.root, worker,
            reviewer=reviewer, workflow='review-first', git_history='off', run_dir=self.root/'run',
            retry_delay_s=0, **options), log=lambda _: None)

    def brief(self):
        return {'verdict': {}, 'text': json.dumps(BRIEF)}

    def test_review_interval_does_not_shorten_worker_hard_limit(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = ScriptedProvider('reviewer', [self.brief(), {'verdict': _verdict('done', evidence=['app.py'])}])
        limits = []
        def observed(*args, **kwargs):
            limits.append(kwargs['timeout_s'])
            return run_command(*args, **kwargs)
        with patch('auto_moniter.supervisor.run_command', observed):
            report = self.monitor(worker, reviewer, max_rounds=1, turn_timeout_s=900, max_runtime_s=3600,
                quality_policy={'review_interval_s': 600}).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(limits, [900, 900, 900])
        self.assertIn('one reviewer-approved item', worker.calls[0]['prompt'])
        self.assertIn('Remaining total budget', worker.calls[0]['prompt'])

    def test_elapsed_interval_audits_at_normal_return_and_reuses_worker_session(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('continue', gaps=['part remains']), 'touch': 'one.py'},
            {'verdict': _verdict('continue', gaps=['part remains']), 'touch': 'two.py'},
            {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        for script in worker.scripts:
            script['session_id'] = '11111111-1111-1111-1111-111111111111'
        reviewer = ScriptedProvider('reviewer', [self.brief(),
            {'verdict': _verdict('continue', gaps=['Complete the remaining reviewed item'])},
            {'verdict': _verdict('done', evidence=['app.py'], closed=['AUD-1'])}])
        monitor = self.monitor(worker, reviewer, max_rounds=3, review_every=100, restart_after_stall=0,
            quality_policy={'review_interval_s': 600})
        invoke = monitor._invoke
        def elapsed(*args, **kwargs):
            turn = invoke(*args, **kwargs)
            if kwargs['role'] == 'worker': turn.elapsed_s = 310
            return turn
        with patch.object(monitor, '_invoke', elapsed): report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(3,3))
        self.assertTrue(worker.calls[2]['session_id'])
        state = json.loads((self.root/'run/state.json').read_text())
        self.assertEqual(state['review_schedule']['worker_seconds_since_review'], 0)

    def test_elapsed_review_time_survives_resume(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue', gaps=['still working']), 'touch': 'one.py'}])
        monitor = self.monitor(worker, ScriptedProvider('reviewer',[self.brief()]), max_rounds=1,
            quality_policy={'review_interval_s':600},review_every=100)
        monitor.log=lambda message: monitor.request_stop() if 'worker status=continue' in message else None
        invoke = monitor._invoke
        def elapsed(*args, **kwargs):
            turn=invoke(*args, **kwargs)
            if kwargs['role']=='worker': turn.elapsed_s=400
            return turn
        with patch.object(monitor,'_invoke',elapsed):
            self.assertEqual(monitor.run().status,'stopped')
        reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('continue',gaps=['finish implementation'])}])
        resumed=self.monitor(ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['still working']), 'touch':'two.py'}]),
            reviewer,resume=True,max_rounds=2)
        invoke=resumed._invoke
        def second(*args,**kwargs):
            turn=invoke(*args,**kwargs)
            if kwargs['role']=='worker':turn.elapsed_s=250
            return turn
        with patch.object(resumed,'_invoke',second): report=resumed.run()
        self.assertEqual(report.status,'max_rounds',report.summary)
        self.assertEqual(len(reviewer.calls),1)

    def test_hard_timeout_still_limits_worker(self):
        class Slow(ScriptedProvider):
            def _command(inner,*args):
                command=super()._command(*args)
                script=Path(command.argv[-1]);script.write_text('import time; time.sleep(5)\n'+script.read_text())
                return command
        worker=Slow('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
        report=self.monitor(worker,ScriptedProvider('reviewer',[self.brief()]),max_rounds=1,
            turn_timeout_s=.2,quality_policy={'review_interval_s':1}).run()
        self.assertNotEqual(report.status,'done')
        record=next(r for r in report.records if r.role=='worker')
        self.assertTrue(record.timed_out)
        self.assertEqual(record.interruption,'turn limit')
