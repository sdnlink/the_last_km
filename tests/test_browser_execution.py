import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from auto_moniter import browser_execution, field_handoff
from auto_moniter.storage import write_json
from console.tasks import TaskStore


class BrowserExecutionTests(unittest.TestCase):
    def test_configured_case_is_collected_without_a_child_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            run.mkdir()
            task = {'id':'VERIFY', 'kind':'verification'}
            state = {'function_tasks':{'current':task}, 'rounds':[
                {'role':'worker', 'index':8, 'tree':'a'*64,
                 'watcher_result':{'candidate_checks':{'context':{'task':task}, 'digest':'b'*64}}},
                {'role':'reviewer', 'index':8, 'task_status':'changes_required',
                 'blockers':['浏览器证据'], 'watcher_result':{'status':'blocked'}},
            ]}
            value = field_handoff.configure(run, state, [
                {'kind':'browser', 'case_id':'role-login', 'title':'核对角色', 'owner':'浏览器执行器'},
            ])
            command = ('import json,sys; r=json.load(sys.stdin); assert r["binding"]["round"]==8; '
                       'print(json.dumps(dict(version=r["expected_version"],session_reference="test-tab",result="ok")))')
            write_json(root / browser_execution.CONFIG, {'cases': [{
                'workspace':str(root), 'case_id':'role-login', 'url':'https://example.test/login',
                'expected_version':'1', 'argv':[sys.executable,'-c',command],
            }]}, mode=0o600)
            store = object.__new__(TaskStore)
            store.data_dir = root
            meta = {'id':'parent','cwd':str(root)}
            limit = time.monotonic() + 10
            while not field_handoff.ready(value) and time.monotonic() < limit:
                value = store._collect_field_results(meta, run, state, value)
                if not field_handoff.ready(value):
                    time.sleep(.1)
            self.assertTrue(field_handoff.ready(value), value.get('executor_status'))
            self.assertEqual(value['items'][0]['receipt']['executor_task'], 'browser:role-login')
            self.assertEqual(value['resume']['status'], 'pending')
            jobs = list((run / 'browser-executions').iterdir())
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads((jobs[0] / 'launch.json').read_text())['attempts'], 1)

    def test_automation_failure_is_routed_only_once_per_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            value = {'binding':{'round':8}, 'revision':'initial',
                     'items':[{'id':'1','kind':'browser','case_id':'role-login'}],
                     'failed_cases':{'1':{'case_id':'role-login','reason':'未完成','role':'referee'}}}
            write_json(run / field_handoff.FILE, value)
            store = object.__new__(TaskStore)
            with patch.object(store, 'action') as start:
                store._route_browser_failure({'id':'t'}, run, value)
                current = field_handoff.read(run)
                store._route_browser_failure({'id':'t'}, run, current)
                start.assert_called_once_with('t', 'resume')
            self.assertEqual(current['fallback']['role'], 'referee')
            self.assertEqual(current['fallback']['status'], 'launched')

    def test_wrong_observed_version_cannot_create_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            command = 'print(\'{"version":"wrong","session_reference":"test","result":"ok"}\')'
            write_json(job/'launch.json', {'request': {
                'binding':{},'revision':'r','id':'1','expected_version':'correct'},
                'argv':[sys.executable,'-c',command]})
            browser_execution.run_case(job)
            result = json.loads((job/'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn('submission', result)
