"""Atomic app changes keep explicit permissions and recover without unchanged writers."""
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter import function_gate, function_tasks
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import FunctionWorker, plan, result
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider

CHECK = "python3 -c \"from app import parse_input; assert parse_input() == 42\""
SOURCE = "from pathlib import Path\nimport json\ndef parse_input():\n    return load_route()\ndef load_route():\n    return json.loads(Path('routes.json').read_text())['steps']\n"
WRITES = {'app.py':SOURCE, 'routes.json':'{"steps":42}\n', 'NOTE.md':'Original climbing route.\n'}
EDITS = list(WRITES)


class AssignmentEditScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'app.py').write_text('def parse_input():\n    return 0\n')

    def monitor(self, worker, reviewer, **extra):
        return Supervisor(RunConfig('Implement the route and wire its resources',self.root,worker,
            reviewer=reviewer,workflow='review-first',function_dispatch=True,checks=[CHECK],
            git_history='off',run_dir=self.root/'run',max_rounds=2,retry_delay_s=0,**extra),log=lambda _:None)

    def response(self, decision, **extra):
        value=result('F1',decision,status='done' if decision in ('done','accepted') else 'continue',**extra)
        value['verdict']['evidence']=EDITS
        return value

    def test_atomic_source_helpers_resources_and_docs_are_checked_and_hash_bound(self):
        assignment=final_task();assignment['edit_files']=EDITS
        worker=FunctionWorker('worker',[{**self.response('done'),'writes':WRITES}])
        reviewer=ScriptedProvider('reviewer',[plan(assignment),self.response('accepted')])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        state=json.loads((self.root/'run/state.json').read_text())
        ledger=function_tasks.FunctionTasks(state['function_tasks'])
        self.assertEqual(set(ledger.accepted[0]['file_hashes']),set(EDITS))
        receipt=json.loads((self.root/'run/acceptance-results.json').read_text())
        self.assertTrue(receipt['complete']);self.assertTrue(all(c['status']=='passed' for c in receipt['checks']))
        verify=final_task('VERIFY');verify['criterion_ids']=['main:1','req:1:1']
        modules={m['id']:m for m in state['modules']}
        self.assertEqual(function_gate.coverage(ledger,modules,verify,BRIEF,self.root),[])
        (self.root/'routes.json').write_text('{"steps":13}\n')
        self.assertEqual(function_gate.coverage(ledger,modules,verify,BRIEF,self.root),['req:1:2'])

    def test_edit_manifest_rejects_patterns_aliases_symlinks_and_test_changes(self):
        assignment=final_task()
        modules={'main':{'acceptance':['Original behavior']}}
        (self.root/'tests').mkdir();(self.root/'tests/test_route.py').write_text('assert True\n')
        (self.root/'linked.json').symlink_to(self.root/'routes.json')
        for name in ['../outside','dir/*','./alias.json','linked.json','tests/test_route.py','new/test_other.py','.git/config']:
            with self.subTest(name=name),self.assertRaises(ValueError):
                value={**assignment,'edit_files':[name]}
                value=function_tasks.validate(value,modules)
                function_gate.validate_task(value,self.root,modules,[CHECK],BRIEF)

    def test_repeated_unchanged_scope_failure_replans_once_and_reuses_candidate(self):
        legacy=final_task()
        fixed={**legacy,'edit_files':EDITS}
        replanned=plan(fixed);value=json.loads(replanned['text']);value['modules']=[];replanned['text']=json.dumps(value)
        accepted=self.response('accepted')
        accepted['verdict']['issue_resolutions']=[{'id':'AUD-1','summary':'Approved manifest matches the atomic route change',
                                                   'evidence':EDITS}]
        worker=FunctionWorker('worker',[{**self.response('done'),'writes':WRITES},self.response('done')])
        reviewer=ScriptedProvider('reviewer',[plan(legacy),self.response('changes_required',gaps=['Scope manifest needs route files']),
            self.response('changes_required',gaps=['Scope manifest needs route files']),replanned,accepted])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,5))
        self.assertTrue(reviewer.calls[3]['read_only'])
        events=(self.root/'run/events.jsonl').read_text()
        self.assertIn('assignment_scope_replanning',events)
        self.assertIn('worker_candidate_reused',events)
        self.assertEqual((self.root/'app.py').read_text(),SOURCE)

    def test_unsolved_scope_replan_stops_without_a_third_unchanged_worker(self):
        legacy=final_task();replanned=plan(legacy)
        value=json.loads(replanned['text']);value['modules']=[];replanned['text']=json.dumps(value)
        worker=FunctionWorker('worker',[{**self.response('done'),'writes':WRITES},self.response('done')])
        reviewer=ScriptedProvider('reviewer',[plan(legacy),self.response('changes_required',gaps=['Scope manifest needs route files']),
            self.response('changes_required',gaps=['Scope manifest needs route files']),replanned])
        report=self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('scope repair still rejects',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,4))
        self.assertEqual((self.root/'app.py').read_text(),SOURCE)

    def test_verification_or_integration_cannot_expand_scope_through_edit_files(self):
        value={**final_task(),'kind':'verification','edit_files':['routes.json']}
        with self.assertRaisesRegex(ValueError,'verified implementation'):
            function_tasks.validate(value)

    def test_scope_repair_cannot_change_the_primary_target_or_weaken_checks(self):
        from auto_moniter.modules import ModuleLedger
        current=final_task()
        monitor=self.monitor(FunctionWorker('worker',[]),ScriptedProvider('reviewer',[]))
        monitor.modules=ModuleLedger([{'id':'main','title':'Original behavior','acceptance':['Original behavior']}])
        monitor.function_tasks.assign(current,monitor.modules.items)
        monitor._planning_rejection={'scope_repair':True}
        for changes in [{'symbol':'unrelated'}, {'check_ids':['auto:1']}, {'criterion_ids':['main:1']}]:
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'preserve the current assignment'):
                monitor._stage_requirement_plan({**current,'edit_files':EDITS,**changes},[],None,BRIEF,next_function=True)


if __name__=='__main__':unittest.main()
