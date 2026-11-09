import json,tempfile,unittest
from pathlib import Path
from auto_moniter.failures import classify
from auto_moniter.supervisor import Supervisor,RunConfig
from auto_moniter.providers import resolve_provider
from auto_moniter.live import public_events
from tests.test_supervisor import ScriptedProvider,_verdict
from console.tasks import TaskStore

class GrokAuthenticationTests(unittest.TestCase):
 def test_actual_login_error_is_not_retried(self):
  error='Alternatively, set the XAI_API_KEY environment variable or run `grok login` on a machine with a browser.'
  self.assertEqual(classify(error),('authentication','stop'))
  self.assertEqual(classify('Not signed in. To authenticate without a browser, run: grok login --device-code'),('authentication','stop'))
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);worker=ScriptedProvider('grok',[{'verdict':_verdict('continue'),'exit_code':1,'text':error}])
   r=Supervisor(RunConfig('task',root,worker,no_review=True,run_dir=root/'run',git_history='off',retry_delay_s=0),log=lambda _:None).run()
   self.assertEqual(r.status,'action_required');self.assertEqual(len(worker.calls),1)
   task=TaskStore(root/'data',root).task_summaries() # fixture run is not scanned here
 def test_grok_print_uses_streaming_progress_and_keeps_plan_boundary(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);prompt=root/'p';prompt.write_text('task');schema=root/'s';schema.write_text('{}')
   cmd=resolve_provider('grok').start_command(prompt,root,schema,read_only=True).argv
   self.assertEqual(cmd[cmd.index('--output-format')+1],'streaming-json');self.assertIn('plan',cmd)
 def test_streaming_public_messages_and_tools_exclude_reasoning(self):
  events=public_events({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'private'},
    {'type':'text','text':'正在检查项目'},{'type':'tool_use','name':'Bash','input':{'command':'pwd'},'id':'call1'}]}})
  self.assertTrue(any(e['kind']=='command' and e['text']=='pwd' for e in events))
  self.assertNotIn('private',json.dumps(events));self.assertIn('正在检查项目',json.dumps(events,ensure_ascii=False))
 def test_stopped_grok_task_explains_real_auth_failure_in_web(self):
  from auto_moniter.storage import write_json
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);store=TaskStore(root/'data',root)
   task=store.create({'title':'Grok fixture','requirement':'task','provider':'grok','reviewer':'codex','workflow':'legacy'})
   run=Path(task['run_dir']);run.mkdir()
   row={'role':'worker','index':1,'status':'continue','summary':'failed','gaps':['Not signed in. Run grok login'],'failure':'set the XAI_API_KEY environment variable'}
   write_json(run/'state.json',{'cwd':str(root),'status':'stopped','rounds':[row],'last_worker':row})
   write_json(run/'report.json',{'status':'stopped','rounds':1,'records':[row],'summary':'stopped','stop_reason':'stopped'})
   (run/'supervisor.lock').touch()
   view=store.detail(task['id'])
   self.assertEqual(view['status'],'stopped');self.assertIn('认证不可用',view['control_message'])
   self.assertIn('grok login --device-code',view['control_message'])
