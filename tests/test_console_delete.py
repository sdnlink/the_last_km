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
 def test_delete_stopped_removes_all_generated_logs_preserves_git_and_excludes_rediscovery(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  names=['001-worker.log','001-reviewer.log','001-check-01.log','console.log','monitor.log','live.jsonl',
         'live.previous.jsonl','events.jsonl','events.previous.jsonl','001-worker-prompt.md',
         '001-worker-text.md','001-reviewer-repair-1-text.md','monitor.log.1','console.log.gz','console.log.2.gz']
  for name in names:(run/name).write_text('audit log')
  history=self.project/'.auto-moniter/history';history.mkdir(parents=True);(history/'evidence').write_text('keep')
  self.store.logs(task['id'])
  result=self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertTrue(result['logs_cleaned']);self.assertEqual(result['log_files_removed'],len(names))
  for name in names:self.assertFalse((run/name).exists(),name)
  self.assertEqual(self.store._log_reader.indexes,{})
  self.assertTrue((run/'state.json').is_file());self.assertTrue((run/'report.json').is_file())
  self.assertEqual((run/'requirement.md').read_text(),'test')
  self.assertEqual((history/'evidence').read_text(),'keep')
  restored=TaskStore(self.root/'data',self.project,[run.parent])
  self.assertEqual(restored.task_summaries()['tasks'],[])
 def test_delete_discovered_run_cleans_only_its_logs(self):
  run=self.project/'.auto-moniter/imported/run';self.finish(run,'done')
  (run/'001-worker.log').write_text('remove')
  (self.project/'business.log').write_text('preserve project log')
  task=self.store.task_summaries()['tasks'][0]
  self.assertTrue(task['id'].startswith('ext-'));self.assertIn('delete',task['actions'])
  self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertFalse((run/'001-worker.log').exists())
  self.assertEqual((self.project/'business.log').read_text(),'preserve project log')
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
 def test_log_cleanup_failure_keeps_task_visible_and_retryable(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  (run/'001-worker.log').write_text('remove on retry')
  with patch('console.deletion.os.unlink',side_effect=PermissionError('cannot unlink log')):
   with self.assertRaises(TaskError) as error:self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertEqual(error.exception.status,500)
  self.assertEqual(self.store.detail(task['id'])['id'],task['id'])
  self.assertEqual(list(self.store.deleted_dir.glob('*.json')),[])
  self.assertTrue((run/'001-worker.log').exists())
  self.assertTrue(self.store.action(task['id'],'delete',{'confirm':task['id']})['deleted'])
 def test_delete_does_not_follow_symlinks_or_delete_project_root_logs(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  target=self.project/'business.log';target.write_text('keep')
  (run/'001-worker.log').symlink_to(target)
  self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertFalse((run/'001-worker.log').is_symlink());self.assertEqual(target.read_text(),'keep')
  other=self.create();meta=self.store._get(other['id']);meta['run_dir']=str(self.project);self.store._save(meta)
  with self.assertRaises(TaskError):self.store.action(other['id'],'delete',{'confirm':other['id']})
  self.assertEqual(target.read_text(),'keep')
 def test_delete_refuses_a_symlinked_run_directory(self):
  task=self.create();run=Path(task['run_dir']);external=self.root/'external';self.finish(external)
  (external/'001-worker.log').write_text('keep')
  run.symlink_to(external,target_is_directory=True)
  with self.assertRaises(TaskError):self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertEqual((external/'001-worker.log').read_text(),'keep')
 def test_registration_removal_failure_does_not_hide_a_partially_deleted_task(self):
  task=self.create();run=Path(task['run_dir']);self.finish(run)
  (run/'001-worker.log').write_text('remove')
  original=Path.unlink
  def fail_registration(path,*args,**kwargs):
   if path==self.store.tasks_dir/(task['id']+'.json'):raise PermissionError('registration locked')
   return original(path,*args,**kwargs)
  with patch.object(Path,'unlink',fail_registration):
   with self.assertRaises(TaskError):self.store.action(task['id'],'delete',{'confirm':task['id']})
  self.assertEqual(self.store.detail(task['id'])['id'],task['id'])
  self.assertEqual(list(self.store.deleted_dir.glob('*.json')),[])
  self.assertTrue(self.store.action(task['id'],'delete',{'confirm':task['id']})['deleted'])
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
