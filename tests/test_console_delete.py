import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from auto_moniter.storage import write_json
from auto_moniter.workspace import file_lock
from console.server import ConsoleServer
from console.tasks import TaskStore, TaskError


class DeleteTaskTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.project=self.root/'project';self.project.mkdir()
  self.env=patch.dict(os.environ,{'AUTO_MONITER_STATE_DIR':str(self.root/'state')});self.env.start()
  self.store=TaskStore(self.root/'data',self.project)
 def tearDown(self):self.env.stop();self.temp.cleanup()
 def create(self):
  return self.store.create({'title':'待删除任务','requirement':'test','provider':'codex','reviewer':'codex'})
 def finish(self,run,status='stopped'):
  run.mkdir(parents=True,exist_ok=True)
  write_json(run/'state.json',{'cwd':str(self.project),'status':status,'rounds':[]})
  write_json(run/'report.json',{'status':status,'rounds':0,'summary':'test','stop_reason':status})
  (run/'supervisor.lock').touch();(run/'requirement.md').write_text('test')
 def test_delete_queued_persists_and_preserves_project(self):
  task=self.create();(self.project/'app.py').write_text('preserve')
  self.assertIn('delete',task['actions'])
  result=self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertTrue(result['deleted']);self.assertEqual((self.project/'app.py').read_text(),'preserve')
  restored=TaskStore(self.root/'data',self.project)
  self.assertEqual(restored.task_summaries()['tasks'],[])
  with self.assertRaises(TaskError) as error:restored.detail(task['id'])
  self.assertEqual(error.exception.status,404)
 def test_delete_stopped_retains_logs_git_history_and_excludes_rediscovery(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  (run/'001-worker.log').write_text('audit log')
  history=self.project/'.auto-moniter/history';history.mkdir(parents=True);(history/'evidence').write_text('keep')
  self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertEqual((run/'001-worker.log').read_text(),'audit log')
  self.assertEqual((history/'evidence').read_text(),'keep')
  restored=TaskStore(self.root/'data',self.project,[run.parent])
  self.assertEqual(restored.task_summaries()['tasks'],[])
 def test_delete_discovered_run_is_persistent_without_removing_external_files(self):
  run=self.project/'.auto-moniter/imported/run';self.finish(run,'done')
  task=self.store.task_summaries()['tasks'][0]
  self.assertTrue(task['id'].startswith('ext-'));self.assertIn('delete',task['actions'])
  self.store.action(task['id'],'delete',{'confirm':task['id']})
  restored=TaskStore(self.root/'data',self.project)
  self.assertEqual(restored.task_summaries()['tasks'],[]);self.assertTrue((run/'state.json').exists())
 def test_wrong_or_missing_confirmation_never_deletes(self):
  task=self.create()
  for payload in [None,{}, {'confirm':'different'}, {'confirm':True}]:
   with self.subTest(payload=payload),self.assertRaises(TaskError):self.store.action(task['id'],'delete',payload)
  self.assertEqual(self.store.detail(task['id'])['status'],'queued')
 def test_held_locks_paused_state_and_pending_process_prevent_delete(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  for lock in ['supervisor.lock','guardian.lock']:
   with self.subTest(lock=lock),file_lock(run/lock):
    self.assertNotIn('delete',self.store.detail(task['id'])['actions'])
    with self.assertRaises(TaskError):self.store.action(task['id'],'delete',{'confirm':task['id']})
  write_json(run/'process.json',{'pid':123})
  self.assertNotIn('delete',self.store.detail(task['id'])['actions'])
  with self.assertRaises(TaskError):self.store.action(task['id'],'delete',{'confirm':task['id']})
 def test_lock_acquired_after_render_still_refuses_delete(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  view=self.store.detail(task['id']);self.assertIn('delete',view['actions'])
  with file_lock(run/'guardian.lock'),patch.object(self.store,'_view',return_value=view):
   with self.assertRaises(TaskError) as error:self.store.action(task['id'],'delete',{'confirm':task['id']})
   self.assertEqual(error.exception.status,409)
  self.assertTrue((self.store.tasks_dir/(task['id']+'.json')).exists())
 def test_launcher_and_stale_recovery_record_are_not_hidden(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  view=self.store.detail(task['id'])
  with patch.object(self.store,'_launcher_alive',return_value=True),patch.object(self.store,'_view',return_value=view):
   with self.assertRaises(TaskError):self.store.action(task['id'],'delete',{'confirm':task['id']})
  (run/'process.json').write_text('{}')
  with patch.object(self.store,'_view',return_value=view):
   with self.assertRaises(TaskError):self.store.action(task['id'],'delete',{'confirm':task['id']})
 def test_authenticated_http_delete_and_stale_requests(self):
  task=self.create();ident=task['id']
  with ConsoleServer(('127.0.0.1',0),self.store) as server:
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();port=server.server_address[1]
   def request(method,path,data=None,token=None):
    connection=HTTPConnection('127.0.0.1',port,timeout=5)
    headers={'Content-Type':'application/json'}
    if token:headers['X-Console-Token']=token
    connection.request(method,path,json.dumps(data) if data is not None else None,headers)
    response=connection.getresponse();result=(response.status,json.loads(response.read()));connection.close();return result
   try:
    token=request('GET','/api/config')[1]['csrf_token']
    self.assertEqual(request('POST',f'/api/tasks/{ident}/delete',{'confirm':ident})[0],403)
    self.assertEqual(request('POST',f'/api/tasks/{ident}/delete',{},token)[0],400)
    status,result=request('POST',f'/api/tasks/{ident}/delete',{'confirm':ident},token)
    self.assertEqual(status,200);self.assertTrue(result['deleted'])
    self.assertEqual(request('GET',f'/api/tasks/{ident}')[0],404)
    self.assertEqual(request('POST',f'/api/tasks/{ident}/resume',{},token)[0],404)
    self.assertEqual(request('GET','/api/tasks')[1]['tasks'],[])
   finally:server.shutdown();thread.join(timeout=5)
