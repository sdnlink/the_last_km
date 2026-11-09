import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from auto_moniter.quality import Quality, policy
from auto_moniter.metering import UsageMeter
from auto_moniter.modules import ModuleLedger
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.providers import PreparedCommand
from auto_moniter.verdict import Verdict
from auto_moniter.workspace import file_snapshot
from tests.test_supervisor import ScriptedProvider, _verdict

PLAN = _verdict('done')['modules']

class QualityPolicyTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
 def tearDown(self): self.temp.cleanup()
 def run_loop(self,worker,reviewer=None,**options):
  return Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,no_review=reviewer is None,
   git_history='off',run_dir=self.root/'run',retry_delay_s=0,modules=PLAN,**options),log=lambda _:None).run()
 def test_requirement_mapping_and_both_cited_file_evidence_gate_delivery(self):
  req={'id':'R1','text':'implement API','module_ids':['main'],'evidence_paths':['missing.py']}
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('done',evidence=['app.py'])}])
  result=self.run_loop(worker,reviewer,quality_policy={'requirements':[req]},max_rounds=1)
  self.assertEqual(result.status,'max_rounds')
  rows=json.loads((self.root/'run/requirement-coverage.json').read_text())
  self.assertFalse(rows['complete']);self.assertFalse(rows['requirements'][0]['covered'])
 def test_valid_requirement_writes_coverage_for_exact_delivery_tree(self):
  req={'id':'R1','text':'implement API','module_ids':['main'],'evidence_paths':['app.py']}
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('done',evidence=['app.py'])}])
  result=self.run_loop(worker,reviewer,quality_policy={'requirements':[req]},max_rounds=1)
  self.assertEqual(result.status,'done')
  rows=json.loads((self.root/'run/requirement-coverage.json').read_text());report=json.loads((self.root/'run/report.json').read_text())
  self.assertTrue(rows['complete']);self.assertEqual(rows['tree'],report['accepted_tree'])
 def test_existing_test_rewrite_stops_before_review_new_tests_are_allowed(self):
  (self.root/'tests').mkdir();(self.root/'tests/test_api.py').write_text('assert False')
  baseline=file_snapshot(self.root);q=Quality({'protect_tests':True},baseline)
  (self.root/'tests/test_new.py').write_text('assert True')
  self.assertEqual(q.baseline_errors(self.root),[])
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['tests/test_api.py']),'touch':'tests/test_api.py'}])
  reviewer=ScriptedProvider('reviewer',[])
  result=self.run_loop(worker,reviewer,quality_policy={'protect_tests':True},max_rounds=1)
  self.assertEqual(result.status,'action_required');self.assertEqual(reviewer.calls,[])
 def test_cosmetic_edits_do_not_reset_verified_progress(self):
  worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['unfinished'])} for _ in range(5)])
  config=RunConfig('task',self.root,worker,no_review=True,git_history='off',run_dir=self.root/'run',
   max_rounds=5,retry_delay_s=0,quality_policy={'no_progress_limit':2,'review_interval_s':0},modules=PLAN)
  def log(message):
   if 'worker running' in message:(self.root/'format.txt').write_text(message)
  result=Supervisor(config,log=log).run()
  self.assertEqual(result.status,'action_required');self.assertLess(result.rounds,5)
  self.assertIn('Verified progress',result.summary)
 def test_repeated_check_and_reopened_issue_do_not_farm_progress(self):
  q=Quality({'verified_progress':True},{})
  self.assertTrue(q.check_result('test',True));self.assertFalse(q.check_result('test',True))
  self.assertFalse(q.check_result('test',False));self.assertFalse(q.check_result('test',True))
  self.assertTrue(q.milestone('audit:AUD-1'));self.assertFalse(q.milestone('audit:AUD-1'))
 def test_scoped_invalidation_propagates_dependencies_and_unknown_files_invalidate_all(self):
  plan=[{'id':'A','title':'A','acceptance':['ok'],'files':['a.py']},
        {'id':'B','title':'B','acceptance':['ok'],'files':['b.py'],'depends_on':['A']},
        {'id':'C','title':'C','acceptance':['ok'],'files':['c.py']}]
  ledger=ModuleLedger(plan)
  for m in ledger.items.values():m.update(status='accepted',tree='old')
  self.assertEqual(set(ledger.invalidate('new',['a.py'])),{'A','B'})
  self.assertEqual(ledger.items['C']['status'],'accepted');self.assertEqual(ledger.items['C']['tree'],'new')
  restored=ModuleLedger.restore(ledger.export());self.assertEqual(restored.items['C']['files'],['c.py'])
  self.assertEqual(ledger.invalidate('newer',['shared.cfg']),['C'])
 def test_repeated_independently_confirmed_blocker_suspends_calls(self):
  worker=ScriptedProvider('worker',[{'verdict':{**_verdict('continue',gaps=['dependency']), 'blockers':['database unavailable']}} for _ in range(4)])
  reviewer=ScriptedProvider('reviewer',[{'verdict':{**_verdict('continue',gaps=['dependency']), 'blockers':['database unavailable']}} for _ in range(4)])
  result=self.run_loop(worker,reviewer,quality_policy={'blocked_review_limit':2},max_rounds=4)
  self.assertEqual(result.status,'action_required');self.assertEqual(len(worker.calls),2)
  self.assertTrue((self.root/'run/dependency-block.json').exists())
 def test_quality_policy_cannot_be_weakened_on_resume(self):
  worker=ScriptedProvider('worker',[])
  s=Supervisor(RunConfig('task',self.root,worker,no_review=True,git_history='off',run_dir=self.root/'run',quality_policy={'protect_tests':True}),log=lambda _:None)
  s.request_stop();s.run()
  s=Supervisor(RunConfig('task',self.root,worker,no_review=True,git_history='off',run_dir=self.root/'run',resume=True,quality_policy={'protect_tests':False}),log=lambda _:None)
  self.assertEqual(s.run().status,'invalid')
 def test_invalid_requirement_mapping_is_rejected_before_call(self):
  result=self.run_loop(ScriptedProvider('worker',[]),quality_policy={'requirements':[{'id':'R1','text':'x','module_ids':['missing'],'evidence_paths':['app.py']}]})
  self.assertEqual(result.status,'invalid')
 def test_meter_ignores_conclusion_claims_and_does_not_double_count_cached_tokens(self):
  meter=UsageMeter()
  output='\n'.join([json.dumps({'status':'done','tokens':100000,'cost':1}),json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10}})])
  meter.record(output);self.assertEqual(meter.state['tokens'],110);self.assertFalse(meter.last_cost_reported)
  meter.record(json.dumps({'type':'result','usage':{'input_tokens':5,'output_tokens':3,'cache_read_input_tokens':2},'total_cost_usd':0.2}))
  self.assertEqual(meter.state['tokens'],120);self.assertTrue(meter.exhausted({'max_cost_usd':0.1}))
 def test_required_meter_without_provider_report_stops(self):
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  result=self.run_loop(worker,quality_policy={'max_tokens':1000},max_rounds=2)
  self.assertEqual(result.status,'budget_exceeded');self.assertIn('did not report',result.summary)
 def test_timeout_checkpoint_is_audited_even_when_worker_has_no_final_conclusion(self):
  class Slow(ScriptedProvider):
   def _command(self,*args):
    return PreparedCommand([sys.executable,'-c',"from pathlib import Path;import time;Path('app.py').write_text('partial');time.sleep(30)"])
  reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('continue',gaps=['partial needs correction'])}])
  result=self.run_loop(Slow('worker',[]),reviewer,quality_policy={'review_interval_s':1},max_rounds=1)
  self.assertEqual(len(reviewer.calls),1);self.assertNotEqual(result.status,'done')
 def test_check_script_is_protected_even_outside_test_directories(self):
  (self.root/'scripts').mkdir();script=self.root/'scripts/check_api.py';script.write_text('assert False')
  q=Quality({'protect_tests':True},file_snapshot(self.root));q.bind_checks(self.root,['python scripts/check_api.py'])
  script.write_text('assert True')
  self.assertEqual(q.baseline_errors(self.root),['scripts/check_api.py'])
 def test_module_only_edit_preserves_independent_acceptance_but_final_checks_still_run(self):
  from tests.test_modules import outcome
  plan=[{'id':'A','title':'A','acceptance':['A works'],'files':['a.py']},
        {'id':'B','title':'B','acceptance':['B works'],'files':['b.py']}]
  def done(ids,scope):
   v=_verdict('done',evidence=[('a.py' if ids==['A'] else 'b.py')],scope=scope)
   v['module_ids']=ids;v['modules']=[];return v
  worker=ScriptedProvider('worker',[{'verdict':done(['A'],'module'),'touch':'a.py'},
                                   {'verdict':done(['B'],'requirement'),'touch':'b.py'}])
  reviewer=ScriptedProvider('reviewer',[{'verdict':done(['A'],'module')},{'verdict':done(['B'],'requirement')}])
  result=Supervisor(RunConfig('A and B',self.root,worker,reviewer=reviewer,modules=plan,
                   quality_policy={'verified_progress':True},checks=['echo integration-check'],
                   run_dir=self.root/'run',git_history='off',retry_delay_s=0,max_rounds=2),log=lambda _:None).run()
  self.assertEqual(result.status,'done');self.assertEqual(result.rounds,2)
  self.assertIn('integration-check',(self.root/'run/002-check-01.log').read_text())
 def test_files_only_reviewer_cannot_be_selected_without_strict_boundary(self):
  from auto_moniter.preflight import validate
  from auto_moniter.providers import resolve_provider
  config=RunConfig('task',self.root,resolve_provider('claude'),reviewer=resolve_provider('claude'),
                   quality_policy={'review_tools':'files_only'},review_isolation='provider')
  with self.assertRaisesRegex(ValueError,'requires strict'):validate(config)
 def test_changed_file_threshold_forces_audit_before_normal_round_frequency(self):
  worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['unfinished']),'touch':'app.py'}])
  reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('continue',gaps=['needs more work'])}])
  self.run_loop(worker,reviewer,quality_policy={'review_changed_files':1},review_every=100,max_rounds=1)
  self.assertEqual(len(reviewer.calls),1)
 def test_resumed_policy_is_inherited_and_baseline_change_does_not_launch_worker(self):
  (self.root/'tests').mkdir();test=self.root/'tests/test_api.py';test.write_text('assert False')
  s=Supervisor(RunConfig('task',self.root,ScriptedProvider('worker',[]),no_review=True,
        git_history='off',run_dir=self.root/'run',quality_policy={'protect_tests':True}),log=lambda _:None)
  s.request_stop();s.run();test.write_text('assert True')
  worker=ScriptedProvider('worker',[])
  result=Supervisor(RunConfig('task',self.root,worker,no_review=True,git_history='off',run_dir=self.root/'run',resume=True),log=lambda _:None).run()
  self.assertEqual(result.status,'action_required');self.assertEqual(worker.calls,[])
 def test_files_only_disables_mcp_hooks_shell_and_deployment_credentials(self):
  import os
  from auto_moniter.selftest import FixtureProvider
  worker,reviewer=FixtureProvider(),FixtureProvider(True);reviewer.name='claude'
  seen=[]
  def wrap(boundary,command,provider):seen.append(command.argv);return command
  # This checks tool flags and credential scrubbing, not the host mount probe.
  with patch('auto_moniter.isolation.ReviewIsolation.__init__',return_value=None),patch('auto_moniter.isolation.ReviewIsolation.verify'),patch('auto_moniter.isolation.ReviewIsolation.wrap',wrap),patch.dict(os.environ,{'AWS_SECRET_ACCESS_KEY':'fake-fixture'}):
   result=Supervisor(RunConfig('fixture',self.root,worker,reviewer=reviewer,
    modules=[{'id':'fixture','title':'Fixture','acceptance':['content is ok']}],quality_policy={'review_tools':'files_only'},
    review_isolation='strict',run_dir=self.root/'run',git_history='off',max_rounds=2,retry_delay_s=0),log=lambda _:None).run()
  self.assertEqual(result.status,'done');self.assertEqual(len(seen),2)
  for argv in seen:
   for flag in ['--restricted','--safe-mode','--strict-mcp-config','--mcp-config','Read,Glob,Grep','AWS_SECRET_ACCESS_KEY']:self.assertIn(flag,argv)
   self.assertEqual(argv[0],'env');self.assertIn('-u',argv)
 def test_strict_worker_mounts_original_test_files_without_blocking_new_tests(self):
  import shutil
  from auto_moniter.isolation import WorkerIsolation
  from auto_moniter.storage import write_json
  if not shutil.which('bwrap'):self.skipTest('bwrap unavailable')
  project=self.root/'project';project.mkdir();tests=project/'tests';tests.mkdir()
  test=tests/'test_api.py';test.write_text('assert False')
  run=self.root/'run';run.mkdir()
  write_json(run/'state.json',{'acceptance_baseline_paths':[str(test)]})
  boundary=WorkerIsolation(project,run)
  code="from pathlib import Path;p=Path('tests/test_api.py');Path('tests/test_new.py').write_text('new');p.write_text('weakened')"
  import subprocess
  result=subprocess.run(boundary.wrap(PreparedCommand([sys.executable,'-c',code]),'codex').argv,capture_output=True,text=True)
  self.assertNotEqual(result.returncode,0);self.assertEqual(test.read_text(),'assert False')
  self.assertTrue((tests/'test_new.py').exists());self.assertIn('Read-only file system',result.stderr)
 def test_web_quality_policy_is_normalized_and_passed_to_cli(self):
  from console.tasks import TaskStore, TaskError
  from auto_moniter.cli import build_parser
  store=TaskStore(self.root/'catalog',self.root)
  request={'title':'task','requirement':'implement API','provider':'codex','reviewer':'codex',
           'modules':PLAN,'quality_policy':{'verified_progress':True,'requirements':[{'id':'R1','text':'API','module_ids':['main'],'evidence_paths':['app.py']}]}}
  task=store.create(request);meta=store._get(task['id']);args=build_parser().parse_args(store._new_args(meta))
  self.assertEqual(args.quality_policy,Path(meta['run_dir'])/'quality-policy.json')
  self.assertEqual(task['quality_policy']['requirements'][0]['id'],'R1')
  with self.assertRaises(TaskError):store.create({**request,'reviewer':'codex','quality_policy':{'review_tools':'files_only'}})
 def test_missing_cost_is_not_treated_as_zero_and_survives_checkpoint(self):
  meter=UsageMeter();meter.record(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':2}}))
  restored=UsageMeter(json.loads(json.dumps(meter.state)))
  self.assertEqual(restored.state['unknown_cost_turns'],1)
  self.assertEqual(restored.state['unknown_token_turns'],0)
 def test_reaccepting_same_module_does_not_reset_verified_progress(self):
  worker=ScriptedProvider('worker',[])
  supervisor=Supervisor(RunConfig('task',self.root,worker,no_review=True,git_history='off',run_dir=self.root/'run'),log=lambda _:None)
  supervisor.quality=Quality({'verified_progress':True},{})
  supervisor.quality.milestone('module:main');supervisor._no_progress=5
  from auto_moniter.workspace import workspace_fingerprint
  supervisor._delivery_tree=workspace_fingerprint(self.root,ignore_dir=self.root/'run')
  with patch.object(supervisor,'_event'),patch.object(supervisor,'_close_delivered_sessions',return_value=[]):
   supervisor._complete_module(Verdict('done',scope='module',module_ids=['main']))
  self.assertEqual(supervisor._no_progress,5);self.assertFalse(supervisor._progress_reset)
 def test_requirement_evidence_does_not_accept_filename_substrings(self):
  from auto_moniter.quality import cited
  self.assertFalse(cited(['app.py'],['not_app.py','other/app.py']))
  self.assertTrue(cited(['app.py'],['checked `app.py:12`']))
