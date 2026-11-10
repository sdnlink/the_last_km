import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.audit import AuditLedger
from auto_moniter.modules import ModuleLedger
from auto_moniter.providers import PreparedCommand
from auto_moniter.runtime_policy import RetryWatch, reserve_seconds
from auto_moniter.supervisor import Supervisor, RunConfig, run_command
from auto_moniter.verdict import Verdict
from tests.test_supervisor import ScriptedProvider, _verdict
from tests.test_review_first import BRIEF

TREE='a'*64
PLAN=[{'id':key,'title':key,'acceptance':['works']} for key in ['Web','Native']]

class ScopeAndHistoryTests(unittest.TestCase):
    def test_review_scopes_preserve_independent_acceptance_but_shared_changes_revalidate(self):
        m=ModuleLedger(PLAN)
        for key,paths in [('Web',['preview/**','shared/**']),('Native',['Sources/**','shared/**'])]:
            m.review_scope(key,paths);m.items[key].update(status='accepted',tree=TREE)
        m=ModuleLedger.restore(m.export())
        self.assertEqual(m.invalidate('b'*64,['Sources/app.swift']),['Native'])
        self.assertEqual(m.items['Web']['status'],'accepted')
        self.assertEqual(m.invalidate('c'*64,['shared/config.json']),['Web'])
        self.assertTrue(m.items['Web']['evidence']==[])

    def test_changes_require_revalidation_without_erasing_last_closure(self):
        a=AuditLedger();a.record(['Web/empty input fails'],1,['Web'])
        resolution={'id':'AUD-1','summary':'Fixed input','evidence':['preview/index.html']}
        a.resolve([resolution],2,TREE,lambda _:[])
        old=a.items[0]['resolution']
        self.assertEqual(a.invalidate('b'*64,{'Native'},['Sources/app.swift']),[])
        self.assertEqual(a.invalidate('c'*64,{'Web'},['preview/index.html']),['AUD-1'])
        self.assertEqual(a.pending()[0]['status'],'needs_review')
        self.assertEqual(a.pending()[0]['resolution'],old)
        a=AuditLedger.restore(a.export())
        self.assertEqual(a.invalidate('d'*64,{'Web'},['preview/index.html']),[])
        a.record(['AUD-1: reproduced empty input failure'],3,['Web'])
        self.assertEqual(a.pending()[0]['status'],'open')
        self.assertIsNone(a.pending()[0]['resolution'])

    def test_worker_cannot_set_scopes_but_reviewer_can_and_user_scope_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Supervisor(RunConfig('task',Path(tmp),ScriptedProvider('worker',[]),no_review=True,modules=PLAN))
            proposal={**PLAN[0],'files':['preview/**']}
            w=Verdict('continue',modules=[proposal]);s._merge_modules(w)
            self.assertTrue(w.parse_error)
            r=Verdict('continue',modules=[proposal]);s._merge_modules(r,reviewer=True)
            self.assertFalse(r.parse_error)
            self.assertEqual(s.modules.impact_files('Web'),['preview/**'])
            fixed=ModuleLedger([{**PLAN[0],'files':['fixed/**']}]);fixed.review_scope('Web',['different/**'])
            self.assertEqual(fixed.impact_files('Web'),['fixed/**'])
            self.assertEqual(s.modules.affected(['unmapped.config']),{'Web','Native'})

class RetryAndBudgetTests(unittest.TestCase):
    def test_model_retry_exhaustion_stops_loop_without_another_worker_call(self):
        class NetworkWorker(ScriptedProvider):
            def _command(inner, session, prompt, read_only):
                inner.calls.append({'prompt':prompt.read_text()})
                return PreparedCommand([sys.executable,'-c',
                    "import sys,time\nwhile True:\n print('Reconnecting... 502 Bad Gateway',file=sys.stderr,flush=True);time.sleep(.02)"])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);worker=NetworkWorker('worker',[])
            reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)}])
            report=Supervisor(RunConfig('task',root,worker,reviewer=reviewer,workflow='review-first',
                git_history='off',run_dir=root/'run',quality_policy={'network_retry_s':1},
                max_rounds=5,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,1))
            self.assertIn('transport retry budget',report.summary)

    def test_continuous_retry_output_does_not_extend_the_transport_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            code="import sys,time\nwhile True:\n print('Reconnecting... 1/5 (502 Bad Gateway)',file=sys.stderr,flush=True);time.sleep(.02)"
            result=run_command(PreparedCommand([sys.executable,'-c',code]),Path(tmp),timeout_s=3,
                               idle_timeout_s=1,network_retry_s=.12)
            self.assertEqual(result.interruption,'network retry budget')
            self.assertFalse(result.timed_out)
            self.assertIn('502',result.stderr)

    def test_retry_timer_recovers_only_after_substantive_output(self):
        watch=RetryWatch(10)
        with patch('auto_moniter.runtime_policy.time.monotonic',return_value=1):
            watch.feed(1,b'Reconnect');watch.feed(1,b'ing... 1/5\n')
        with patch('auto_moniter.runtime_policy.time.monotonic',return_value=12):
            watch.feed(0,b'{"type":"turn.started"}\n');self.assertTrue(watch.exhausted())
            watch.feed(0,b'{"type":[]}\n{"type":"item.completed","item":{"type":"error","message":"502 upstream"}}\n')
            self.assertTrue(watch.exhausted())
            watch.feed(0,b'{"type":"item.completed","item":{"type":"command_execution"}}\n')
            self.assertFalse(watch.exhausted())

    def test_reserved_time_stops_new_work_and_saves_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            worker=ScriptedProvider('worker',[])
            s=Supervisor(RunConfig('task',root,worker,no_review=True,run_dir=root/'run',git_history='off',max_runtime_s=600),log=lambda _:None)
            original=s._elapsed
            s._elapsed=lambda:580 if (root/'run/state.json').exists() else original()
            report=s.run()
            self.assertEqual(report.status,'budget_exceeded',report.summary)
            self.assertEqual(worker.calls,[])
            handoff=json.loads((root/'run/handoff.json').read_text())
            self.assertIn('reserved for handoff',handoff['reason'])
            self.assertEqual(handoff['usage']['agent_calls'],0)
            self.assertEqual(reserve_seconds(7200),120)
            self.assertEqual(reserve_seconds(0),0)

    def test_environment_evidence_is_saved_and_reused_in_next_worker_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            blocked={**_verdict('blocked'),'blockers':['Apple SDK unavailable: no SwiftUI on Linux']}
            worker=ScriptedProvider('worker',[{'verdict':blocked,'touch':'app.py'}, {'verdict':_verdict('continue',gaps=['independent work']),'touch':'next.py'}])
            reviewer=ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)},{'verdict':blocked}])
            report=Supervisor(RunConfig('task',root,worker,reviewer=reviewer,workflow='review-first',git_history='off',
                run_dir=root/'run',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
            self.assertEqual(report.status,'max_rounds',report.summary)
            self.assertIn('Apple SDK unavailable',worker.calls[1]['prompt'])
            self.assertIn('not proof of code defects',worker.calls[1]['prompt'])
            self.assertTrue((root/'run/environment-blocks.json').exists())

    def test_format_only_acceptance_is_explicitly_incomplete(self):
        from console.acceptance import coverage_warning
        self.assertIn('不能证明功能',coverage_warning(['git diff --check']))
        self.assertEqual(coverage_warning(['node --test behavior.cjs','git diff --check']),'')
