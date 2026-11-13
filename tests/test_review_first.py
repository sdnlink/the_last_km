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

class ReviewFirstTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def brief(self,value=None):return {'verdict':{},'text':json.dumps(value or BRIEF)}
 def run_loop(self,worker,reviewer,**kwargs):
  return Supervisor(RunConfig('original functional request',self.root,worker,reviewer=reviewer,
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
    root=Path(d);worker=ScriptedProvider('worker',[]);reviewer=ScriptedProvider('reviewer',[self.brief(value) if value else {'verdict':{},'text':'{}'}])
    result=Supervisor(RunConfig('task',root,worker,reviewer=reviewer,workflow='review-first',run_dir=root/'run',git_history='off'),log=lambda _:None).run()
    self.assertEqual(result.status,'action_required');self.assertEqual(worker.calls,[])
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
