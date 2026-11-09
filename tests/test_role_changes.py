"""Task role overrides must preserve checkpoint policy and isolate sessions."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from auto_moniter.cli import build_parser
from auto_moniter.providers import resolve_provider
from auto_moniter.providers import _which
from auto_moniter.role_changes import authorized_change
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.roles import prepare
from console.tasks import TaskStore, TaskError


class RoleChangeTests(unittest.TestCase):
    def setUp(self):
        # Pin outside the project so a watcher TMPDIR nested under a locked run
        # does not make workspace_lease treat fixtures as the live project.
        self.tmp = tempfile.TemporaryDirectory(prefix='am-role-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # These tests validate configuration/checkpoints, never model execution.
        # A fail-fast executable satisfies real preflight without depending on
        # locally installed AI CLIs or silently invoking a paid model.
        fixture = self.root / 'offline-agent'
        fixture.write_text('#!/bin/sh\necho "Unexpected model execution in role fixture" >&2\nexit 97\n')
        fixture.chmod(0o700)
        discovery = patch('auto_moniter.providers._which',
                          side_effect=lambda name: str(fixture) if name in {'codex', 'grok', 'claude'} else _which(name))
        discovery.start()
        self.addCleanup(discovery.stop)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.argv = ['run', '--cwd', str(self.root), '--run-dir', str(self.run), '--provider', 'codex',
                     '--reviewer', 'codex', '--model=old', '--reviewer-model=old-review',
                     '--review-isolation=strict', '--check=python -m unittest', '--max-runtime=7200',
                     '--max-agent-calls=32', '--mode=full-access', '--reviewer-mode=read-only']

    def test_provider_specific_modes_reset_and_policy_is_preserved(self):
        args, values, config = prepare(self.argv, {'provider':'grok', 'model':'grok-4.5',
                      'reviewer_model':'gpt-6.1-sol'}, self.root)
        parsed = build_parser().parse_args(args)
        self.assertEqual((parsed.provider, parsed.reviewer, parsed.mode, parsed.reviewer_mode),
                         ('grok', 'codex', '', 'read-only'))
        self.assertEqual(parsed.check, ['python -m unittest'])
        self.assertEqual((parsed.max_runtime, parsed.max_agent_calls, parsed.review_isolation), ('7200',32,'strict'))
        self.assertEqual(config['reviewer']['mode'], 'read-only')

    def test_rejects_policy_changes_daemon_and_backend_arguments(self):
        for data, args in [({'review_isolation':'provider'},self.argv), ({'provider':'paseo'},self.argv),
                           ({'provider':'grok'},self.argv+['--extra-arg=--force'])]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                prepare(args,data,self.root)

    def monitor(self, provider='codex', **kw):
        return Supervisor(RunConfig('Original requirement',self.root,resolve_provider(provider),
            reviewer=resolve_provider('codex',role='reviewer'),run_dir=self.run,git_history='off',
            model='grok-4.5' if provider=='grok' else 'old',reviewer_model='gpt-6.1-sol',**kw),log=lambda _:None)

    def checkpoint(self):
        first=self.monitor();first.request_stop();self.assertEqual(first.run().status,'stopped')
        state=json.loads((self.run/'state.json').read_text())
        state.update(session_id='old-worker',reviewer_session_id='old-reviewer',active_reviewer_session_id='old-active')
        write_json(self.run/'state.json',state)
        return state

    def save_stopped_policy(self, monitor):
        # Test saved strict policy and authorization, not OS namespace support.
        # No role may execute while constructing this stopped checkpoint.
        with patch('auto_moniter.supervisor.ReviewIsolation') as boundary:
            boundary.return_value.wrap.side_effect = AssertionError('A stopped policy fixture must not execute an agent')
            monitor.request_stop()
            self.assertEqual(monitor.run().status, 'stopped')

    def test_switch_requires_exact_authorization_and_discards_old_sessions(self):
        state=self.checkpoint();new=self.monitor('grok',resume=True)
        with self.assertRaisesRegex(ValueError,'providers'):
            new._load_resume()
        assignment={'id':'test-assignment','from':state['provider_config'],'to':new._provider_config(),
                    'models':{'worker':'grok-4.5','reviewer':'gpt-6.1-sol'}}
        write_json(self.run/'role-change.json',assignment)
        new._load_resume()
        self.assertEqual((new.worker_session_id,new.reviewer_session_id,new._active_reviewer_id),('','',''))
        self.assertEqual(new._role_assignment_id,'test-assignment')
        changed={**assignment['to'],'review_isolation':'strict'}
        write_json(self.run/'role-change.json',{**assignment,'to':changed})
        self.assertIsNone(authorized_change(self.run,state['provider_config'],changed))

    def test_applied_assignment_does_not_discard_new_sessions_on_next_resume(self):
        state=self.checkpoint();new=self.monitor(resume=True)
        assignment={'id':'already-applied','from':state['provider_config'],'to':new._provider_config(),
                    'models':{'worker':'old','reviewer':'gpt-6.1-sol'}}
        write_json(self.run/'role-change.json',assignment)
        state['role_assignment_id']='already-applied';write_json(self.run/'state.json',state)
        new._load_resume();self.assertEqual(new.worker_session_id,'old-worker')

    def test_different_providers_do_not_share_model_names(self):
        monitor=self.monitor('grok');monitor.config.reviewer_model=''
        self.assertEqual(monitor._reviewer_model(),'')
        monitor.config.provider=resolve_provider('codex')
        self.assertEqual(monitor._reviewer_model(),'grok-4.5')

    def test_queued_task_override_does_not_change_other_tasks_or_checks(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        with patch.dict(os.environ,{'AUTO_MONITER_STATE_DIR':str(self.root/'state')}):
            store=TaskStore(self.root/'data',self.root)
            payload={'title':'Temporary roles','requirement':'Original requirement','provider':'codex','reviewer':'codex',
                     'checks':['python -m unittest'],'workflow':'legacy'}
            task=store.create(payload);other=store.create(payload)
            with patch('console.tasks.discover_providers',return_value={'codex':'codex','grok':'grok'}):
                updated=store.action(task['id'],'roles',{'provider':'grok','model':'grok-4.5','reviewer_model':'gpt-6.1-sol'})
            self.assertEqual(updated['provider'],'grok')
            self.assertEqual(updated['checks'],task['checks'])
            self.assertEqual(updated['status'],'queued')
            self.assertEqual(store.detail(other['id'])['provider'],'codex')
            self.assertEqual(len(updated['role_changes']),1)
            store.action(task['id'],'cancel')
            with self.assertRaises(TaskError):store.action(task['id'],'roles',{'provider':'codex'})

    def test_stopped_task_override_preserves_checkpoint_and_resume_controls(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        with patch.dict(os.environ,{'AUTO_MONITER_STATE_DIR':str(self.root/'state')}):
            store=TaskStore(self.root/'data',self.root)
            task=store.create({'title':'Resume roles','requirement':'Original requirement',
                               'provider':'codex','reviewer':'codex','workflow':'legacy',
                               'checks':['python -m unittest'],'max_agent_calls':32})
            meta=store._get(task['id']);args=build_parser().parse_args(store._new_args(meta))
            run=Path(meta['run_dir']);run.mkdir(parents=True,exist_ok=True)
            monitor=Supervisor(RunConfig('Original requirement',self.root,resolve_provider('codex'),
                reviewer=resolve_provider('codex'),run_dir=run,git_history='off',
                checks=args.check,review_isolation=args.review_isolation,worker_isolation=args.worker_isolation,
                protect_paths=args.protect,workflow='legacy'),log=lambda _:None)
            self.save_stopped_policy(monitor)
            before=(run/'state.json').read_bytes()
            with patch('console.tasks.discover_providers',return_value={'codex':'codex','grok':'grok'}):
                changed=store.action(task['id'],'roles',{'provider':'grok','model':'grok-4.5',
                                                       'reviewer_model':'gpt-6.1-sol'})
            self.assertEqual((run/'state.json').read_bytes(),before)
            self.assertIn('resume',changed['actions'])
            resumed=build_parser().parse_args(store._resume_args(store._get(task['id'])))
            self.assertEqual((resumed.provider,resumed.reviewer,resumed.max_agent_calls),('grok','codex',32))
            self.assertEqual(json.loads(before)['checks'],args.check)

    def _strip_isolation_defaults(self, run):
        state = json.loads((run / 'state.json').read_text())
        config = dict(state['provider_config'])
        for key in ('review_isolation', 'worker_isolation', 'protect_paths'):
            config.pop(key, None)
        state['provider_config'] = config
        write_json(run / 'state.json', state)
        return state

    def test_legacy_checkpoint_missing_isolation_defaults_still_authorizes(self):
        """role-change.from must match resume setdefaults for old provider_config shapes."""
        state = self.checkpoint()
        state = self._strip_isolation_defaults(self.run)
        self.assertNotIn('review_isolation', state['provider_config'])
        # Keep isolation defaults identical to the legacy checkpoint after setdefault.
        new = self.monitor('grok', resume=True)
        target = new._provider_config()
        # Raw from without defaults must still authorize after normalize (bug that blocked resume).
        assignment = {'id': 'legacy-defaults', 'from': state['provider_config'], 'to': target,
                      'models': {'worker': 'grok-4.5', 'reviewer': 'gpt-6.1-sol'}}
        write_json(self.run / 'role-change.json', assignment)
        matched = authorized_change(self.run, state['provider_config'], target)
        self.assertIsNotNone(matched)
        self.assertEqual(matched['id'], 'legacy-defaults')
        new._load_resume()
        self.assertEqual(new._role_assignment_id, 'legacy-defaults')
        self.assertEqual((new.worker_session_id, new.reviewer_session_id), ('', ''))

    def test_stopped_role_change_writes_normalized_from_and_survives_save_failure(self):
        """Second pending override must not leave a contradictory role-change if meta save fails."""
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        with patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')}):
            store = TaskStore(self.root / 'data', self.root)
            task = store.create({'title': 'Pending roles', 'requirement': 'Original requirement',
                                 'provider': 'codex', 'reviewer': 'codex', 'workflow': 'legacy',
                                 'checks': ['python -m unittest'], 'max_agent_calls': 32,
                                 'review_isolation': 'strict'})
            meta = store._get(task['id'])
            args = build_parser().parse_args(store._new_args(meta))
            run = Path(meta['run_dir'])
            run.mkdir(parents=True, exist_ok=True)
            monitor = Supervisor(RunConfig('Original requirement', self.root, resolve_provider('codex'),
                reviewer=resolve_provider('codex'), run_dir=run, git_history='off',
                checks=args.check, review_isolation=args.review_isolation,
                worker_isolation=args.worker_isolation, protect_paths=args.protect,
                workflow='legacy'), log=lambda _: None)
            self.save_stopped_policy(monitor)
            state = json.loads((run / 'state.json').read_text())
            expected_from = {
                'review_isolation': state['provider_config']['review_isolation'],
                'worker_isolation': state['provider_config']['worker_isolation'],
                'protect_paths': list(state['provider_config']['protect_paths']),
            }
            providers = {'codex': 'codex', 'grok': 'grok', 'claude': 'claude'}
            with patch('console.tasks.discover_providers', return_value=providers):
                first = store.action(task['id'], 'roles', {
                    'provider': 'grok', 'model': 'grok-4.5', 'reviewer_model': 'gpt-6.1-sol'})
            self.assertEqual(first['provider'], 'grok')
            first_auth = (run / 'role-change.json').read_bytes()
            first_from = json.loads(first_auth)['from']
            self.assertEqual(first_from.get('review_isolation'), expected_from['review_isolation'])
            self.assertEqual(first_from.get('worker_isolation'), expected_from['worker_isolation'])
            self.assertEqual(first_from.get('protect_paths'), expected_from['protect_paths'])
            resumed = build_parser().parse_args(store._resume_args(store._get(task['id'])))
            self.assertEqual(resumed.provider, 'grok')
            real_save = store._save

            def fail_save(_meta):
                raise OSError('simulated metadata save failure')

            store._save = fail_save
            with patch('console.tasks.discover_providers', return_value=providers):
                with self.assertRaises(TaskError):
                    store.action(task['id'], 'roles', {
                        'provider': 'claude', 'model': 'opus', 'reviewer_model': 'sonnet'})
            store._save = real_save
            meta_after = store._get(task['id'])
            self.assertEqual(meta_after['provider'], 'grok')
            self.assertEqual((run / 'role-change.json').read_bytes(), first_auth)
            # A successful second change still replaces authorization and keeps resume consistent.
            with patch('console.tasks.discover_providers', return_value=providers):
                second = store.action(task['id'], 'roles', {
                    'provider': 'claude', 'model': 'opus', 'reviewer_model': 'sonnet'})
            self.assertEqual(second['provider'], 'claude')
            second_auth = json.loads((run / 'role-change.json').read_text())
            self.assertEqual(second_auth['to']['worker']['name'], 'claude')
            self.assertEqual(second_auth['from'].get('review_isolation'), expected_from['review_isolation'])
            self.assertNotEqual(second_auth['id'], json.loads(first_auth)['id'])
            resumed2 = build_parser().parse_args(store._resume_args(store._get(task['id'])))
            self.assertEqual((resumed2.provider, resumed2.model), ('claude', 'opus'))

    def test_taskstore_role_change_from_legacy_checkpoint_missing_defaults(self):
        """When checkpoint omits isolation keys that equal resume defaults, roles still works."""
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        with patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')}):
            store = TaskStore(self.root / 'data', self.root)
            # provider isolation matches normalize_provider_config setdefaults.
            task = store.create({'title': 'Legacy defaults', 'requirement': 'Original requirement',
                                 'provider': 'codex', 'reviewer': 'codex', 'workflow': 'legacy',
                                 'checks': ['python -m unittest'], 'max_agent_calls': 32,
                                 'review_isolation': 'provider', 'worker_isolation': 'provider'})
            meta = store._get(task['id'])
            args = build_parser().parse_args(store._new_args(meta))
            self.assertEqual(args.review_isolation, 'provider')
            run = Path(meta['run_dir'])
            run.mkdir(parents=True, exist_ok=True)
            monitor = Supervisor(RunConfig('Original requirement', self.root, resolve_provider('codex'),
                reviewer=resolve_provider('codex'), run_dir=run, git_history='off',
                checks=args.check, review_isolation=args.review_isolation,
                worker_isolation=args.worker_isolation, protect_paths=args.protect,
                workflow='legacy'), log=lambda _: None)
            monitor.request_stop()
            self.assertEqual(monitor.run().status, 'stopped')
            self._strip_isolation_defaults(run)
            with patch('console.tasks.discover_providers', return_value={'codex': 'codex', 'grok': 'grok'}):
                changed = store.action(task['id'], 'roles', {
                    'provider': 'grok', 'model': 'grok-4.5', 'reviewer_model': 'gpt-6.1-sol'})
            auth = json.loads((run / 'role-change.json').read_text())
            self.assertEqual(auth['from'].get('review_isolation'), 'provider')
            self.assertEqual(auth['from'].get('worker_isolation'), 'provider')
            self.assertEqual(auth['from'].get('protect_paths'), [])
            self.assertEqual(changed['provider'], 'grok')
            resumed = build_parser().parse_args(store._resume_args(store._get(task['id'])))
            self.assertEqual(resumed.provider, 'grok')
