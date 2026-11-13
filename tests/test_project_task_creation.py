"""Console tasks automatically branch from a validated committed checkout."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.project_workspace import git, task_status
from console.acceptance import scan
from console.tasks import TaskError, TaskStore
from tests import test_project_workspace as fixtures


class ParallelCreationTests(unittest.TestCase):
    def setUp(self):
        fixtures.ProjectWorkspaceTests.setUp(self)
        binary = self.root/'bin'
        binary.mkdir()
        codex = binary/'codex'
        codex.write_text(f'#!{sys.executable}\nraise SystemExit("fixture only")\n')
        codex.chmod(0o700)
        env = patch.dict(os.environ,{'PATH':str(binary)+os.pathsep+os.environ['PATH']})
        env.start()
        self.addCleanup(env.stop)
        self.store = TaskStore(self.root/'data',self.source)

    def create(self,**kwargs):
        return self.store.create({'title':'parallel fixture','requirement':'Implement task behavior',
            'provider':'codex','reviewer':'codex','review_isolation':'provider','parallel_project':True,
            'function_dispatch':False,'verification_mode':'review_only',**kwargs})

    def test_create_tasks_without_file_scope_automatically_creates_independent_branches(self):
        a = self.create()
        b = self.create(task_scope=[])
        self.assertTrue(a['parallel_project'])
        self.assertEqual(a['project_cwd'],str(self.source))
        self.assertEqual(a['task_scope'],[])
        self.assertEqual(b['task_scope'],[])
        self.assertNotEqual(a['cwd'],b['cwd'])
        self.assertNotEqual(a['task_branch'],b['task_branch'])
        for task in (a,b):
            self.assertEqual(task['task_branch'],'refs/heads/auto-moniter-task/'+task['id'])
            self.assertEqual(git(task['cwd'],'symbolic-ref','HEAD'),task['task_branch'])
            self.assertEqual(git(task['cwd'],'rev-parse','HEAD'),self.baseline)
        self.assertTrue((Path(a['run_dir'])/'project-workspace.json').is_file())
        self.assertEqual(task_status(a['run_dir']),'queued')
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.baseline)
        self.assertEqual(len(self.store.task_summaries()['tasks']),2)

    def test_stale_acceptance_assets_roll_back_new_worktree_and_registry(self):
        (self.source/'test_new.py').write_text('def test_new(): pass\n')
        plan = {**scan(self.source),'criteria':'Review scoped implementation','selected_ids':[]}
        with self.assertRaisesRegex(TaskError,'重新扫描'):
            self.create(acceptance_plan=plan)
        self.assertEqual(git(self.source,'for-each-ref','--format=%(refname)','refs/heads/auto-moniter-task/'),'')
        self.assertEqual(list((self.source/'.git/auto-moniter-projects').glob('*.json')),[])
        self.assertEqual((self.source/'test_new.py').read_text(),'def test_new(): pass\n')
        self.create()

    def test_failed_auto_start_validation_leaves_no_scope_reservation(self):
        with patch.object(self.store,'_check_start',side_effect=TaskError('provider unavailable',409)):
            with self.assertRaisesRegex(TaskError,'provider unavailable'):
                self.create(auto_start=True)
        self.assertEqual(git(self.source,'for-each-ref','--format=%(refname)','refs/heads/auto-moniter-task/'),'')
        self.create()

    def test_overlap_requires_dependency_and_cancel_releases_scope(self):
        a=self.create(task_scope=['app.py'])
        with self.assertRaisesRegex(TaskError,'重叠'):
            self.create(task_scope=['app.py'])
        b=self.create(task_scope=['app.py'],task_dependencies=[a['id']])
        self.assertEqual(b['task_dependencies'],[a['id']])
        self.store.action(a['id'],'cancel')
        self.store.action(b['id'],'cancel')
        self.assertEqual(task_status(a['run_dir']),'cancelled')
        self.create(task_scope=['app.py'])

    def test_existing_same_worktree_lock_policy_is_not_disabled(self):
        a=self.create()
        with self.assertRaises(TaskError):
            self.create(task_scope=['.'])
        with self.assertRaisesRegex(TaskError,'相对路径'):
            self.create(task_scope=['other.py'],verification_mode='executable',
                        checks=['python3 '+str(self.source/'app.py')])
        self.assertTrue(Path(a['cwd']).exists())

    def test_delete_queued_parallel_task_releases_scope_and_preserves_checkout(self):
        a=self.create(task_scope=['app.py'])
        result=self.store.action(a['id'],'delete',{'confirm':a['id']})
        self.assertTrue(result['deleted'])
        self.assertEqual(task_status(a['run_dir']),'cancelled')
        self.assertTrue((Path(a['cwd'])/'app.py').exists())
        self.create(task_scope=['app.py'])
