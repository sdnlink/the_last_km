"""Recovery limits survive restart; evidence is required for learned successes."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.adaptive_recovery import RecoveryJournal
from auto_moniter.experience import Experience
from tests.test_function_assignment_ids import final_task


class AdaptiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        env=patch.dict(os.environ,{'AUTO_MONITER_STATE_DIR':str(self.root/'private')})
        env.start();self.addCleanup(env.stop)
        self.run=self.root/'run';self.run.mkdir()
        self.task=final_task()
        self.experience=Experience(self.root,self.run)

    def receipt(self, passed, index=1, tree='candidate'):
        return {'checks':[{'status':'passed' if passed else 'check_failed'}],
                'passed':passed,'complete':True,'source_changed':False,
                'context':{'commands':['python -m unittest'],'round':index,'tree':tree}}

    def test_distinct_errors_get_finite_retries_and_restart_cannot_reset_them(self):
        context={'tree':'source','kind':'planning'}
        for index in range(4):
            self.assertTrue(RecoveryJournal(self.run).admit(context,'error '+str(index)))
        self.assertFalse(RecoveryJournal(self.run).admit(context,'new error'))
        self.assertFalse(RecoveryJournal(self.run).admit(context,'error 0'))
        self.assertTrue(RecoveryJournal(self.run).admit({**context,'tree':'new source'},'error 0'))

    def test_repeating_the_same_error_is_not_progress(self):
        journal=RecoveryJournal(self.run)
        self.assertTrue(journal.admit({'tree':'source'},'invalid module'))
        self.assertFalse(RecoveryJournal(self.run).admit({'tree':'source'},'invalid module'))
        journal.recovered({'tree':'source'})
        self.assertEqual(journal.read()['episodes'][0]['status'],'recovered')

    def test_corrupt_journal_is_preserved_and_cannot_grant_new_retries(self):
        journal=RecoveryJournal(self.run)
        journal.path.write_text('{')
        with self.assertRaises(ValueError):
            journal.admit({},'error')
        self.assertEqual(journal.path.read_text(),'{')

    def test_no_success_is_learned_from_failure_or_passing_tests_alone(self):
        self.experience.observe(self.task,self.receipt(False))
        self.experience.confirm('F1','candidate')
        self.assertEqual(self.experience.prompt(),'')
        self.experience.observe(self.task,self.receipt(True,2,'fixed'))
        self.assertEqual(self.experience.prompt(),'')
        self.experience.confirm('F1','wrong tree')
        self.assertEqual(self.experience.prompt(),'')
        self.experience.confirm('F1','fixed')
        self.assertIn('verified_repairs',self.experience.prompt())
        self.experience.confirm('F1','fixed')
        self.assertEqual(self.experience.read()['lessons'][0]['successes'],1)

    def test_experience_is_project_scoped_and_observations_are_idempotent(self):
        self.experience.observe(self.task,self.receipt(False))
        self.experience.observe(self.task,self.receipt(False))
        self.assertEqual(self.experience.read()['lessons'][0]['failures'],1)
        other=Experience(self.root/'other-project',self.run)
        self.assertEqual(other.prompt(),'')
        other_run=Experience(self.root,self.root/'other-run')
        other_run.observe(self.task,self.receipt(True,2,'fixed'))
        other_run.confirm('F1','fixed')
        self.assertEqual(other_run.prompt(),'')

    def test_environment_and_source_change_are_not_successful_repairs(self):
        receipt=self.receipt(False)
        receipt['checks'][0]['status']='environment_unavailable'
        self.experience.observe(self.task,receipt)
        self.assertFalse(self.experience.path.exists())
        self.experience.observe(self.task,self.receipt(False))
        receipt=self.receipt(True,2,'fixed');receipt['source_changed']=True
        self.experience.observe(self.task,receipt)
        self.experience.confirm('F1','fixed')
        self.assertEqual(self.experience.prompt(),'')

    def test_memory_symlink_and_malformed_records_are_refused(self):
        self.experience.directory.mkdir(parents=True)
        self.experience.path.symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):
            self.experience.read()
        self.experience.path.unlink()
        self.experience.path.write_text(json.dumps({'version':1,'lessons':[{'id':'bad'}]}))
        with self.assertRaises(ValueError):
            self.experience.read()

    def test_experience_cli_is_read_only_and_does_not_create_state(self):
        from auto_moniter.cli import main
        directory=self.experience.directory
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['experience','--cwd',str(self.root)]),0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['verified_repairs'],0)
        self.assertFalse(result['changes_permissions'])
        self.assertFalse(directory.exists())
