"""Cross-run references and invalid closures cannot force implementation retries."""
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.audit import AuditLedger
from auto_moniter.supervisor import Supervisor, RunConfig
from tests.test_supervisor import ScriptedProvider, _verdict
from tests.test_review_first import BRIEF


class CrossRunAuditTests(unittest.TestCase):
    def test_foreign_findings_keep_provenance_and_cannot_alias_newly_allocated_ids(self):
        a=AuditLedger()
        original=['M/first problem','AUD-1: M/second inherited problem','AUD-6: M/third problem']
        findings,refs=a.local_findings(original)
        ids=a.record(findings,1,['M'],known_modules={'M'})
        self.assertEqual(ids,['AUD-1','AUD-2','AUD-3'])
        self.assertEqual([r['source_id'] for r in refs],['AUD-1','AUD-6'])
        self.assertIn('second inherited problem',a.items[1]['summary'])
        self.assertIn('external audit reference: AUD-1',a.items[1]['summary'])
        self.assertEqual(a.items[1]['module_ids'],['M'])
        # An unknown closure is never translated into permission to close local IDs.
        with self.assertRaisesRegex(ValueError,'Unknown audit issue AUD-6'):
            a.resolve([{'id':'AUD-6','summary':'fixed','evidence':['app.py']}],2,'a'*64,lambda _:[])
        self.assertEqual(len(a.pending()),3)

    def run_monitor(self,root,worker,reviewer,**extra):
        return Supervisor(RunConfig('implement task',root,worker,reviewer=reviewer,
            workflow='review-first',git_history='off',run_dir=root/'run',max_rounds=1,
            review_every=1,retry_delay_s=0,**extra),log=lambda _:None).run()

    def brief(self):return {'verdict':{},'text':json.dumps(BRIEF)}

    def test_progress_review_foreign_findings_are_registered_without_an_extra_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['more work']),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':_verdict('continue',gaps=['Real new defect','AUD-6: main/inherited defect','AUD-7: main/other defect'])}])
            report=self.run_monitor(root,worker,reviewer)
            self.assertEqual(report.status,'max_rounds',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(len(state['audit_issues']['items']),3)
            self.assertEqual(state['rounds'][-1]['watcher_result']['status'],'applied')
            self.assertTrue(all(i['status']=='open' for i in state['audit_issues']['items']))

    def test_invalid_closure_gets_one_reviewer_only_repair_and_preserves_new_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            invalid=_verdict('continue',gaps=['app.py: Missing rejection case'],closed=['AUD-99'],evidence=['app.py'])
            worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':invalid},
                {'verdict':_verdict('continue',gaps=['AUD-1: Missing rejection case'])}])
            report=self.run_monitor(root,worker,reviewer)
            self.assertEqual(report.status,'max_rounds',report.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,3))
            repair=reviewer.calls[-1]
            self.assertTrue(repair['read_only'])
            self.assertIn('Reviewer-only repair of the SAME candidate',repair['prompt'])
            self.assertIn('Unknown audit issue AUD-99',repair['prompt'])
            self.assertIn('Authoritative current-run audit ledger',repair['prompt'])
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['audit_issues']['items'][0]['id'],'AUD-1')
            self.assertEqual(state['audit_issues']['items'][0]['status'],'open')

    def test_unchanged_continue_candidate_resumes_review_only_after_audit_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            invalid=_verdict('continue',closed=['AUD-99'],evidence=['app.py'])
            worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['work remains']),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':invalid},{'verdict':invalid}])
            report=self.run_monitor(root,worker,reviewer)
            self.assertEqual(report.status,'action_required',report.summary)
            self.assertIn('after reviewer-only retry',report.summary)
            self.assertIn('Unknown audit issue AUD-99',report.summary)
            unused=ScriptedProvider('worker',[])
            resumed=ScriptedProvider('reviewer',[{'verdict':_verdict('continue',gaps=['main/actual remaining defect'])}])
            result=self.run_monitor(root,unused,resumed,resume=True)
            self.assertEqual(result.status,'max_rounds',result.summary)
            self.assertEqual((len(unused.calls),len(resumed.calls)),(0,1))
            self.assertTrue(resumed.calls[0]['read_only'])
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['usage']['agent_calls'],5)
            self.assertEqual(state['rounds'][-1]['watcher_result']['status'],'applied')

    def test_new_source_prevents_reusing_rejected_progress_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            invalid=_verdict('continue',closed=['AUD-99'],evidence=['app.py'])
            worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['work remains']),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':invalid},{'verdict':invalid}])
            self.run_monitor(root,worker,reviewer)
            (root/'app.py').write_text('new version')
            unused=ScriptedProvider('worker',[]);r=ScriptedProvider('reviewer',[])
            result=self.run_monitor(root,unused,r,resume=True)
            self.assertEqual(result.status,'max_rounds',result.summary)
            self.assertEqual(r.calls,[])

    def test_repaired_closure_can_deliver_only_after_valid_current_run_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            invalid=_verdict('continue',gaps=['app.py: Case needs independent confirmation'],closed=['AUD-99'],evidence=['app.py'])
            worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':invalid},
                {'verdict':_verdict('done',closed=['AUD-1'],evidence=['app.py'])}])
            result=self.run_monitor(root,worker,reviewer,checks=['test -f app.py'])
            self.assertEqual(result.status,'done',result.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,3))
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['audit_issues']['items'][0]['status'],'resolved')
            self.assertEqual(state['rounds'][-1]['watcher_result']['closed'],['AUD-1'])
            self.assertTrue((root/'run/001-check-01.log').exists())

    def test_reviewer_only_repair_does_not_bypass_call_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            invalid=_verdict('continue',closed=['AUD-99'],evidence=['app.py'])
            worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['more work']),'touch':'app.py'}])
            reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':invalid}])
            result=self.run_monitor(root,worker,reviewer,max_agent_calls=3)
            self.assertEqual(result.status,'budget_exceeded',result.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            state=json.loads((root/'run/state.json').read_text())
            self.assertEqual(state['usage']['agent_calls'],3)
            self.assertTrue((root/'run/handoff.json').exists())
