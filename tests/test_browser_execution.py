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
from console import browser_captcha
from console.tasks import TaskError, TaskStore


class BrowserExecutionTests(unittest.TestCase):
    def test_reopen_recovers_explicitly_registered_observation_without_case_id(self):
        request = {'kind': 'browser', 'title': '核对布局', 'owner': '执行器', 'evidence_needed': '记录实际宽度'}
        config = {'task_id': 'VERIFY', 'candidate_tree': 'a' * 64, 'url': 'https://example.test/login',
                  'expected_version': '1', 'handoff_revision': 'old', 'handoff_ids': ['1'],
                  'reuse_on_reopen': True, 'reopen_observation_digests': [candidate_checks.digest(request)]}
        item = {**request, 'id': '2'}
        changed = {**item, 'id': '3', 'evidence_needed': '记录其他页面'}
        value = {'revision': 'new', 'binding': {'task_id': 'VERIFY', 'tree': 'a' * 64},
                 'items': [item, changed]}
        rebound = browser_execution.for_handoff(config, value, 'login-routes')
        self.assertTrue(browser_execution.matches_item(rebound, value, item))
        self.assertFalse(browser_execution.matches_item(rebound, value, changed))

    def test_temporary_resume_failure_keeps_the_receipt_and_retries_after_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            store = object.__new__(TaskStore)
            value = {'revision': 'current', 'items': [{'id': '1', 'receipt': {'result': '观察已完成'}}],
                     'resume': {'status': 'pending', 'agent_calls': 7}}
            write_json(run / field_handoff.FILE, value)
            with patch.object(store, 'action', side_effect=[TaskError('稍后重试', 503), {}]) as launch:
                store._resume_field_handoff('task', run, value)
                pending = field_handoff.read(run)
                self.assertEqual(pending['items'], value['items'])
                store._resume_field_handoff('task', run, pending)
                launch.assert_called_once()
                with patch('console.tasks.time.time', return_value=pending['resume']['retry_at'] + 1):
                    self.assertTrue(store._resume_field_handoff('task', run, pending)['resuming'])
                self.assertEqual(launch.call_count, 2)

    def test_tig_session_reuses_connected_role_pages(self):
        from unittest.mock import Mock
        from auto_moniter import tig_browser_case

        origin = 'https://example.test'
        browser = Mock()
        session = tig_browser_case.BrowserSession(browser)
        for role, route in tig_browser_case.SUCCESS_PATHS.items():
            page = Mock(url=origin + route)
            session.pages[role] = (page, {'login_at': 'previous', 'reload_verified': True})
        request = {'case_id': 'login-routes', 'url': origin + '/login', 'expected_version': '1'}
        settings = {'origin': origin, 'accounts': {}, 'interactive_captcha': True}
        with (patch.object(tig_browser_case, '_state', return_value={'menus': []}),
              patch.object(tig_browser_case, '_route_checks', return_value={'navigation': []}) as checks,
              patch.object(tig_browser_case, '_login') as login):
            result = tig_browser_case.execute(request, settings, session=session)
        login.assert_not_called()
        browser.new_context.assert_not_called()
        self.assertEqual(set(checks.call_args.args[0]), set(session.pages))
        self.assertEqual(result['receipt']['version'], '1')

    def test_tig_session_waits_through_handoff_and_closes_after_completion(self):
        from auto_moniter import tig_browser_session

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            write_json(run/'state.json', {'status': 'action_required'})
            self.assertFalse(tig_browser_session.finished(run))
            write_json(run/'state.json', {'status': 'done'})
            self.assertTrue(tig_browser_session.finished(run))
            request = {'url': 'https://example.test/login', 'expected_version': '1', 'binding': {'tree': 'a'}}
            changed = {**request, 'binding': {'tree': 'b'}}
            self.assertNotEqual(tig_browser_session.address(run, request, 'settings'),
                                tig_browser_session.address(run, changed, 'settings'))

    def test_reopen_keeps_manual_browser_registration_only_for_unchanged_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            request = {'kind': 'browser', 'title': '核对返回', 'owner': '现场执行者', 'evidence_needed': '记录返回 URL'}
            item = {**request, 'id': '1', 'allow_manual': True, 'browser_proof': True,
                    'url': 'https://example.test/login', 'expected_version': '1',
                    'receipt': {'result': '已有导航记录'}}
            value = {'revision': 'original', 'binding': {'round': 39}, 'items': [item]}
            field_handoff.reopen(run, value, ['需要补证'], [
                {**request, 'evidence_needed': '记录刷新 URL'}, request,
            ])
            reopened = field_handoff.read(run)
            changed, unchanged = reopened['items']
            self.assertNotIn('allow_manual', changed)
            self.assertEqual(unchanged['id'], '2')
            self.assertTrue(unchanged['allow_manual'])
            self.assertTrue(unchanged['browser_proof'])
            self.assertEqual(unchanged['expected_version'], '1')
            self.assertNotIn('receipt', unchanged)
            self.assertEqual(reopened['retained_evidence'][0]['citation'], 'handoff:original:1')

    def test_runner_update_keeps_the_session_for_the_same_approved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            value = {'binding': {'round': 39}, 'revision': 'current'}
            item = {'id': '2', 'case_id': 'login-routes'}
            config = {'url': 'https://example.test/login', 'expected_version': '1',
                      'runner_digest': 'old', 'settings_digest': 'same'}
            job, request = browser_execution._job(run, value, item, config)
            job.mkdir(parents=True)
            write_json(job/'launch.json', {'request': request, 'runner_digest': 'old'})
            updated = {**config, 'runner_digest': 'new'}
            self.assertEqual(browser_execution._job(run, value, item, updated), (job, request))
            changed = {**updated, 'settings_digest': 'changed'}
            self.assertNotEqual(browser_execution._job(run, value, item, changed)[0], job)
            write_json(job/'result.json', {'status': 'failed'})
            self.assertNotEqual(browser_execution._job(run, value, item, updated)[0], job)

    def test_timed_out_captcha_can_retry_without_changing_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            value = {'binding': {'round': 39}, 'revision': 'current'}
            item = {'id': '2', 'case_id': 'login-routes'}
            config = {'url': 'https://example.test/login', 'expected_version': '1',
                      'interactive_captcha': True, 'argv': [sys.executable]}
            job, request = browser_execution._job(run, value, item, config)
            job.mkdir(parents=True)
            expired = {'status': 'failed', 'reason': browser_execution.CAPTCHA_TIMEOUT}
            write_json(job / 'launch.json', {'request': request, 'attempts': 1})
            write_json(job / 'result.json', expired)
            with patch.object(browser_execution.subprocess, 'Popen') as spawn:
                self.assertEqual(browser_execution.poll(run, value, item, config), expired)
                self.assertEqual(browser_execution.poll(run, value, item, config, retry_captcha=True)['status'], 'running')
                spawn.assert_called_once()
            self.assertEqual(json.loads((job / 'result-attempt-1.json').read_text()), expired)
            self.assertEqual(json.loads((job / 'launch.json').read_text())['request'], request)
            saved = {'status': 'ready'}
            write_json(job / 'result.json', saved)
            with patch.object(browser_execution.subprocess, 'Popen') as spawn:
                self.assertEqual(browser_execution.poll(run, value, item, config, retry_captcha=True), saved)
                spawn.assert_not_called()

    def test_captcha_timeout_waits_for_retry_without_launching_referee(self):
        store = object.__new__(TaskStore)
        value = {'items': [{'id': '2', 'kind': 'browser'}],
                 'failed_cases': {'2': {'retry_captcha': True}}}
        with patch.object(store, 'action') as start:
            store._route_browser_failure({'id': 'test'}, Path('.'), value, referee_enabled=True)
            start.assert_not_called()

    def test_reopened_batch_requires_same_task_candidate_and_explicit_case(self):
        config = {'task_id': 'VERIFY', 'candidate_tree': 'a' * 64, 'url': 'https://example.test/login',
                  'expected_version': '1', 'handoff_revision': 'old', 'handoff_ids': ['1'],
                  'reuse_on_reopen': True}
        item = {'id': '2', 'kind': 'browser', 'case_id': 'login-routes'}
        value = {'revision': 'new', 'binding': {'task_id': 'VERIFY', 'tree': 'a' * 64},
                 'items': [item, {'id': '3', 'kind': 'browser'}, {**item, 'id': '4', 'expected_version': '2'}]}
        rebound = browser_execution.for_handoff(config, value, 'login-routes')
        self.assertEqual(rebound['handoff_ids'], ['2'])
        self.assertTrue(browser_execution.matches_item(rebound, value, item))
        changed = {**value, 'binding': {**value['binding'], 'task_id': 'OTHER'}}
        self.assertEqual(browser_execution.for_handoff(config, changed, 'login-routes'), config)
        self.assertEqual(config['handoff_revision'], 'old')

    def test_console_routes_captcha_to_the_bound_active_or_deferred_browser(self):
        from base64 import b64encode

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, workspace = root / 'run', root / 'project'
            run.mkdir()
            workspace.mkdir()
            current = {'id': 'LOGIN', 'kind': 'verification'}
            deferred = {'id': 'RETURNS', 'kind': 'verification'}
            tree = workspace_fingerprint(workspace, ignore_dir=run)
            rounds = []
            for index, task in enumerate([current, deferred], 8):
                receipt = candidate_checks.create(run, {'commands': [], 'task': task}, {}, True)
                rounds.extend([
                    {'role': 'worker', 'index': index, 'tree': tree, 'watcher_result': {'candidate_checks': receipt}},
                    {'role': 'reviewer', 'index': index, 'task_status': 'changes_required',
                     'blockers': ['浏览器验证'], 'watcher_result': {'status': 'blocked'}},
                ])
            state = {'cwd': str(workspace), 'rounds': rounds, 'function_plan': {'tasks': [current, deferred]},
                     'function_tasks': {'current': current}}
            write_json(run / 'state.json', state)
            png = b'\x89PNG\r\n\x1a\n'
            item = {'id': '1', 'kind': 'browser', 'title': '核对返回入口', 'case_id': 'login-routes'}
            active = {'binding': field_handoff.binding(state), 'revision': 'a' * 32, 'items': [item],
                      'browser_inputs': {'active': {'id': '1', 'kind': 'image', 'nonce': 'active',
                                                    'expires': time.time() + 30, 'image': b64encode(png).decode()}}}
            write_json(run / field_handoff.FILE, active)
            checkpoint = {**state, 'function_tasks': {'current': deferred}}
            prior = {'binding': field_handoff.binding(checkpoint), 'revision': 'b' * 32, 'items': [item]}
            filename = 'field-handoff-' + prior['revision'] + '.json'
            write_json(run / filename, prior)
            config = {'url': 'https://example.test/login', 'expected_version': '1',
                      'interactive_captcha': True, 'task_id': deferred['id'], 'candidate_tree': tree}
            job, _ = browser_execution._job(run, prior, item, config)
            job.mkdir(parents=True)
            challenge = {'kind': 'image', 'nonce': 'deferred', 'expires': time.time() + 30,
                         'image': b64encode(png).decode()}
            write_json(job / 'captcha.json', challenge)
            meta = {'id': 'task', 'cwd': str(workspace), 'run_dir': str(run)}
            store = object.__new__(TaskStore)
            store.data_dir = root
            store._lock = RLock()
            with (patch.object(store, '_get', return_value=meta),
                  patch.object(store, '_view', return_value={'status': 'action_required'}) as view,
                  patch.object(browser_execution, 'cases', return_value={'login-routes': config}),
                  (job / 'execution.lock').open('a') as executor):
                fcntl.flock(executor, fcntl.LOCK_EX)
                store._collect_field_results(meta, run, checkpoint, prior, filename=filename)
                self.assertEqual(store.browser_captcha_image('task', 'active'), png)
                view.return_value = {'status': 'running'}
                self.assertEqual(store.browser_captcha_image('task', 'deferred', revision=prior['revision']), png)
                displayed = browser_captcha.presentation(run, state, 'running')
                self.assertEqual([row['revision'] for row in displayed], [prior['revision']])
                self.assertNotIn('image', displayed[0]['browser_inputs']['deferred'])
                for nonce, revision in (('active', None), ('deferred', active['revision']), ('unknown', None)):
                    with self.assertRaises(TaskError) as error:
                        store.browser_captcha_image('task', nonce, revision=revision)
                    self.assertEqual(error.exception.status, 404)
                request = {'action': 'browser_captcha', 'id': '1', 'nonce': 'deferred', 'code': 'A123',
                           'binding': prior['binding'], 'revision': prior['revision']}
                with self.assertRaises(TaskError):
                    store.field_handoff('task', {**request, 'binding': active['binding']})
                self.assertEqual(store.field_handoff('task', request), {'submitted': True})
                self.assertEqual(json.loads((job / 'captcha-answer.json').read_text())['nonce'], 'deferred')
                self.assertEqual(field_handoff.read(run), active)
                saved = field_handoff.read(run, filename)
                saved['browser_inputs']['deferred']['expires'] = 0
                saved['failed_cases'] = {'1': {'retry_captcha': True}}
                write_json(run / filename, saved)
                with self.assertRaises(TaskError):
                    store.browser_captcha_image('task', 'deferred')
                retry = {**request, 'action': 'retry_browser_captcha'}
                with patch.object(browser_execution, 'poll', return_value={'status': 'running'}) as execute:
                    self.assertEqual(store.field_handoff('task', retry), {'status': 'running'})
                    execute.assert_called_once_with(run, saved, saved['items'][0], config, retry_captcha=True)
                write_json(run / 'control.json', {'action': 'pause'})
                self.assertEqual(browser_captcha.presentation(run, state, 'running'), [])
                with self.assertRaises(TaskError):
                    store.field_handoff('task', retry)
                (run / 'control.json').unlink()
                (workspace / 'app.py').write_text('pass\n')
                self.assertEqual(browser_captcha.presentation(run, state, 'running'), [])
                with self.assertRaises(TaskError):
                    store.field_handoff('task', retry)

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
                     'items':[{'id':'1','kind':'browser','case_id':'role-login'},
                              {'id':'2','kind':'observation'}],
                     'executor_status':{'2':'等待原站候选回执'},
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
            self.assertEqual(current['fallback']['waiting'], {'2':'等待原站候选回执'})

    def test_unassigned_deployment_routes_to_referee_without_starting_dependent_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            value = {'binding': {'round': 8}, 'revision': 'current', 'items': [
                {'id': '1', 'kind': 'deployment'},
                {'id': '2', 'kind': 'browser', 'case_id': 'role-login'},
            ]}
            store = object.__new__(TaskStore)
            with patch.object(browser_execution, 'poll') as poll, patch.object(store, 'action') as launch:
                current = store._collect_field_results({'id': 't'}, run, {}, value)
                self.assertEqual(list(current['failed_cases']), ['1'])
                self.assertEqual(current['failed_cases']['1']['kind'], 'deployment')
                store._route_browser_failure({'id': 't'}, run, current, referee_enabled=True)
                poll.assert_not_called()
                launch.assert_called_once_with('t', 'resume')
            fallback = field_handoff.read(run)['fallback']
            self.assertEqual(fallback['role'], 'referee')
            self.assertEqual(fallback['waiting'], {'2': '等待同一候选的部署回执。'})

    def test_fallback_retries_temporary_launch_failure_before_marking_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            value = {'binding': {'round': 8}, 'revision': 'current', 'items': [{'id': '1', 'kind': 'deployment'}],
                     'failed_cases': {'1': {'case_id': '', 'kind': 'deployment', 'reason': '缺少执行器', 'role': 'reviewer'}}}
            store = object.__new__(TaskStore)
            with patch('console.tasks.time.time', return_value=100) as clock, patch.object(
                    store, 'action', side_effect=[
                        TaskError('该项目已有任务执行中', 409, retryable=True), {}]) as launch:
                store._route_browser_failure({'id': 't'}, run, value, referee_enabled=True)
                retry = field_handoff.read(run)
                self.assertEqual(retry['fallback']['status'], 'pending')
                self.assertFalse(retry.get('fallback_history'))
                store._route_browser_failure({'id': 't'}, run, retry, referee_enabled=True)
                launch.assert_called_once()
                clock.return_value = 115
                store._route_browser_failure({'id': 't'}, run, retry, referee_enabled=True)
                launched = field_handoff.read(run)
                store._route_browser_failure({'id': 't'}, run, launched, referee_enabled=True)
                self.assertEqual(launch.call_count, 2)
                self.assertEqual(launched['fallback']['status'], 'launched')
                self.assertFalse(launched.get('fallback_history'))
                clock.return_value = launched['fallback']['retry_at']
                launch.side_effect = None
                store._route_browser_failure({'id': 't'}, run, launched, referee_enabled=True)
                self.assertEqual(launch.call_count, 3)
                field_handoff.finish_fallback(run, launched['fallback'])
                handled = field_handoff.read(run)
                store._route_browser_failure({'id': 't'}, run, handled, referee_enabled=True)
                self.assertEqual(launch.call_count, 3)
            self.assertEqual(handled['fallback']['status'], 'handled')
            self.assertEqual(handled['fallback_history'], [launched['fallback']['key']])

    def test_enabled_referee_handles_failure_previously_routed_to_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            value = {'binding': {'round': 8}, 'revision': 'current',
                     'items': [{'id': '1', 'kind': 'browser'}],
                     'failed_cases': {'1': {'case_id': 'role-login', 'reason': '未完成', 'role': 'reviewer'}}}
            write_json(run / field_handoff.FILE, value)
            store = object.__new__(TaskStore)
            with patch.object(store, 'action') as start:
                store._route_browser_failure({'id': 't'}, run, value)
                previous = field_handoff.read(run)
                self.assertEqual(previous['fallback']['role'], 'reviewer')
                store._route_browser_failure({'id': 't'}, run, previous, referee_enabled=True)
                current = field_handoff.read(run)
                self.assertEqual(current['fallback']['role'], 'referee')
                store._route_browser_failure({'id': 't'}, run, current, referee_enabled=True)
                self.assertEqual(start.call_count, 2)

    def test_browser_receipt_links_raw_observations_to_the_bound_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            request = {'binding': {'round': 39}, 'revision': 'r', 'id': '2', 'expected_version': '1'}
            output = {'receipt': {'version': '1', 'session_reference': 'session', 'result': '已观察'},
                      'observations': {'desktop': {'1024': {'last_url': '/example'}}}}
            write_json(job/'launch.json', {'request': request, 'argv': [sys.executable, '-c', f'print({json.dumps(output)!r})']})
            browser_execution.run_case(job)
            result = json.loads((job/'result.json').read_text())
            raw = json.loads((job/'observations.json').read_text())
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(raw['request'], request)
            self.assertEqual(raw['observations'], output['observations'])
            self.assertIn(raw['observations_sha256'], result['submission']['receipt']['result'])

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
