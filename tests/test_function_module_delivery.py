"""Final function reports jointly confirm the whole requirement's module ledger."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.supervisor import RunConfig, Supervisor, RoundRecord
from tests.test_function_dispatch import CHECK, FunctionWorker, plan, result, task
from tests.test_supervisor import ScriptedProvider


MODULES = [
    {'id':'A', 'title':'Input model', 'acceptance':['Input model works']},
    {'id':'B', 'title':'Integrated product', 'acceptance':['Product integrates the model'], 'depends_on':['A']},
]


def assignment(ident, file, module, *, final=False):
    value = task(ident, file, depends=['F1'] if final else [], scope='requirement' if final else 'function')
    value.update(module_id=module, criterion_ids=[module + ':1', 'req:1:2' if final else 'req:1:1'])
    return value


def conclusion(ident, decision, ids, *, final=False, file='app.py', next_task=None):
    value = result(ident, decision, status='done' if final else 'continue', file=file, next_task=next_task)
    value['verdict']['module_ids'] = ids
    return value


class FunctionModuleDeliveryTests(unittest.TestCase):
    def test_prerequisite_module_verification_allows_the_dependent_milestone_and_final_delivery(self):
        implementation = {**self.final,'delivery_scope':'function'}
        verify = {**self.first,'id':'VERIFY_A','kind':'verification','delivery_scope':'module','depends_on':['F2']}
        milestone = {**self.final,'id':'VERIFY_B','kind':'verification','delivery_scope':'module','depends_on':['VERIFY_A']}
        final = {**self.final,'id':'FINAL','kind':'verification','depends_on':['VERIFY_B'],
                 'criterion_ids':['A:1','B:1','req:1:1','req:1:2']}
        def delivery(ident,role,ids,file,scope,next_task=None):
            value=conclusion(ident,role,ids,final=True,file=file,next_task=next_task)
            value['verdict']['scope']=scope
            return value
        worker = FunctionWorker('worker',[
            {**conclusion('F1','done',['A']),'touch':'app.py'},
            {**conclusion('F2','done',['B'],file='next.py'),'touch':'next.py'},
            delivery('VERIFY_A','done',['A'],'app.py','module'),
            delivery('VERIFY_B','done',['B'],'next.py','module'),
            delivery('FINAL','done',['A','B'],'next.py','requirement')])
        reviewer = ScriptedProvider('reviewer',[self.planning(),
            conclusion('F1','accepted',[],next_task=implementation),
            conclusion('F2','accepted',[],file='next.py',next_task=verify),
            delivery('VERIFY_A','accepted',['A'],'app.py','module',milestone),
            delivery('VERIFY_B','accepted',['B'],'next.py','module',final),
            delivery('FINAL','accepted',['A','B'],'next.py','requirement')])
        monitor=self.monitor(worker,reviewer)
        monitor.config.max_rounds=5
        report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual([m['status'] for m in monitor.modules.items.values()],['accepted','accepted'])
        self.assertEqual([call['read_only'] for call in worker.calls],[False,False,True,True,True])

    def test_full_plan_rejects_dependent_module_delivery_without_a_prerequisite_milestone(self):
        from auto_moniter.function_tasks import FunctionTasks
        from auto_moniter.modules import ModuleLedger
        from tests.test_review_first import BRIEF
        monitor=self.monitor(self.workers(),self.reviewers())
        modules=ModuleLedger(MODULES)
        ledger=FunctionTasks()
        ledger.current=self.first
        milestone={**self.final,'delivery_scope':'module'}
        final={**self.final,'id':'FINAL','depends_on':['F2']}
        with self.assertRaisesRegex(ValueError,'prerequisite module acceptance'):
            monitor._validate_complete_function_plan([self.first,milestone,final],ledger,modules.items,BRIEF)
        implementation={**milestone,'delivery_scope':'function'}
        verify_a={**self.first,'id':'VERIFY_A','kind':'verification','delivery_scope':'module','depends_on':['F2']}
        verify_b={**milestone,'id':'VERIFY_B','kind':'verification','depends_on':['VERIFY_A']}
        final={**final,'kind':'verification','depends_on':['VERIFY_B']}
        planned=monitor._validate_complete_function_plan([self.first,implementation,verify_a,verify_b,final],
                                                         ledger,modules.items,BRIEF)
        self.assertEqual(len(planned['tasks']),5)

    def test_full_plan_accounts_for_source_edits_invalidating_prior_module_acceptance(self):
        from auto_moniter.function_tasks import FunctionTasks
        from auto_moniter.modules import ModuleLedger
        from tests.test_review_first import BRIEF
        monitor=self.monitor(self.workers(),self.reviewers())
        ledger=FunctionTasks();ledger.current=self.first
        verify_a={**self.first,'id':'VERIFY_A','kind':'verification','delivery_scope':'module','depends_on':['F1']}
        milestone={**self.final,'delivery_scope':'module','depends_on':['VERIFY_A']}
        final={**self.final,'id':'FINAL','kind':'verification','depends_on':['F2']}
        modules=ModuleLedger(MODULES)
        with self.assertRaisesRegex(ValueError,'prerequisite module acceptance'):
            monitor._validate_complete_function_plan([self.first,verify_a,milestone,final],ledger,modules.items,BRIEF)
        scoped=ModuleLedger([{**MODULES[0],'files':['app.py']},{**MODULES[1],'files':['next.py']}])
        planned=monitor._validate_complete_function_plan([self.first,verify_a,milestone,final],ledger,scoped.items,BRIEF)
        self.assertEqual(len(planned['tasks']),4)

    def test_legacy_retry_approval_also_obeys_the_current_report_protocol(self):
        from auto_moniter.modules import ModuleLedger
        from auto_moniter.workspace import workspace_fingerprint
        from auto_moniter.verdict import parse_verdict
        monitor=self.monitor(self.workers(),self.reviewers())
        monitor.modules=ModuleLedger(MODULES)
        monitor.function_tasks.current=self.final
        tree=workspace_fingerprint(self.root,ignore_dir=self.root/'run')
        raw=conclusion('F2','accepted',['B'],final=True,file='next.py')['verdict']
        review=parse_verdict(json.dumps(raw),function_gate=True)
        record=RoundRecord(2,'reviewer',review,0,False,0,'','',tree=tree,watcher_result={'check_retry':{
            'tree':tree,'task':self.final,'provider_config':monitor._provider_config(),
            'reviewer_model':monitor._reviewer_model(),'direction_ids':[],'review':raw}})
        self.assertIsNone(monitor._check_retry_review(record))
        record.watcher_result['check_retry']['review']={**raw,'module_ids':['A','B']}
        self.assertIsNotNone(monitor._check_retry_review(record))

    def test_cached_old_protocol_review_is_refreshed_without_repeating_worker_or_checks(self):
        from auto_moniter.verdict import parse_verdict
        worker,reviewer=self.workers(),self.reviewers()
        monitor=self.monitor(worker,reviewer)
        invoke=monitor._invoke_review
        def cached(provider,prompt,session_id,**kwargs):
            if kwargs['index']==2:
                record=next(r for r in reversed(monitor._records) if r.role=='worker' and r.index==2)
                receipt=record.watcher_result['candidate_checks']
                old=parse_verdict(json.dumps(conclusion('F2','accepted',['B'],final=True,file='next.py')['verdict']),function_gate=True)
                monitor._records.append(RoundRecord(2,'reviewer',old,0,False,0,'','',tree=record.tree,
                    watcher_result={'candidate_approval':{'checks_digest':receipt['digest'],
                        'context':receipt['context'],'review':old.to_dict()}}))
            return invoke(provider,prompt,session_id,**kwargs)
        with patch.object(monitor,'_invoke_review',side_effect=cached):
            report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,3))
        events=[json.loads(line) for line in (self.root/'run/events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e['event']=='check_finished' for e in events),2)

    def test_saved_approval_file_is_checked_against_current_module_protocol(self):
        from tests.test_candidate_handoff import CheckedReviewer
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=fixture@local',
                        'commit','--allow-empty','-qm','baseline'],check=True)
        worker=self.workers()
        reviewer=CheckedReviewer(self.root,self.reviewers().scripts)
        monitor=self.monitor(worker,reviewer)
        monitor.config.git_history='required'
        invoke=monitor._invoke_review
        def cached(provider,prompt,session_id,**kwargs):
            if kwargs['index']==2:
                record=next(r for r in reversed(monitor._records) if r.role=='worker' and r.index==2)
                (self.root/'run/approval-ready.json').write_text(json.dumps({
                    'round':2,'tree':record.tree,'candidate_tree':monitor.git_history.candidate['commit'],
                    'business_head':monitor.git_history.business_head,'provider_config':monitor._provider_config(),
                    'reviewer_model':monitor._reviewer_model(),
                    'checks_digest':record.watcher_result['candidate_checks']['digest'],
                    'review':conclusion('F2','accepted',['B'],final=True,file='next.py')['verdict']}))
            return invoke(provider,prompt,session_id,**kwargs)
        with patch.object(monitor,'_invoke_review',side_effect=cached):
            report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,3))
        events=(self.root/'run/events.jsonl').read_text()
        self.assertNotIn('approval_review_reused',events)

    def test_dependent_module_milestone_is_rejected_before_its_worker_runs(self):
        milestone = copy.deepcopy(self.final)
        milestone['delivery_scope'] = 'module'
        worker = FunctionWorker('worker',[{**conclusion('F1','done',['A']),'touch':'app.py'}])
        reviewer = ScriptedProvider('reviewer',[self.planning(),
            conclusion('F1','accepted',[],next_task=milestone)])
        monitor = self.monitor(worker,reviewer)
        control = monitor._process_control
        def stop_after_plan_rejection(active=False):
            if monitor._planning_rejection:
                monitor.request_stop()
            return control(active)
        monitor._process_control = stop_after_plan_rejection
        report = monitor.run()
        self.assertEqual(report.status,'stopped',report.summary)
        self.assertEqual(len(worker.calls),1)
        self.assertIn('prerequisite module acceptance',monitor._planning_rejection['reason'])
        self.assertFalse((self.root/'next.py').exists())

    def test_approval_of_old_module_assignment_does_not_trigger_a_second_review(self):
        milestone = copy.deepcopy(self.final)
        milestone['delivery_scope'] = 'module'
        work = conclusion('F2','done',['B'],final=True,file='next.py')
        review = conclusion('F2','accepted',['B'],final=True,file='next.py')
        work['verdict']['scope'] = review['verdict']['scope'] = 'module'
        worker = FunctionWorker('worker',[{**conclusion('F1','done',['A']),'touch':'app.py'},
                                          {**work,'touch':'next.py'}])
        reviewer = ScriptedProvider('reviewer',[self.planning(),
            conclusion('F1','accepted',[],next_task=milestone),review])
        monitor = self.monitor(worker,reviewer)
        validate = monitor._validate_function_plan
        def old_dispatch(ledger,modules,brief,**kwargs):
            if ledger.current['delivery_scope']=='module':
                # Simulate the old validator: check the function and criteria,
                # without the new prerequisite-module dispatch guard.
                from auto_moniter import function_gate
                function_gate.validate_task(ledger.current,monitor.config.cwd,modules,monitor.config.checks,brief)
                self.assertEqual(function_gate.coverage(ledger,modules,ledger.current,brief,monitor.config.cwd,
                                                       provisional=kwargs.get('provisional')),[])
                return
            return validate(ledger,modules,brief,**kwargs)
        with patch.object(monitor,'_validate_function_plan',side_effect=old_dispatch):
            report = monitor.run()
        self.assertEqual(report.status,'max_rounds',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(2,3))
        self.assertTrue((self.root/'next.py').exists())
        self.assertEqual([m['status'] for m in monitor.modules.items.values()],['pending','accepted'])
        last = next(r for r in reversed(report.records) if r.role=='reviewer')
        self.assertEqual(last.delivery['status'],'accepted')

    def test_module_dependency_validation_allows_a_provisionally_accepted_module(self):
        from auto_moniter.function_tasks import FunctionTasks
        from auto_moniter.modules import ModuleLedger
        from tests.test_review_first import BRIEF
        monitor = self.monitor(self.workers(),self.reviewers())
        modules = ModuleLedger(MODULES)
        ledger = FunctionTasks()
        ledger.current = copy.deepcopy(self.final)
        ledger.current['delivery_scope']='module'
        ledger.current['kind']='verification'
        (self.root/'next.py').write_text('def parse_input():\n    return 42\n')
        provisional = {**self.first,'delivery_scope':'module'}
        monitor._validate_function_plan(ledger,modules.items,BRIEF,provisional=provisional)
        with self.assertRaisesRegex(ValueError,'prerequisite module acceptance'):
            monitor._validate_function_plan(ledger,modules.items,BRIEF,provisional=self.first)
        ledger.current.pop('kind')
        with self.assertRaisesRegex(ValueError,'prerequisite module acceptance'):
            monitor._validate_function_plan(ledger,modules.items,BRIEF,provisional=provisional)

    def test_large_final_criterion_sets_have_matching_schema_and_validation_limits(self):
        from auto_moniter import function_tasks
        from auto_moniter.verdict import VERDICT_SCHEMA
        value=copy.deepcopy(self.final)
        value['criterion_ids']=['B:'+str(index) for index in range(1,45)]
        self.assertEqual(len(function_tasks.validate(value)['criterion_ids']),44)
        schema=function_tasks.schema(VERDICT_SCHEMA,gate=True,integration=True)
        for variant in schema['properties']['next_task']['anyOf'][1:]:
            for key,limit in function_tasks.GATE_LIST_LIMITS.items():
                self.assertEqual(variant['properties'][key]['maxItems'],limit)
        for key,limit in function_tasks.GATE_LIST_LIMITS.items():
            invalid=copy.deepcopy(value);invalid[key]=[f'item-{index}' for index in range(limit+1)]
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'Invalid function assignment '+key):
                function_tasks.validate(invalid)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = assignment('F1', 'app.py', 'A')
        self.final = assignment('F2', 'next.py', 'B', final=True)

    def planning(self):
        value = plan(self.first)
        brief = json.loads(value['text'])
        brief['modules'] = copy.deepcopy(MODULES)
        value['text'] = json.dumps(brief)
        return value

    def workers(self, final_ids=None):
        return FunctionWorker('worker', [
            {**conclusion('F1', 'done', ['A']), 'touch':'app.py'},
            {**conclusion('F2', 'done', ['A','B'] if final_ids is None else final_ids,
                          final=True, file='next.py'), 'touch':'next.py'},
        ])

    def reviewers(self, final_ids=None):
        return ScriptedProvider('reviewer', [self.planning(),
            conclusion('F1', 'accepted', [], next_task=self.final),
            conclusion('F2', 'accepted', ['A','B'] if final_ids is None else final_ids,
                       final=True, file='next.py'),
        ])

    def monitor(self, worker, reviewer, **options):
        return Supervisor(RunConfig('Build both modules', self.root, worker, reviewer=reviewer,
            workflow='review-first', function_dispatch=True, git_history='off', run_dir=self.root/'run',
            max_rounds=2, retry_delay_s=0, checks=[CHECK], **options), log=lambda _:None)

    def test_final_assignment_accepts_pending_prerequisite_modules_with_verified_function_coverage(self):
        worker, reviewer = self.workers(), self.reviewers()
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (2,3))
        saved = json.loads((self.root/'run/state.json').read_text())
        self.assertEqual([item['status'] for item in saved['modules']], ['accepted','accepted'])
        self.assertEqual([item['task']['id'] for item in saved['function_tasks']['accepted']], ['F1','F2'])
        for prompt in (worker.calls[-1]['prompt'], reviewer.calls[-1]['prompt']):
            self.assertIn('module_ids=["A", "B"]', prompt)
            self.assertIn('not file edit permissions', prompt)

    def test_legacy_final_report_recovers_read_only_without_repeating_implementation(self):
        worker, reviewer = self.workers(final_ids=['B']), self.reviewers(final_ids=['B'])
        reviewer.scripts.append(conclusion('F2', 'accepted', ['B'], final=True, file='next.py'))
        old_monitor = self.monitor(worker, reviewer)
        # Reproduce the old current-module-only final report policy.
        with patch.object(old_monitor, '_validate_function_worker', return_value=[]):
            stopped = old_monitor.run()
        self.assertEqual(stopped.status, 'action_required', stopped.summary)
        self.assertIn('function handoff', stopped.summary.lower())
        files = {name:(self.root/name).read_bytes() for name in ('app.py','next.py')}
        repair = FunctionWorker('worker', [conclusion('F2', 'done', ['A','B'], final=True, file='next.py')])
        review = ScriptedProvider('reviewer', [conclusion('F2', 'accepted', ['A','B'], final=True, file='next.py')])
        resumed = self.monitor(repair, review, resume=True).run()
        self.assertEqual(resumed.status, 'done', resumed.summary)
        self.assertEqual((len(repair.calls), len(review.calls)), (1,1))
        self.assertTrue(repair.calls[0]['read_only'])
        self.assertIn('Worker report correction only', repair.calls[0]['prompt'])
        for name, expected in files.items():
            self.assertEqual((self.root/name).read_bytes(), expected)
        originals = list((self.root/'run').glob('002-worker-report-original-*-text.md'))
        self.assertEqual(len(originals), 1)
        self.assertEqual(json.loads(originals[0].read_text())['module_ids'], ['B'])

    def test_unknown_or_omitted_modules_do_not_validate_final_worker_completion(self):
        monitor = self.monitor(self.workers(), self.reviewers())
        from auto_moniter.function_tasks import FunctionTasks
        from auto_moniter.modules import ModuleLedger
        from auto_moniter.verdict import parse_verdict
        monitor.modules = ModuleLedger(MODULES)
        monitor.function_tasks = FunctionTasks()
        monitor.function_tasks.current = self.final
        for ids in (['B'], ['A'], ['A','B','unknown'], ['A','B','B']):
            with self.subTest(ids=ids):
                value = parse_verdict(json.dumps(conclusion('F2','done',ids,final=True)['verdict']),
                                      function_gate=True, worker_submission=True)
                self.assertTrue(monitor._validate_function_worker(value))
                self.assertEqual(value.task_status, 'incomplete')

    def test_missing_final_review_modules_get_one_read_only_correction(self):
        worker, reviewer = self.workers(), self.reviewers(final_ids=['B'])
        reviewer.scripts.append(conclusion('F2','accepted',['A','B'],final=True,file='next.py'))
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,4))
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertIn('Reviewer-only repair of the SAME candidate', reviewer.calls[-1]['prompt'])
        self.assertIn('module_ids=["A", "B"]', reviewer.calls[-1]['prompt'])
        self.assertEqual(len(list((self.root/'run').glob('002-reviewer-repair-*-text.md'))), 1)

    def test_reviewer_task_id_typo_is_repaired_without_another_worker(self):
        worker, reviewer = self.workers(), self.reviewers()
        correct = copy.deepcopy(reviewer.scripts[-1])
        reviewer.scripts[-1]['verdict']['task_id'] = 'wrong-task'
        reviewer.scripts.append(correct)
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,4))
        self.assertIn('dispatched function ID', reviewer.calls[-1]['prompt'])

    def test_repeated_invalid_review_stops_after_one_correction_and_preserves_candidate(self):
        worker, reviewer = self.workers(), self.reviewers(final_ids=['B'])
        reviewer.scripts.append(conclusion('F2','accepted',['B'],final=True,file='next.py'))
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertIn('Invalid function handoff report', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,4))
        self.assertIn('return 42', (self.root/'next.py').read_text())
        saved = json.loads((self.root/'run/state.json').read_text())
        self.assertTrue(all(item['status']=='pending' for item in saved['modules']))
        self.assertEqual([x['task']['id'] for x in saved['function_tasks']['accepted']], ['F1'])

    def test_report_correction_honors_the_original_call_budget(self):
        worker, reviewer = self.workers(), self.reviewers(final_ids=['B'])
        report = self.monitor(worker, reviewer, max_agent_calls=5).run()
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (2,3))
        self.assertTrue((self.root/'next.py').exists())

    def test_final_dispatch_still_rejects_uncovered_prerequisite_criterion(self):
        planning = self.planning()
        brief = json.loads(planning['text'])
        brief['modules'][0]['acceptance'].append('Required uncovered failure path')
        planning['text'] = json.dumps(brief)
        worker = FunctionWorker('worker', [{**conclusion('F1','done',['A']), 'touch':'app.py'}])
        reviewer = ScriptedProvider('reviewer', [planning,
            conclusion('F1','accepted',[],next_task=self.final)])
        monitor = self.monitor(worker, reviewer)
        original_control = monitor._process_control
        def stop_after_rejection(active=False):
            if monitor.function_tasks.accepted:
                monitor.request_stop()
            return original_control(active)
        monitor._process_control = stop_after_rejection
        report = monitor.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(len(worker.calls), 1)
        saved = json.loads((self.root/'run/state.json').read_text())
        self.assertIn('A:2', saved['planning_rejection']['reason'])
        self.assertTrue(all(item['status']=='pending' for item in saved['modules']))
