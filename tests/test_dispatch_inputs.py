import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auto_moniter import dispatch_inputs, field_handoff
from auto_moniter.directions import Directions
from auto_moniter.quality import policy
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.supervisor_checks import AcceptanceChecks
from auto_moniter.supervisor_planning import RequirementPlanning
from console.tasks import TaskError, TaskStore
from tests.test_function_dispatch import CHECK, FunctionWorker, result, verification_task
from tests.test_referee import final_task, planning
from tests.test_supervisor import ScriptedProvider


class DispatchInputTests(unittest.TestCase):
    def test_provisioned_input_resumes_only_the_matching_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            run = root / 'run'
            run.mkdir()
            task = {'id': 'APP-DEVICE'}
            quality = policy({'assignment_inputs': {'APP-DEVICE': ['device/result.json']}})
            gate = AcceptanceChecks()
            gate.run_dir = run
            gate._dispatch_directions = []
            gate.config = SimpleNamespace(cwd=root, checks=[], quality_policy=quality)
            missing = gate._missing_dispatch_inputs(task)
            self.assertEqual(missing, [('APP-DEVICE', 'device/result.json')])
            summary = gate._wait_for_dispatch_inputs(task, missing)
            state = {'status': 'action_required', 'summary': summary, 'cwd': str(root),
                     'function_tasks': {'current': task}, 'checks': [], 'quality_policy': quality,
                     'dispatch_directions': []}
            write_json(run / 'state.json', state)
            store = TaskStore(root / 'console', root)
            store._catalog = lambda: {'task': {'managed': True, 'id': 'task', 'run_dir': str(run), 'cwd': str(root)}}
            store._view = lambda *args, **kwargs: {'status': 'action_required', 'actions': ['resume']}
            with patch.object(store, 'action') as action:
                store.recover_field_handoffs()
                action.assert_not_called()
                (root / 'device').mkdir()
                (root / 'device/result.json').write_text('{}')
                store.recover_field_handoffs()
                action.assert_called_once_with('task', 'resume')
                action.reset_mock()
                Directions(run).append('改用另一台设备')
                store.recover_field_handoffs()
                action.assert_not_called()
            wait = json.loads((run / dispatch_inputs.WAIT_FILE).read_text())
            self.assertFalse(dispatch_inputs.ready(wait, {**state, 'checks': ['changed']}, root, []))

    def test_observation_without_a_browser_can_route_to_a_referee(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            value = {'binding': {'round': 3}, 'revision': 'r', 'items': [
                {'id': '1', 'kind': 'observation'},
                {'id': '2', 'kind': 'observation', 'case_id': field_handoff.CHECK_ARCHIVE_CASE},
                {'id': '3', 'kind': 'observation', 'claim': {'name': '设备测试员'}},
            ]}
            store = object.__new__(TaskStore)
            store.action = Mock()
            updated = store._collect_field_results({}, run, {}, value)
            self.assertEqual(list(updated['failed_cases']), ['1'])
            store._route_browser_failure({'id': 'app'}, run, updated, referee_enabled=True)
            store.action.assert_called_once_with('app', 'resume')
            self.assertEqual(field_handoff.read(run)['fallback']['role'], 'referee')

    def test_replacement_of_a_blocked_verification_keeps_scope_and_checks(self):
        planning = RequirementPlanning()
        current = {**verification_task('OLD'), 'delivery_scope': 'function', 'criterion_ids': ['main:1']}
        planning.function_tasks = SimpleNamespace(current=current)
        planning.config = SimpleNamespace(quality_policy={})
        planning._verification_only = lambda: True
        planning._unplanned_directions = lambda: True
        planning._records = [SimpleNamespace(role='reviewer', verdict=SimpleNamespace(task_id='OLD'),
                                             watcher_result={'status': 'blocked'})]
        replacement = {**current, 'id': 'NEW', 'verification': ['使用已授权的客户端验证返回结果。']}
        self.assertTrue(planning._amended_verification_replacement(replacement))
        self.assertFalse(planning._amended_verification_replacement(current))
        self.assertFalse(planning._amended_verification_replacement({**replacement, 'check_ids': ['check:2']}))
        self.assertTrue(planning._amended_verification_replacement({**replacement, 'file': 'other.py'}))
        self.assertFalse(planning._amended_verification_replacement({**replacement, 'delivery_scope': 'module'}))
        module = {**current, 'delivery_scope': 'module'}
        planning.function_tasks.current = module
        planning.function_plan = {'tasks': [module, replacement]}
        self.assertTrue(planning._amended_verification_replacement(replacement, [replacement, module]))
        self.assertTrue(planning._amended_verification_replacement({**replacement, 'delivery_scope': 'module'}))
        planning.function_tasks.current = current
        planning.config.quality_policy = {'assignment_inputs': {'OLD': ['device/result.json']}}
        self.assertFalse(planning._amended_verification_replacement(replacement))
        planning.config.quality_policy['assignment_inputs']['NEW'] = ['device/result.json']
        self.assertTrue(planning._amended_verification_replacement(replacement))
        planning._unplanned_directions = lambda: False
        self.assertFalse(planning._amended_verification_replacement(replacement))

    @unittest.skipUnless(shutil.which('ssh-keygen', path=os.defpath), 'OpenSSH signature verification requires ssh-keygen')
    def test_device_receipt_requires_approved_signature_current_candidate_and_freshness(self):
        import time
        from auto_moniter import readiness_proof
        from auto_moniter.workspace import workspace_fingerprint

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as trusted:
            root, trust = Path(tmp).resolve(), Path(trusted)
            (root / 'app.py').write_text('pass')
            (root / '.auto-moniter').mkdir()
            spec = {'path': '.auto-moniter/device.json', 'environment': 'android-35',
                    'executor': 'android-device', 'max_age_s': 30}
            policy({'assignment_inputs': {'APP-DEVICE': [spec]}})
            payload = {'candidate_tree': workspace_fingerprint(root), 'environment': 'android-35',
                       'executor': 'android-device', 'ready': True, 'observed_at': int(time.time()),
                       'workspace': str(root), 'path': spec['path'], 'resource': 'test-device-1'}
            key = trust / 'executor-key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            public = ' '.join(key.with_suffix('.pub').read_text().split()[:2])
            registration = {field: payload[field] for field in ('workspace', 'environment', 'executor', 'resource')}
            write_json(trust / readiness_proof.CONFIG, {'executors': [{**registration, 'public_key': public}]})

            def sign(receipt):
                signature = subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(key), '-n', readiness_proof.NAMESPACE],
                    input=readiness_proof.signing_bytes(receipt), check=True, capture_output=True).stdout.decode()
                return {**receipt, 'signature': signature}

            path = root / spec['path']
            with patch.dict('os.environ', {readiness_proof.ENV: str(trust)}):
                write_json(path, payload)
                self.assertEqual(dispatch_inputs.missing(root, [spec]), [spec])
                for invalid in ({'ready': False}, {'candidate_tree': 'old'}, {'environment': 'other'},
                                {'executor': 'wrong'}, {'observed_at': int(time.time()) - 32},
                                {'resource': 'unregistered'}, {'workspace': '/other'}, {'path': 'different.json'}):
                    write_json(path, sign({**payload, **invalid}))
                    self.assertEqual(dispatch_inputs.missing(root, [spec]), [spec], invalid)
                signed = sign(payload)
                write_json(path, {**signed, 'observed_at': payload['observed_at'] - 1})
                self.assertEqual(dispatch_inputs.missing(root, [spec]), [spec])
                write_json(path, signed)
                self.assertFalse(dispatch_inputs.missing(root, [spec]))
                (root / 'app.py').write_text('different = True')
                self.assertEqual(dispatch_inputs.missing(root, [spec]), [spec])

    def test_explicit_dependencies_only_wait_for_their_named_prerequisites(self):
        value = {'items': [{'id': '1', 'kind': 'deployment'},
                           {'id': '2', 'kind': 'browser', 'depends_on': []},
                           {'id': '3', 'kind': 'observation', 'depends_on': ['1']}]}
        self.assertEqual(field_handoff.dependency_error(value, value['items'][1]), '')
        self.assertIn('1', field_handoff.dependency_error(value, value['items'][2]))
        self.assertFalse(field_handoff.valid_dependencies(['3'], 2))
        self.assertFalse(field_handoff.valid_dependencies(['1', '1'], 3))
        value['items'][0]['receipt'] = {'version': 'current'}
        self.assertEqual(field_handoff.dependency_error(value, value['items'][2]), '')

    def test_changed_candidate_waits_for_fresh_preparation_before_running_checks(self):
        from auto_moniter.workspace import workspace_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            (root / 'app.py').write_text('def parse_input(): return 0\n')
            source_tree = workspace_fingerprint(root, ignore_dir=run)
            spec = {'path': '.auto-moniter/device.json', 'environment': 'android', 'executor': 'device', 'max_age_s': 30}
            settings = dict(run_dir=run, git_history='off', retry_delay_s=0, workflow='review-first',
                            function_dispatch=True, full_function_plan=True, checks=[CHECK],
                            quality_policy={'assignment_inputs': {'F1': [spec]}})
            ready = {source_tree}

            def missing(workspace, inputs, *, run):
                return [] if workspace_fingerprint(workspace, ignore_dir=run) in ready else inputs

            worker = FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [planning(final_task())])
            with patch('auto_moniter.dispatch_inputs.missing', side_effect=missing):
                first = Supervisor(RunConfig('Deliver application', root, worker, reviewer=reviewer, **settings),
                                   log=lambda _: None).run()
                self.assertEqual(first.status, 'action_required', first.summary)
                self.assertFalse((run / '001-check-01.log').exists())
                self.assertEqual(len(reviewer.calls), 1)
                updated = workspace_fingerprint(root, ignore_dir=run)
                self.assertNotEqual(updated, source_tree)
                ready.add(updated)
                worker = FunctionWorker('worker', [])
                reviewer = ScriptedProvider('reviewer', [result('F1', 'accepted', status='done')])
                resumed = Supervisor(RunConfig('Deliver application', root, worker, reviewer=reviewer, resume=True,
                                               **settings), log=lambda _: None).run()
            self.assertEqual(resumed.status, 'done', resumed.summary)
            self.assertFalse(worker.calls)

    def test_readiness_expiring_during_review_preserves_candidate_without_accepting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            (root / 'app.py').write_text('def parse_input(): return 0\n')
            spec = {'path': '.auto-moniter/device.json', 'environment': 'android', 'executor': 'device', 'max_age_s': 30}
            settings = dict(run_dir=run, git_history='off', retry_delay_s=0, workflow='review-first',
                            function_dispatch=True, full_function_plan=True, checks=[CHECK],
                            quality_policy={'assignment_inputs': {'F1': [spec]}})
            worker = FunctionWorker('worker', [{**result('F1', 'done', status='done'), 'touch': 'app.py'}])
            reviewer = ScriptedProvider('reviewer', [planning(final_task()), result('F1', 'accepted', status='done')])

            def missing(workspace, inputs, *, run):
                return inputs if len(reviewer.calls) > 1 else []

            with patch('auto_moniter.dispatch_inputs.missing', side_effect=missing):
                first = Supervisor(RunConfig('Deliver application', root, worker, reviewer=reviewer, **settings),
                                   log=lambda _: None).run()
            self.assertEqual(first.status, 'action_required', first.summary)
            self.assertEqual(json.loads((run / 'recovery-wait.json').read_text())['condition'], 'dispatch_inputs')
            self.assertFalse(any(record.delivery.get('status') == 'accepted' for record in first.records))
            worker = FunctionWorker('worker', [])
            reviewer = ScriptedProvider('reviewer', [result('F1', 'accepted', status='done')])
            with patch('auto_moniter.dispatch_inputs.missing', return_value=[]):
                resumed = Supervisor(RunConfig('Deliver application', root, worker, reviewer=reviewer, resume=True,
                                               **settings), log=lambda _: None).run()
            self.assertEqual(resumed.status, 'done', resumed.summary)
            self.assertFalse(worker.calls)

    def test_registered_executor_waits_for_device_and_then_binds(self):
        from auto_moniter import field_executors

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            parent = {'id': 'parent', 'cwd': str(run / 'project')}
            first = {'id': 'a' * 32, 'managed': True, 'run_dir': str(run / 'first')}
            second = {'id': 'b' * 32, 'managed': True, 'run_dir': str(run / 'second')}
            for child in (first, second):
                Path(child['run_dir']).mkdir()
            write_json(Path(first['run_dir']) / field_handoff.EXECUTOR_REQUEST,
                       {'item': {'executor_resource': 'android-35'}})
            write_json(run / field_executors.CONFIG, {'executors': [
                {'task_id': second['id'], 'workspace': parent['cwd'], 'kind': 'observation',
                 'capability': 'android-ui', 'resource': 'android-35'}]})
            store = Mock(data_dir=run)
            store._catalog.return_value = {row['id']: row for row in (first, second)}
            store._validate_handoff_task.return_value = second
            statuses = {first['id']: 'running', second['id']: 'queued'}
            store._view.side_effect = lambda child, *args, **kwargs: {'status': statuses[child['id']]}
            value = {'items': [{'id': '1', 'kind': 'observation', 'capability': 'android-ui'}]}
            self.assertIn('1', field_executors.bind(store, parent, run, value))
            self.assertNotIn('executor_task', value['items'][0])
            statuses[first['id']] = 'done'
            self.assertFalse(field_executors.bind(store, parent, run, value))
            self.assertEqual(field_handoff.read(run)['items'][0]['executor_task'], second['id'])

    def test_failed_browser_executor_is_replaced_after_process_release(self):
        from auto_moniter import field_executors
        from auto_moniter.workspace import file_lock

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            parent = {'id': 'parent', 'cwd': str(run / 'project')}
            old = {'id': 'a' * 32, 'managed': True, 'run_dir': str(run / 'old')}
            backup = {'id': 'b' * 32, 'managed': True, 'run_dir': str(run / 'backup')}
            original = {'id': '1', 'kind': 'browser', 'capability': 'login-check',
                        'executor_task': old['id'], 'executor_resource': 'chrome'}
            value = {'binding': {'tree': 'candidate'}, 'revision': 'r', 'items': [original]}
            field_handoff.prepare_executor(Path(old['run_dir']), parent['id'], value, original)
            write_json(run / field_executors.CONFIG, {'executors': [{
                'task_id': backup['id'], 'workspace': parent['cwd'], 'kind': 'browser',
                'capability': 'login-check', 'resource': 'chrome'}]})
            store = object.__new__(TaskStore)
            store.data_dir = run
            children = {child['id']: child for child in (old, backup)}
            store._catalog = lambda: children
            store._validate_handoff_task = lambda parent, ident, **_: children[ident]
            store._view = lambda child, *args, **kwargs: (
                {'status': 'failed', 'summary': 'Chrome 提前退出。'} if child == old else {'status': 'queued'})
            value = store._collect_field_results(parent, run, {}, value)
            self.assertEqual(value['failed_cases']['1']['executor_task'], old['id'])
            process = Path(old['run_dir']) / 'process.json'
            write_json(process, {'pid': 123})
            field_executors.bind(store, parent, run, value)
            self.assertEqual(original['executor_task'], old['id'])
            process.unlink()
            with file_lock(Path(old['run_dir']) / 'supervisor.lock'):
                field_executors.bind(store, parent, run, value)
                self.assertEqual(original['executor_task'], old['id'])
            field_executors.bind(store, parent, run, value)
            self.assertEqual(original['executor_task'], backup['id'])
            self.assertEqual(original['executor_failures'][0]['binding'], value['binding'])
            self.assertFalse(value['failed_cases'])
            store._get = lambda _: {'run_dir': str(run)}
            old_request = Directions(Path(old['run_dir'])).read(field_handoff.EXECUTOR_REQUEST)
            self.assertFalse(field_executors.request_current(store, old['id'], old_request))

    def test_terminal_preflight_failure_releases_queued_executor_for_backup(self):
        from auto_moniter import field_executors

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            run = data / 'parent'
            run.mkdir()
            parent = {'id': 'parent', 'run_dir': str(run), 'cwd': str(data / 'project')}
            children = [{'id': letter * 32, 'managed': True, 'run_dir': str(data / letter)} for letter in 'ab']
            catalog = {child['id']: child for child in children}
            write_json(data / field_executors.CONFIG, {'executors': [
                {'task_id': child['id'], 'workspace': parent['cwd'], 'kind': 'browser',
                 'capability': 'login-check', 'resource': 'chrome'} for child in children]})
            store = object.__new__(TaskStore)
            store.data_dir = data
            store._catalog = lambda: catalog
            store._validate_handoff_task = lambda parent, ident, **kwargs: catalog[ident]
            store._view = Mock(return_value={'status': 'queued'})
            store.action = Mock(side_effect=[TaskError('启动配置无效。', 409), {}])
            value = {'binding': {'tree': 'candidate'}, 'revision': 'r',
                     'items': [{'id': '1', 'kind': 'browser', 'capability': 'login-check'}]}
            field_executors.bind(store, parent, run, value)
            store._dispatch_handoff_tasks(parent, value)
            value = store._collect_field_results(parent, run, {}, field_handoff.read(run))
            field_executors.bind(store, parent, run, value)
            store._dispatch_handoff_tasks(parent, value)
            self.assertEqual([call.args[0] for call in store.action.call_args_list], list(catalog))
            self.assertEqual(field_handoff.read(run)['items'][0]['executor_failures'][0]['reason'], '启动配置无效。')
