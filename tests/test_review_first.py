import json,re,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from auto_moniter.supervisor import RunConfig,Supervisor
from auto_moniter.requirements_review import parse
from auto_moniter.approved_history import ApprovedHistory
from tests.test_supervisor import ScriptedProvider,_verdict

BRIEF={'status':'ready','summary':'Requirement clarified','refined_requirement':'Implement original behavior and reject invalid inputs.',
       'acceptance_criteria':['valid input succeeds','invalid input rejected'],'constraints':['Preserve original scope'],
       'assumptions':['Use existing project conventions'],'blockers':[]}

def approve_requirement_coverage():
 return {'verdict':{},'text':json.dumps({'status':'pass','summary':'Source requirements covered','findings':[]})}

class ReviewFirstTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def brief(self,value=None):return {'verdict':{},'text':json.dumps(value or BRIEF)}
 def run_loop(self,worker,reviewer,*,requirement='original functional request',**kwargs):
  return Supervisor(RunConfig(requirement,self.root,worker,reviewer=reviewer,
               workflow='review-first',run_dir=self.root/'run',git_history='off',retry_delay_s=0,max_rounds=3,**kwargs),log=lambda _:None).run()
 def test_requirement_review_precedes_implementation_and_rejection_returns_to_worker(self):
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'},
                                   {'verdict':_verdict('done',evidence=['app.py'])}])
  reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':_verdict('continue',['missing negative path'])},
                                      {'verdict':_verdict('done',evidence=['app.py'],closed=['AUD-1'])}])
  result=self.run_loop(worker,reviewer)
  self.assertEqual(result.status,'done');self.assertEqual(len(reviewer.calls),3)
  self.assertTrue(all(c['read_only'] for c in reviewer.calls))
  self.assertIn(BRIEF['refined_requirement'],worker.calls[0]['prompt'])
  self.assertIn('original functional request',worker.calls[0]['prompt'])
  self.assertIn('missing negative path',worker.calls[1]['prompt'])
  state=json.loads((self.root/'run/state.json').read_text());self.assertEqual(state['requirements_review']['status'],'ready')
 def test_invalid_or_blocked_requirement_never_launches_worker(self):
  for value in ({}, {**BRIEF,'status':'needs_input','blockers':['External authority must confirm scope']}):
   with self.subTest(value=value),tempfile.TemporaryDirectory() as d:
    root=Path(d);worker=ScriptedProvider('worker',[]);reviewer=ScriptedProvider('reviewer',[self.brief(value),self.brief(value)] if value else [{'verdict':{},'text':'{}'}])
    result=Supervisor(RunConfig('task',root,worker,reviewer=reviewer,workflow='review-first',run_dir=root/'run',git_history='off'),log=lambda _:None).run()
    self.assertEqual(result.status,'action_required');self.assertEqual(worker.calls,[])
 def test_complex_requirement_is_revised_and_independently_rechecked_before_dispatch(self):
  requirement='逐页复查筛选，返回列表时恢复原筛选。'
  gap='验收条件遗漏返回列表后恢复筛选的场景'
  findings=[{'source_id':'original','quote':'返回列表时恢复原筛选','gap':gap}]
  approved={'status':'pass','summary':'需求已覆盖','findings':[]}
  revised={**BRIEF,'acceptance_criteria':[*BRIEF['acceptance_criteria'],'返回列表后恢复筛选']}
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  reviewer=ScriptedProvider('reviewer',[
   self.brief(),self.brief({'status':'revise','summary':'发现漏项','findings':findings}),
   self.brief(revised),self.brief(approved),{'verdict':_verdict('done',evidence=['app.py'])},
  ])
  result=self.run_loop(worker,reviewer,requirement=requirement,fresh_review=False)
  self.assertEqual(result.status,'done',result.summary)
  self.assertEqual(len(worker.calls),1)
  self.assertIn('返回列表后恢复筛选',worker.calls[0]['prompt'])
  self.assertIn(gap,reviewer.calls[2]['prompt'])
  for call in (reviewer.calls[1],reviewer.calls[3]):
   self.assertTrue(call['read_only'])
   self.assertEqual(call['session_id'],'')
   self.assertIn(requirement,call['prompt'])
  receipts=sorted((self.root/'run').glob('*-requirement-coverage.json'))
  self.assertEqual(len(receipts),2)
  initial,final=[json.loads(p.read_text()) for p in receipts]
  self.assertEqual((initial['status'],final['status']),('revise','pass'))
  self.assertNotEqual(initial['plan_sha256'],final['plan_sha256'])

 def test_invalid_coverage_review_cannot_approve_a_plan(self):
  worker=ScriptedProvider('worker',[])
  invalid=self.brief({
   'status':'revise','summary':'引用无效','findings':[{
    'source_id':'original','quote':'原文没有这个字句','gap':'缺少证据',
   }],
  })
  reviewer=ScriptedProvider('reviewer',[self.brief(),invalid,invalid])
  result=self.run_loop(worker,reviewer,requirement='跨角色复查权限。')
  self.assertEqual(result.status,'action_required',result.summary)
  self.assertIn('用户原文',result.summary)
  self.assertIn('2000002-reviewer.log',result.summary)
  self.assertEqual(worker.calls,[])
  self.assertFalse((self.root/'run'/'reviewed-requirement.json').exists())

 def test_invalid_coverage_source_is_rechecked_without_replanning(self):
  invalid=self.brief({
   'status':'revise','summary':'引用了裁判意见','findings':[
    {'source_id':'referee.instructions','quote':'额外建议','gap':'超出本次检查范围'},
   ],
  })
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  reviewer=ScriptedProvider('reviewer',[
   self.brief(),invalid,approve_requirement_coverage(),{'verdict':_verdict('done',evidence=['app.py'])},
  ])
  result=self.run_loop(worker,reviewer,requirement='跨角色复查权限。')
  self.assertEqual(result.status,'done',result.summary)
  initial,correction=reviewer.calls[1:3]
  self.assertTrue(correction['read_only'])
  self.assertIn('修正以下缺项的引用',correction['prompt'])
  self.assertIn('撤回该项',correction['prompt'])
  for call in (initial,correction):
   self.assertIn(BRIEF['refined_requirement'],call['prompt'])
  self.assertEqual(len(list((self.root/'run').glob('*-requirement-coverage.json'))),1)

 def test_unresolved_coverage_gap_never_dispatches_the_plan(self):
  rejected=self.brief({'status':'revise','summary':'仍有漏项','findings':[
   {'source_id':'original','quote':'跨角色复查权限','gap':'缺少权限不足的验收场景'},
  ]})
  reviewer=ScriptedProvider('reviewer',[self.brief(),rejected,self.brief(),rejected])
  worker=ScriptedProvider('worker',[])
  result=self.run_loop(worker,reviewer,requirement='跨角色复查权限。')
  self.assertEqual(result.status,'action_required',result.summary)
  self.assertIn('缺少权限不足的验收场景',result.summary)
  self.assertEqual(len(reviewer.calls),4)
  self.assertEqual(worker.calls,[])

 def test_direction_arriving_during_coverage_review_forces_a_new_plan(self):
  from auto_moniter.directions import Directions
  run=self.root/'run'

  class Reviewer(ScriptedProvider):
   def _command(self,session,prompt_file,read_only):
    if len(self.calls)==1:
     Directions(run).append('新要求：返回列表时保留页码。')
    return super()._command(session,prompt_file,read_only)

  approved=self.brief({'status':'pass','summary':'需求已覆盖','findings':[]})
  revised=self.brief({**BRIEF,'acceptance_criteria':['返回列表时保留页码']})
  reviewer=Reviewer('reviewer',[
   self.brief(),approved,revised,approved,{'verdict':_verdict('done',evidence=['app.py'])},
  ])
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  result=self.run_loop(worker,reviewer,requirement='跨页面复查列表和详情。')
  self.assertEqual(result.status,'done',result.summary)
  self.assertIn('返回列表时保留页码',reviewer.calls[2]['prompt'])
  self.assertIn('返回列表时保留页码',worker.calls[0]['prompt'])
  self.assertEqual(len(list(run.glob('*-requirement-coverage.json'))),1)
 def test_resume_reuses_ready_requirement_and_rejects_workflow_change(self):
  worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',['unfinished']),'touch':'app.py'}])
  reviewer=ScriptedProvider('reviewer',[self.brief(),{'verdict':_verdict('continue',['still unfinished'])}])
  supervisor=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',run_dir=self.root/'run',git_history='off',retry_delay_s=0,review_every=1),log=lambda _:None)
  supervisor.log=lambda message:supervisor.request_stop() if 'reviewer status=continue' in message else None
  self.assertEqual(supervisor.run().status,'stopped')
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py'])}]);reviewer=ScriptedProvider('reviewer',[{'verdict':_verdict('done',evidence=['app.py'],closed=['AUD-1'])}])
  result=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='auto',resume=True,run_dir=self.root/'run',git_history='off',retry_delay_s=0),log=lambda _:None).run()
  self.assertEqual(result.status,'done');self.assertEqual(len(reviewer.calls),1)
 def test_git_branch_is_created_only_after_review_and_watcher_checks(self):
  subprocess.run(['git','init','-q',str(self.root)],check=True)
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'},
                                   {'verdict':_verdict('done',evidence=['app.py'])}])
  class Reviewer(ScriptedProvider):
   def _command(inner,session,prompt_file,read_only):
    if inner.calls: # first invocation is requirements refinement
     info=json.loads((self.root/'run/git-history.json').read_text())
     self.assertEqual(info['head'],'')
     branch=subprocess.run(['git','-C',str(self.root),'show-ref','--verify',info['ref']],capture_output=True)
     self.assertNotEqual(branch.returncode,0)
     h=re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt_file.read_text()).group(1)
     inner.scripts[0]['verdict']['evidence'].append(h)
    return super(Reviewer,inner)._command(session,prompt_file,read_only)
  reviewer=Reviewer('reviewer',[self.brief(),{'verdict':_verdict('continue',['need fix'])},
                  {'verdict':_verdict('done',evidence=['app.py'],closed=['AUD-1'])}])
  check='test -f app.py'
  result=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',run_dir=self.root/'run',git_history='required',checks=[check],retry_delay_s=0,max_rounds=2),log=lambda _:None).run()
  self.assertEqual(result.status,'done',result.summary)
  info=json.loads((self.root/'run/git-history.json').read_text())
  commits=subprocess.check_output(['git','-C',str(self.root),'log','--format=%s',info['ref']]).decode()
  self.assertNotIn('Round 1',commits);self.assertIn('Round 2 worker',commits);self.assertIn('Round 2 reviewer',commits)
  self.assertFalse(result.records[0].git_checkpoint)
 def test_rejected_check_does_not_create_approval_commits(self):
  subprocess.run(['git','init','-q',str(self.root)],check=True)
  class Reviewer(ScriptedProvider):
   def _command(inner,s,p,r):
    if inner.calls:inner.scripts[0]['verdict']['evidence'].append(re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',p.read_text()).group(1))
    return super(Reviewer,inner)._command(s,p,r)
  reviewer=Reviewer('reviewer',[self.brief(),{'verdict':_verdict('done',evidence=['app.py'])}])
  worker=ScriptedProvider('worker',[{'verdict':_verdict('done',evidence=['app.py']),'touch':'app.py'}])
  result=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',run_dir=self.root/'run',git_history='required',checks=['false'],retry_delay_s=0,max_rounds=1),log=lambda _:None).run()
  self.assertEqual(result.status,'action_required');self.assertEqual(json.loads((self.root/'run/git-history.json').read_text())['head'],'')
 def test_legacy_checkpoint_cannot_be_silently_upgraded(self):
  w=ScriptedProvider('worker',[])
  s=Supervisor(RunConfig('task',self.root,w,no_review=True,run_dir=self.root/'run',git_history='off'),log=lambda _:None)
  s.request_stop();s.run()
  s=Supervisor(RunConfig('task',self.root,w,reviewer=ScriptedProvider('reviewer',[]),workflow='review-first',resume=True,run_dir=self.root/'run',git_history='off'),log=lambda _:None)
  self.assertEqual(s.run().status,'invalid')
 def test_requirement_reviewer_write_stops_before_worker(self):
  w=ScriptedProvider('worker',[]);r=ScriptedProvider('reviewer',[{**self.brief(),'touch':'unauthorized.py'}])
  result=self.run_loop(w,r)
  self.assertEqual(result.status,'action_required');self.assertEqual(w.calls,[])
 def test_worker_cannot_advance_business_head_without_detection(self):
  from auto_moniter.git_history import GitHistoryError
  subprocess.run(['git','init','-q',str(self.root)],check=True)
  run=self.root/'run';run.mkdir();history=ApprovedHistory(self.root,run)
  subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=test@local','commit','--allow-empty','-qm','unauthorized'],check=True)
  with self.assertRaisesRegex(GitHistoryError,'Business HEAD'):history.verify()
