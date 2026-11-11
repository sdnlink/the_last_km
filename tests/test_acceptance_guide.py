import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from console.acceptance import scan,select,preflight
from console.tasks import TaskStore,TaskError
from console.server import ConsoleServer
from auto_moniter.cli import build_parser
from auto_moniter.supervisor import RunConfig,Supervisor
from tests.test_supervisor import ScriptedProvider,_verdict


class AcceptanceGuideTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.project=self.root/'project with space';self.project.mkdir()
  self.env=patch.dict(os.environ,{'AUTO_MONITER_STATE_DIR':str(self.root/'state')});self.env.start()
  self.store=TaskStore(self.root/'console',self.project)
 def tearDown(self):
  for child in list(self.store._children.values()):
   if child.poll() is None:child.terminate();child.wait(timeout=12)
  self.env.stop();self.temp.cleanup()
 def fixture(self):
  (self.project/'tests').mkdir(exist_ok=True)
  (self.project/'tests/test_api.py').write_text("import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self):self.assertEqual(1,1)\n")
 def plan(self,ids=None,kind='code',python=''):
  found=scan(self.project,kind,python)
  return {'task_type':kind,'python':found['python'],'signature':found['signature'],'criteria':'合法与非法输入有清晰结果','selected_ids':ids or []}
 def test_scan_only_reads_files_does_not_execute_test_or_project_config(self):
  self.fixture();(self.project/'tests/test_api.py').write_text("import unittest\nraise RuntimeError('never import during scan')")
  before=set(self.project.rglob('*'))
  with patch('subprocess.Popen',side_effect=AssertionError('scan must not execute')):found=scan(self.project)
  self.assertEqual(set(self.project.rglob('*')),before)
  self.assertIn('python-unittest',[c['id'] for c in found['checks']])
  self.assertTrue(found['unverified']);self.assertIn('语义',found['semantic_review']['title'])
 def test_empty_project_has_no_fake_tests_or_claims(self):
  self.assertEqual(scan(self.project)['checks'],[])
 def test_nested_swift_packages_are_discovered_without_execution(self):
  package=self.project/'Native App';package.mkdir()
  (package/'Package.swift').write_text('// swift-tools-version: 5.10\nimport PackageDescription\n')
  with patch('subprocess.Popen',side_effect=AssertionError('scan must not execute')):
   found=scan(self.project)
  check=next(c for c in found['checks'] if c['id']=='swift-test-1')
  self.assertEqual(__import__('shlex').split(check['command']),['swift','test','--package-path','Native App'])
  self.assertFalse(check['recommended']);self.assertIn('Apple',check['effects'])
  plan=select(self.project,{**self.plan(['swift-test-1']),'signature':found['signature']})
  with patch('shutil.which',return_value=None):result=preflight(plan)
  self.assertEqual(result['checks'][0]['status'],'environment_unavailable')
 def test_python_dependency_preflight_is_not_a_test_run(self):
  self.fixture();plan=select(self.project,self.plan(['python-unittest']))
  result=preflight(plan)
  self.assertTrue(result['ready']);self.assertFalse(result['executed'])
 def test_missing_python_and_module_are_environment_errors(self):
  self.fixture();plan=select(self.project,self.plan(['python-unittest'],python='/no/such/python'))
  self.assertFalse(preflight(plan)['ready'])
  plan=select(self.project,self.plan(['python-unittest']))
  plan['selected'][0]['dependencies']=['auto_moniter_fake_dependency_xyz']
  self.assertIn('缺少依赖',preflight(plan)['checks'][0]['errors'][0])
 def test_changed_entry_or_fake_ids_cannot_inject_commands(self):
  self.fixture();plan=self.plan(['python-unittest']);plan['selected']=[{'command':'rm -rf /'}]
  chosen=select(self.project,plan)
  self.assertNotIn('rm -rf',chosen['selected'][0]['command'])
  with self.assertRaises(ValueError):select(self.project,{**plan,'selected_ids':['fake']})
  (self.project/'tests/test_api.py').write_text('import unittest\n# changed')
  with self.assertRaisesRegex(ValueError,'重新扫描'):select(self.project,plan)
 def test_node_scripts_disclose_hooks_and_missing_dependencies(self):
  (self.project/'package.json').write_text(json.dumps({'scripts':{'test':'node test.js','pretest':'echo hook'},'devDependencies':{'test':'1'}}))
  found=scan(self.project);c=next(c for c in found['checks'] if c['id']=='npm-test')
  self.assertIn('生命周期',c['effects']);self.assertFalse(c['recommended'])
  with patch('shutil.which',return_value='/usr/bin/npm'):
   result=preflight(select(self.project,self.plan(['npm-test'])))
  self.assertFalse(result['ready']);self.assertIn('node_modules',result['checks'][0]['errors'][0])
 def test_guided_selection_persists_purpose_and_actual_cli_acceptance(self):
  self.fixture();p=self.plan(['python-unittest'])
  task=self.store.create({'title':'guided','requirement':'implement','acceptance_plan':p,'verification_mode':'executable'})
  meta=self.store._get(task['id']);args=build_parser().parse_args(self.store._new_args(meta))
  self.assertEqual(args.accept,p['criteria']);self.assertIn('unittest',args.check[0])
  self.assertEqual(task['acceptance_plan']['selected_ids'],['python-unittest'])
  self.assertEqual(task['acceptance_plan']['selected'][0]['title'],'Python 自动化测试')
 def test_no_selection_cannot_be_executable_review_only_is_explicit(self):
  p=self.plan()
  with self.assertRaises(TaskError):self.store.create({'title':'task','requirement':'implement','acceptance_plan':p,'verification_mode':'executable'})
  task=self.store.create({'title':'task','requirement':'review','acceptance_plan':p,'verification_mode':'review_only'})
  self.assertEqual(task['checks'],[])
 def test_integration_scope_requires_a_real_check_in_the_configured_module(self):
  payload={'title':'joint delivery','requirement':'change both ends','verification_mode':'executable',
           'modules':[{'id':'API','title':'Contract','acceptance':['Both ends agree'],'checks':['python verify_contract.py']}]}
  scope={'id':'contract','module_id':'API','check_ids':['API:check:1'],
         'targets':[{'file':'server.py','symbol':'send'},{'file':'client.py','symbol':'receive'}]}
  task=self.store.create({**payload,'quality_policy':{'integration_scopes':[scope]}})
  self.assertTrue(task['function_dispatch'])
  self.assertEqual(task['quality_policy']['integration_scopes'],[scope])
  with self.assertRaisesRegex(TaskError,'configured checks'):
   self.store.create({**payload,'quality_policy':{'integration_scopes':[{**scope,'check_ids':['API:check:2']}]}})
 def test_planning_task_does_not_claim_original_functionality(self):
  task=self.store.create({'title':'方案','requirement':'实现接口','acceptance_plan':self.plan(),'verification_mode':'planning'})
  self.assertIn('仅制定可执行的验收方案',task['requirement']);self.assertEqual(task['checks'],[])
 def test_scan_refuses_external_symlinked_script(self):
  (self.project/'scripts').mkdir();outside=self.root/'outside.py';outside.write_text('print(1)')
  (self.project/'scripts/check_docs.py').symlink_to(outside)
  self.assertNotIn('check_docs.py',[c['id'] for c in scan(self.project)['checks']])
 def test_runtime_missing_dependency_stops_as_environment_unavailable(self):
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  check=__import__('shlex').join([sys.executable,'-c','import auto_moniter_fake_dependency_xyz'])
  result=Supervisor(RunConfig('task',self.project,worker,no_review=True,run_dir=self.project/'run',checks=[check],git_history='off',retry_delay_s=0,max_rounds=3),log=lambda _:None).run()
  self.assertEqual(result.status,'action_required');self.assertEqual(len(worker.calls),1)
  evidence=json.loads((self.project/'run/acceptance-results.json').read_text())
  self.assertEqual(evidence['checks'][0]['status'],'environment_unavailable');self.assertFalse(evidence['complete'])
 def test_runtime_assertion_failure_is_not_environment_error(self):
  from auto_moniter.verification import outcome
  def result(**kw):return SimpleNamespace(timed_out=False,interruption='',exit_code=1,stdout='',stderr='AssertionError',**kw)
  self.assertEqual(outcome(result()),'check_failed')
 def test_authenticated_scan_api_and_no_task_mutation(self):
  self.fixture()
  with ConsoleServer(('127.0.0.1',0),self.store) as server:
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();port=server.server_address[1]
   def request(method,path,data=None,token=None):
    conn=HTTPConnection('127.0.0.1',port,timeout=5);headers={'Content-Type':'application/json'}
    if token:headers['X-Console-Token']=token
    conn.request(method,path,json.dumps(data) if data is not None else None,headers)
    response=conn.getresponse();value=response.read();status=response.status;conn.close();return status,json.loads(value)
   try:
    token=request('GET','/api/config')[1]['csrf_token'];payload={'cwd':str(self.project),'task_type':'code'}
    self.assertEqual(request('POST','/api/acceptance/scan',payload)[0],403)
    status,body=request('POST','/api/acceptance/scan',payload,token);self.assertEqual(status,200)
    p={**self.plan(['python-unittest']),'signature':body['signature']}
    status,body=request('POST','/api/acceptance/preflight',{'cwd':str(self.project),'acceptance_plan':p},token)
    self.assertEqual(status,200);self.assertFalse(body['executed'])
    self.assertEqual(self.store.task_summaries()['tasks'],[])
   finally:server.shutdown();thread.join(timeout=5)
 def test_preflight_before_criteria_is_allowed_but_task_creation_still_requires_it(self):
  self.fixture();p={**self.plan(['python-unittest']),'criteria':''}
  self.assertTrue(self.store.acceptance_options({'cwd':str(self.project),'acceptance_plan':p},prepare=True)['ready'])
  with self.assertRaises(TaskError):self.store.create({'title':'task','requirement':'implement','acceptance_plan':p})
 def test_npm_invalid_json_object_is_not_an_api_crash(self):
  (self.project/'package.json').write_text('[]')
  self.assertEqual(scan(self.project)['checks'],[])
 def test_selected_guide_is_rechecked_before_start_not_silently_replaced(self):
  self.fixture();task=self.store.create({'title':'queued','requirement':'implement','verification_mode':'executable','acceptance_plan':self.plan(['python-unittest'])})
  (self.project/'tests/test_api.py').write_text('import unittest\n# changed baseline')
  with patch('console.tasks.discover_providers',return_value={'codex':True,'claude':True}),self.assertRaisesRegex(TaskError,'重新扫描'):
   self.store._check_start(self.store._get(task['id']))
 def test_planning_mode_cannot_run_unselected_custom_commands(self):
  task=self.store.create({'title':'plan','requirement':'implement','verification_mode':'planning','checks':['false'],'acceptance_plan':self.plan()})
  self.assertEqual(task['checks'],[])
 def test_guided_task_runs_actual_cli_and_persists_check_snapshot(self):
  import subprocess,time
  self.fixture();subprocess.run(['git','init','-q',str(self.project)],check=True)
  binaries=self.project/'bin';binaries.mkdir();agent=binaries/'codex'
  verdict=_verdict('done',evidence=['app.py'])
  modules=verdict['modules']
  verdict.update(modules=[],task_id='fixture-function',next_task=None)
  from tests.test_review_first import BRIEF
  brief={**BRIEF,'modules':modules,'next_task':{
      'id':'fixture-function','module_id':'main','file':'app.py','symbol':'parse_input',
      'objective':'Handle input','inputs':'value','outputs':'value','boundaries':['Only app.py'],
      'verification':['The selected unittest passes'],'depends_on':[],'delivery_scope':'requirement',
      'criterion_ids':['main:1','req:1:1','req:1:2'],'check_ids':['check:1'],'test_files':[]}}
  agent.write_text('#!'+sys.executable+'\nimport sys,json,re\nfrom pathlib import Path\nprompt=sys.stdin.read()\nif any("requirements.schema.json" in a for a in sys.argv):\n print(json.dumps('+repr(brief)+'));sys.exit(0)\nv='+repr(verdict)+'\nif "read-only" not in sys.argv:\n Path("app.py").write_text("def parse_input(value):\\n    return value\\n")\n v["task_status"]="done"\nelse:\n v["task_status"]="accepted"\n m=re.search(r"Worker checkpoint: ([0-9a-f]{40,64})",prompt)\n if m:v["evidence"].append(m[1])\nprint(json.dumps(v))\n')
  agent.chmod(0o700)
  with patch.dict(os.environ,{'PATH':str(binaries)+os.pathsep+os.environ['PATH']}):
   task=self.store.create({'title':'guided real CLI','requirement':'implement fixture','provider':'codex','reviewer':'codex',
          'verification_mode':'executable','acceptance_plan':self.plan(['python-unittest']),
          'review_isolation':'provider','auto_start':True})
   deadline=time.monotonic()+15;detail=task
   while time.monotonic()<deadline:
    detail=self.store.detail(task['id'])
    if detail['status'] not in ('queued','starting','running'):break
    time.sleep(.05)
   self.assertEqual(detail['status'],'done',detail.get('summary'))
   self.assertTrue(detail['acceptance_results']['complete'])
   self.assertEqual(detail['function_tasks']['accepted'][0]['verification'], {'check:1':'001-check-01.log'})
   self.assertEqual(detail['acceptance_results']['checks'][0]['status'],'passed')
   run=Path(task['run_dir']);self.assertTrue((run/'acceptance-plan.json').exists())
   self.assertEqual(json.loads((run/'state.json').read_text())['acceptance'],'合法与非法输入有清晰结果')
   self.assertIn('001-check-01.log',self.store.logs(task['id'])['files'])
 def test_planning_discards_selected_tests_and_frames_standards_as_plan_requirements(self):
  self.fixture();task=self.store.create({'title':'plan','requirement':'implement','verification_mode':'planning','checks':['false'],'acceptance_plan':self.plan(['python-unittest'])})
  meta=self.store._get(task['id'])
  self.assertEqual(meta['acceptance_plan']['selected_ids'],[])
  self.assertEqual(meta['checks'],[])
  self.assertIn('不要求在本任务实现',meta['acceptance'])
 def test_invalid_task_type_returns_validation_error(self):
  with self.assertRaises(ValueError):scan(self.project,[])
