"""A persisted amendment resumes through the existing guarded launch path."""
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_moniter.directions import Directions
from auto_moniter.processes import process_identity
from auto_moniter.providers import resolve_provider
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.tasks import TaskError, TaskStore
from tests.test_review_first import BRIEF
from tests.test_supervisor import _verdict


class DirectionResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        binaries = self.root/'bin'
        binaries.mkdir()
        fake = binaries/'codex'
        fake.write_text(f'#!{sys.executable}\nraise SystemExit("fixture only")\n')
        fake.chmod(0o700)
        self.env = patch.dict(os.environ, {'PATH':str(binaries)+os.pathsep+os.environ['PATH'],
                                          'AUTO_MONITER_STATE_DIR':str(self.root/'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = TaskStore(self.root/'data',self.project)
        self.task = self.store.create({'title':'fixture','requirement':'implement fixture',
            'provider':'codex','reviewer':'codex','workflow':'legacy','review_isolation':'provider'})
        self.ident = self.task['id']
        self.run = Path(self.task['run_dir'])

    def checkpoint(self,status='action_required'):
        self.run.mkdir(exist_ok=True)
        provider = resolve_provider('codex')
        config = RunConfig('',self.project,provider,reviewer=provider)
        state = {'cwd':str(self.project),'status':status,'checks':[], 'directions_version':1,
            'provider_config':Supervisor._provider_config(SimpleNamespace(config=config)),
            'usage':{'elapsed_s':2,'agent_calls':1},'rounds':[]}
        write_json(self.run/'state.json',state)
        write_json(self.run/'report.json',{'status':status,'rounds':0,'summary':'fixture','stop_reason':status})
        (self.run/'supervisor.lock').touch()
        (self.run/'requirement.md').write_text('implement fixture')
        return state

    def test_stopped_task_saves_once_and_resumes_without_second_submission(self):
        self.checkpoint()
        before=(self.run/'state.json').read_bytes()
        with patch.object(self.store,'_check_start'),patch.object(self.store,'_launch') as launch:
            result=self.store.add_direction(self.ident,{'text':'test the real engine'})
        self.assertEqual(result['direction_submission']['status'],'resuming')
        self.assertEqual(launch.call_args.kwargs,{'resume':True})
        self.assertEqual(Directions(self.run).messages()[0]['text'],'test the real engine')
        self.assertIsNone(result['directions'][0]['worker'])
        self.assertEqual(before,(self.run/'state.json').read_bytes())
        saved=Directions(self.run).messages()
        with patch.object(self.store,'_check_start'),patch.object(self.store,'_launch') as launch:
            result=self.store.add_direction(self.ident,{'text':''})
        self.assertEqual(result['direction_submission']['status'],'resuming')
        self.assertEqual(Directions(self.run).messages(),saved)
        launch.assert_called_once()

    def test_budget_exhaustion_preserves_saved_content_and_requires_explicit_budget(self):
        state=self.checkpoint('budget_exceeded')
        state['usage']={'elapsed_s':7201,'agent_calls':100}
        write_json(self.run/'state.json',state)
        before=(self.run/'state.json').read_bytes()
        with patch.object(self.store,'_check_start'),patch.object(self.store,'_launch') as launch:
            result=self.store.add_direction(self.ident,{'text':'saved even when budget is exhausted'})
        launch.assert_not_called()
        self.assertEqual(result['direction_submission']['status'],'resume_blocked')
        self.assertIn('预算已耗尽',result['direction_submission']['error'])
        self.assertEqual(before,(self.run/'state.json').read_bytes())
        self.assertEqual(len(result['directions']),1)

    def test_unavailable_checkpoint_and_launch_failure_do_not_lose_saved_direction(self):
        self.checkpoint()
        (self.run/'requirement.md').unlink()
        with patch.object(self.store,'_launch') as launch:
            result=self.store.add_direction(self.ident,{'text':'keep this requirement'})
        self.assertEqual(result['direction_submission']['status'],'resume_blocked')
        self.assertIn('缺少原始需求',result['direction_submission']['error'])
        launch.assert_not_called()
        (self.run/'requirement.md').write_text('implement fixture')
        write_json(self.run/'control.json',{'action':'stop'})
        with patch.object(self.store,'_check_start'),patch('console.tasks.subprocess.Popen',side_effect=OSError('spawn failed')):
            result=self.store.add_direction(self.ident,{'text':''})
        self.assertEqual(result['direction_submission']['status'],'resume_blocked')
        self.assertIn('spawn failed',result['direction_submission']['error'])
        self.assertEqual(len(result['directions']),1)
        self.assertEqual(json.loads((self.run/'control.json').read_text()),{'action':'stop'})
        self.assertIsNone(result['directions'][0]['worker'])

    def test_paused_or_pending_pause_continues_without_spawning_another_process(self):
        self.checkpoint()
        for status in ('paused','running'):
            with self.subTest(status=status):
                view={**self.store.detail(self.ident),'status':status,'actions':['continue','stop']}
                write_json(self.run/'control.json',{'action':'pause'})
                with patch.object(self.store,'_view',return_value=view),patch.object(self.store,'_launch') as launch:
                    result=self.store.add_direction(self.ident,{'text':'continue the task'})
                self.assertEqual(result['direction_submission']['status'],'continuing')
                self.assertFalse((self.run/'control.json').exists())
                launch.assert_not_called()

    def test_running_submission_and_explicit_pause_do_not_restart(self):
        self.checkpoint()
        view={**self.store.detail(self.ident),'status':'running','actions':['pause','stop']}
        with patch.object(self.store,'_view',return_value=view),patch.object(self.store,'_launch') as launch:
            result=self.store.add_direction(self.ident,{'text':'next turn'})
            self.assertEqual(result['direction_submission']['status'],'pending')
            result=self.store.add_direction(self.ident,{'text':'pause after this turn','pause':True})
        self.assertEqual(result['direction_submission']['status'],'pause_requested')
        self.assertEqual(json.loads((self.run/'control.json').read_text()),{'action':'pause'})
        launch.assert_not_called()

    def test_concurrent_retries_of_saved_direction_launch_only_once(self):
        self.checkpoint()
        Directions(self.run).append('already saved')
        def launch(meta,*,resume):
            self.assertTrue(resume)
            self.store._save({**meta,'status':'starting','launcher':process_identity(os.getpid())})
        with patch.object(self.store,'_check_start'),patch.object(self.store,'_launch',side_effect=launch) as mocked:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:self.store.add_direction(self.ident,{'text':''}),range(2)))
        mocked.assert_called_once()
        self.assertEqual({r['direction_submission']['status'] for r in results},{'resuming','pending'})
        self.assertEqual(len(Directions(self.run).messages()),1)

    def test_invalid_input_and_finished_task_never_launch(self):
        self.checkpoint()
        with patch.object(self.store,'_launch') as launch:
            for text in ('',None,'x'*4001,'bad\0text'):
                with self.subTest(text=str(text)[:10]),self.assertRaises(TaskError):
                    self.store.add_direction(self.ident,{'text':text})
            self.checkpoint('done')
            with self.assertRaises(TaskError):self.store.add_direction(self.ident,{'text':'too late'})
        self.assertEqual(Directions(self.run).messages(),[])
        launch.assert_not_called()

    def test_real_resume_delivers_previously_saved_direction_to_reviewer_then_worker(self):
        subprocess.run(['git','init','-q',str(self.project)],check=True)
        marker='Validate the refused-input case'
        fake=self.root/'bin/codex'
        fake.write_text(f'''#!{sys.executable}
import json,sys
from pathlib import Path
prompt=sys.stdin.read()
if 'independent requirement reviewer' in prompt:
    result={BRIEF!r}
    if {marker!r} not in prompt:
        result.update(status='needs_input',blockers=['Need the additional instruction'])
    Path('.auto-moniter').mkdir(exist_ok=True)
    Path('.auto-moniter/review-input.txt').write_text(prompt)
else:
    Path('app.py').write_text('fixture implementation\\n')
    result={_verdict('done',evidence=['app.py'])!r}
    if 'implementation agent' in prompt:
        Path('.auto-moniter/worker-input.txt').write_text(prompt)
print(json.dumps({{'type':'result','result':json.dumps(result)}}),flush=True)
''')
        fake.chmod(0o700)
        task=self.store.create({'title':'real direction resume','requirement':'implement fixture',
            'provider':'codex','reviewer':'codex',
            'workflow':'review-first','function_dispatch':False,'review_isolation':'provider',
            'auto_start':True,'max_rounds':1,'checks':['git diff --check']})
        ident=task['id'];run=Path(task['run_dir'])
        def until(predicate):
            end=time.monotonic()+15
            while time.monotonic()<end:
                if predicate():return
                time.sleep(.04)
            self.fail('fixture did not reach its expected state')
        try:
            until(lambda:ident not in self.store._children)
            self.assertEqual(self.store.detail(ident)['status'],'action_required')
            Directions(run).append(marker)
            result=self.store.add_direction(ident,{'text':''})
            self.assertEqual(result['direction_submission']['status'],'resuming')
            until(lambda:ident not in self.store._children)
            self.assertIn(marker,(self.project/'.auto-moniter/review-input.txt').read_text())
            self.assertIn(marker,(self.project/'.auto-moniter/worker-input.txt').read_text())
            saved=Directions(run).snapshot()
            self.assertEqual(len(saved),1)
            self.assertTrue(saved[0]['reviewer']['responded_at'])
            self.assertTrue(saved[0]['worker']['responded_at'])
        finally:
            child=self.store._children.get(ident)
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=12)
