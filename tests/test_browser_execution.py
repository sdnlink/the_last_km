import fcntl
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from threading import RLock
import time
import unittest
from unittest.mock import patch

from auto_moniter import browser_execution, candidate_checks, field_handoff
from auto_moniter.storage import write_json
from auto_moniter.workspace import workspace_fingerprint
from console.tasks import TaskStore


class BrowserExecutionTests(unittest.TestCase):
    def test_captcha_is_sent_only_to_the_live_bound_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            value = {'binding': {'round': 9}, 'revision': 'current'}
            item = {'id': '1', 'case_id': 'login-routes'}
            config = {'url': 'https://example.test/login', 'expected_version': '1',
                      'interactive_captcha': True}
            job, _ = browser_execution._job(run, value, item, config)
            job.mkdir(parents=True)
            write_json(job / 'captcha.json', {
                'nonce': 'current-image', 'expires': time.time() + 30, 'image': 'YWJj', 'role': 'member',
            })
            with (job / 'execution.lock').open('a') as server:
                fcntl.flock(server, fcntl.LOCK_EX)
                self.assertEqual(browser_execution.poll(run, value, item, config)['status'], 'needs_input')
                with self.assertRaisesRegex(ValueError, '过期'):
                    browser_execution.submit_captcha(run, value, item, config, 'stale', 'A123')
                browser_execution.submit_captcha(run, value, item, config, 'current-image', 'A123')
                self.assertEqual(browser_execution.poll(run, value, item, config)['status'], 'running')
                answer = json.loads((job / 'captcha-answer.json').read_text())
                self.assertEqual(answer, {'nonce': 'current-image', 'code': 'A123'})
                (job / 'captcha-answer.json').unlink()
                write_json(job / 'captcha.json', {
                    'nonce': 'current-otp', 'expires': time.time() + 30, 'kind': 'otp', 'role': 'member',
                })
                with self.assertRaisesRegex(ValueError, '六位'):
                    browser_execution.submit_captcha(run, value, item, config, 'current-otp', 'A123')
                browser_execution.submit_captcha(run, value, item, config, 'current-otp', '013579')
                self.assertEqual(json.loads((job / 'captcha-answer.json').read_text())['code'], '013579')

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

    def test_bind_missing_browser_case_preserves_other_receipts(self):
        task = {'id': 'LOGIN-ROUTES', 'kind': 'verification'}
        state = {'function_tasks': {'current': task}, 'rounds': [
            {'role': 'worker', 'index': 9, 'tree': 'a' * 64, 'watcher_result': {
                'candidate_checks': {'context': {'task': task}, 'digest': 'b' * 64}}},
            {'role': 'reviewer', 'index': 9, 'blockers': ['现场证据'],
             'task_status': 'changes_required', 'watcher_result': {'status': 'blocked'}},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            value = field_handoff.configure(run, state, [
                {'kind': 'browser', 'title': '路由取证', 'owner': '现场测试'},
                {'kind': 'observation', 'title': '构建复测', 'owner': 'Watcher'},
            ])
            checkpoint = {key: value[key] for key in ('binding', 'revision')}
            saved = field_handoff.submit(run, state, {
                **checkpoint, 'id': '2', 'receipt': {'result': '构建通过'},
            })
            build = deepcopy(saved['items'][1])
            saved['items'][0]['case_id'] = ''
            write_json(run / field_handoff.FILE, saved)
            config = {'url': 'https://target.example/login', 'expected_version': '1.2.3'}
            bound = field_handoff.bind_browser_case(run, state, {
                **checkpoint, 'id': '1', 'case_id': 'login-routes',
            }, config)
            self.assertEqual(bound['revision'], value['revision'])
            self.assertEqual(bound['items'][0]['case_id'], 'login-routes')
            self.assertTrue(bound['items'][0]['browser_proof'])
            self.assertEqual(field_handoff.read(run)['items'][1], build)

    def test_recovery_auto_binds_for_the_registered_candidate_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run'
            run.mkdir()
            workspace = root / 'project'
            workspace.mkdir()
            (workspace / 'app.py').write_text('pass\n', encoding='utf-8')
            task = {'id': 'LOGIN-ROUTES', 'kind': 'verification'}
            tree = workspace_fingerprint(workspace, ignore_dir=run)
            context = {'commands': [], 'task': task}
            receipt = candidate_checks.create(run, context, {}, True)
            state = {'cwd': str(workspace), 'function_tasks': {'current': task}, 'rounds': [
                {'role': 'worker', 'index': 9, 'tree': tree,
                 'watcher_result': {'candidate_checks': receipt}},
                {'role': 'reviewer', 'index': 9, 'task_status': 'changes_required',
                 'blockers': ['路由证据'], 'watcher_result': {'status': 'blocked'}},
            ]}
            write_json(run / 'state.json', state)
            handoff = field_handoff.configure(run, state, [
                {'kind': 'browser', 'title': '路由点击', 'owner': '现场执行器'},
                {'kind': 'browser', 'title': '路由刷新', 'owner': '现场执行器'},
            ])
            command = (
                'from datetime import datetime, timezone; import json, sys; '
                'r=json.load(sys.stdin); assert r["binding"]["task_id"]=="LOGIN-ROUTES"; '
                'print(json.dumps(dict(receipt=dict(version=r["expected_version"], '
                'session_reference="tab", result="observed"), proof=dict(url=r["url"], '
                'session_reference="tab", observed_at=datetime.now(timezone.utc).isoformat()))))'
            )
            registration = {
                'workspace': str(workspace), 'task_id': task['id'], 'case_id': 'login-routes',
                'candidate_tree': 'a' * 64, 'url': 'https://example.test/login',
                'expected_version': '1', 'argv': [sys.executable, '-c', command],
                'handoff_ids': ['1', '2'], 'handoff_revision': handoff['revision'],
            }
            catalog = root / browser_execution.CONFIG
            write_json(catalog, {'cases': [registration]}, mode=0o600)
            store = object.__new__(TaskStore)
            store.data_dir = root
            store._lock = RLock()
            meta = {'id': 'parent', 'cwd': str(workspace), 'run_dir': str(run), 'managed': True}
            with (patch.object(store, '_catalog', return_value={'parent': meta}),
                  patch.object(store, '_view', return_value={'status': 'action_required', 'actions': ['resume']}),
                  patch.object(store, '_route_browser_failure'),
                  patch.object(store, 'action') as resume):
                store.recover_field_handoffs()
                self.assertFalse((run / 'browser-executions').exists())
                resume.assert_not_called()
                registration['candidate_tree'] = tree
                write_json(catalog, {'cases': [registration]}, mode=0o600)
                deadline = time.monotonic() + 10
                while not resume.called and time.monotonic() < deadline:
                    store.recover_field_handoffs()
                    time.sleep(.1)
                resume.assert_called_once_with('parent', 'resume')
                store.recover_field_handoffs()
                resume.assert_called_once_with('parent', 'resume')
            value = field_handoff.read(run)
            self.assertEqual(value['resume']['status'], 'launched')
            for item in value['items']:
                self.assertEqual(item['case_id'], 'login-routes')
                self.assertEqual(item['receipt']['proof']['url'], registration['url'])
            jobs = list((run / 'browser-executions').iterdir())
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads((jobs[0] / 'launch.json').read_text())['request']['item_ids'], ['1', '2'])

    def test_tig_settings_changes_invalidate_an_old_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = root / 'tig.json'
            config = root / browser_execution.CONFIG
            write_json(config, {'cases': [{
                'workspace': str(root), 'case_id': 'login-routes', 'url': 'https://example.test/login',
                'expected_version': '1', 'tig_settings': str(settings),
            }]}, mode=0o600)
            load = lambda: browser_execution.cases(config, root)['login-routes']
            self.assertIn('execution_error', load())
            adapter = root / 'captcha'
            write_json(settings, {'captcha_command': [str(adapter)]}, mode=0o600)
            self.assertIn('验证码输入程序', load()['execution_error'])
            adapter.write_text('#!/bin/sh\n', encoding='utf-8')
            adapter.chmod(0o700)
            with patch.object(browser_execution, 'find_spec', return_value=True):
                first = load()
                self.assertEqual(first['argv'][1:3], ['-m', 'auto_moniter.tig_browser_case'])
                write_json(settings, {'captcha_command': [str(adapter), '--demo']}, mode=0o600)
                self.assertNotEqual(first['settings_digest'], load()['settings_digest'])

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
