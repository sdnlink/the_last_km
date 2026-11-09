import gzip
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_moniter.cli import build_parser
from auto_moniter.documentation import baseline_problems, verify
from auto_moniter.locking import workspace_lease, workspaces_overlap
from auto_moniter.providers import resolve_provider
from auto_moniter.selftest import selftest
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import LockError, file_snapshot
from tests.test_supervisor import ScriptedProvider
from console.tasks import TaskStore, TaskError


class RuntimeCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_parent_child_conflict_in_both_orders_and_siblings_remain_independent(self):
        child = self.project / 'docs'
        sibling = self.project / 'other'
        child.mkdir(); sibling.mkdir()
        for first, second in [(self.project, child), (child, self.project)]:
            with workspace_lease(first), self.assertRaises(LockError):
                with workspace_lease(second): pass
        with workspace_lease(child), workspace_lease(sibling): pass
        self.assertTrue(workspaces_overlap(self.project, child))
        self.assertFalse(workspaces_overlap(child, sibling))

    def test_same_git_worktree_conflicts_but_linked_worktree_is_independent(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        subprocess.run(['git', '-C', str(self.project), '-c', 'user.name=Test', '-c', 'user.email=t@local',
                        'commit', '--allow-empty', '-qm', 'initial'], check=True)
        child = self.project / 'docs'; child.mkdir()
        linked = self.root / 'linked'
        subprocess.run(['git', '-C', str(self.project), 'worktree', 'add', '-qb', 'linked', str(linked)], check=True)
        with workspace_lease(self.project):
            with self.assertRaises(LockError):
                with workspace_lease(child): pass
            with workspace_lease(linked): pass

    def test_different_state_directories_still_conflict(self):
        child = self.project/'docs'; child.mkdir()
        with workspace_lease(self.project), patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root/'other-state')}):
            with self.assertRaises(LockError):
                with workspace_lease(child): pass

    def create(self, **extra):
        store = TaskStore(self.root / 'data', self.project)
        task = store.create({'title': 'fixture', 'requirement': 'implement fixed plan',
                             'provider': 'codex', 'reviewer': 'codex', 'workflow':'legacy', **extra})
        return store, task

    def test_web_plan_isolation_and_quotas_reach_real_cli_parser(self):
        (self.project / 'tests').mkdir()
        plan = [{'id': 'A', 'title': 'API', 'acceptance': ['negative inputs rejected'], 'checks': ['true']}]
        store, task = self.create(modules=plan, worker_isolation='strict', protect_paths=['tests'], turn_timeout=9,
                                  max_history_bytes=1234, max_agent_state_bytes=5678)
        meta = store._get(task['id'])
        args = build_parser().parse_args(store._new_args(meta))
        self.assertEqual(args.review_isolation, 'strict')
        self.assertEqual(args.worker_isolation, 'strict')
        self.assertEqual(args.protect, ['tests'])
        self.assertEqual(args.turn_timeout, '9')
        self.assertEqual(args.max_history_bytes, 1234)
        self.assertEqual(args.max_agent_state_bytes, 5678)
        self.assertEqual(meta['modules'], plan)
        self.assertEqual(args.modules, Path(task['run_dir']) / 'modules.json')

    def test_invalid_module_dependency_and_daemon_strict_are_rejected_before_save(self):
        for extra in [{'modules': [{'id': 'A', 'title': 'A', 'acceptance': ['ok'], 'depends_on': ['missing']}]},
                      {'reviewer': 'paseo'}, {'worker_isolation': 'provider', 'protect_paths': ['tests']},
                      {'max_history_bytes': True}, {'reviewer': 'paseo', 'review_isolation': 'provider', 'paseo_backend': 'codex'}]:
            with self.subTest(extra=extra), self.assertRaises(TaskError): self.create(**extra)

    def test_command_verification_cannot_be_created_without_commands(self):
        with self.assertRaisesRegex(TaskError, '至少需要一条'): self.create(verification_mode='executable')
        store, task = self.create(verification_mode='executable', checks=['python -m unittest'])
        self.assertEqual(store._get(task['id'])['verification_mode'], 'executable')

    def checkpoint(self):
        store, task = self.create()
        run = Path(task['run_dir']); run.mkdir()
        config = RunConfig('', self.project, resolve_provider('codex'), reviewer=resolve_provider('codex'), review_isolation='strict')
        state = {'cwd': str(self.project), 'status': 'budget_exceeded', 'checks': [], 'rounds': [],
                 'provider_config': Supervisor._provider_config(SimpleNamespace(config=config)),
                 'usage': {'elapsed_s': 7201, 'agent_calls': 100, 'max_runtime_s': 7200, 'max_agent_calls': 100}}
        write_json(run / 'state.json', state)
        write_json(run / 'report.json', {'status': 'budget_exceeded', 'rounds': 0, 'summary': 'budget exhausted', 'stop_reason': 'budget_exceeded'})
        (run / 'supervisor.lock').touch(); (run / 'requirement.md').write_text('task')
        return store, task, run

    def test_exhausted_resume_requires_increase_and_preserves_cumulative_usage(self):
        store, task, run = self.checkpoint()
        before = (run / 'state.json').read_bytes()
        with patch.object(store, '_check_start'), patch.object(store, '_launch') as launch:
            with self.assertRaisesRegex(TaskError, '预算已耗尽'): store.action(task['id'], 'resume')
            launch.assert_not_called()
            store.action(task['id'], 'resume', {'additional_runtime_minutes': 5, 'additional_agent_calls': 3})
            meta = launch.call_args.args[0]
            args = build_parser().parse_args(meta['resume_argv'])
            self.assertGreater(float(args.max_runtime), 7201)
            self.assertEqual(args.max_agent_calls, 104)
        self.assertEqual(before, (run / 'state.json').read_bytes())

    def test_history_quota_stops_before_any_agent_call(self):
        provider = ScriptedProvider('worker', [])
        with patch('auto_moniter.quota.account', return_value={'history_bytes': 100, 'agent_state_bytes': 0}):
            result = Supervisor(RunConfig('task', self.project, provider, no_review=True,
                                 git_history='off', max_history_bytes=10, run_dir=self.root/'run'), log=lambda _:None).run()
        self.assertEqual(result.status, 'budget_exceeded')
        self.assertEqual(result.rounds, 0)
        self.assertEqual(provider.calls, [])
        self.assertIn('history_bytes', result.summary)

    def test_documentation_baseline_does_not_allow_contract_or_test_changes(self):
        baseline = {str(self.project/'docs/a.md'): 'old', str(self.project/'tests/test.py'): 'old'}
        self.assertEqual(baseline_problems(self.project, baseline, {**baseline, str(self.project/'docs/a.md'): 'new'}), [])
        self.assertTrue(baseline_problems(self.project, baseline, {**baseline, str(self.project/'tests/test.py'): 'new'}))
        self.assertTrue(baseline_problems(self.project, baseline, {**baseline, str(self.project/'docs/new.json'): 'new'}))

    def test_installed_smoke_requires_independent_review_and_feedback(self):
        report = selftest()
        self.assertTrue(report['passed'])
        self.assertEqual(report['reviewer_calls'], 2)
        self.assertEqual(report['worker_calls'], 2)
        self.assertIn('explicit audit closure', report['checks'])

    def test_documentation_gate_classifies_only_archived_markdown_and_keeps_other_failures(self):
        import hashlib
        (self.project/'scripts').mkdir()
        (self.project/'docs/sql/validation').mkdir(parents=True)
        (self.project/'docs/a.md').write_text('updated documentation')
        record = self.project/'docs/sql/validation/2026-11-05-code-review.json'
        record.write_text(json.dumps({'artifacts': [{'path': 'docs/a.md', 'sha256': hashlib.sha256(b'old').hexdigest()}]}))
        source = '''class Context:
 def load_context(self): return ({},{},{})
 def load_spec(self): return {}
def module(name): return Context()
def review_plan(*args): return [{'subject':'evidence_hashes' if i==0 else str(i)} for i in range(100)]
def focus_check(focus,*args):
 return (['Stale validation artifact: docs/a.md'] if focus['subject']=='evidence_hashes' else EXTRA, {})
EXTRA = []
'''
        script = self.project/'scripts/review_development.py'
        baseline = self.root/'baseline.json.gz'
        before = record.read_bytes()
        for extra in ([], ['Authorization missing'], ['Stale validation artifact: api/openapi.yaml']):
            script.write_text(source + '\nEXTRA = ' + repr(extra) + '\n')
            baseline.write_bytes(gzip.compress(json.dumps(file_snapshot(self.project)).encode()))
            result = verify(self.project, baseline)
            self.assertEqual(result['passed'], not extra)
            self.assertEqual(result['archived_documentation'], ['docs/a.md'])
            if extra: self.assertIn(extra[0], result['errors'])
        self.assertEqual(before, record.read_bytes())
