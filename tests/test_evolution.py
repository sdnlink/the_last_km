"""Self-upgrades use real isolated Git worktrees, review and fixed regression gates."""
import json
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution, git, issues
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig
from tests.test_function_assignment_ids import final_task
from tests.test_function_dispatch import plan, result
from tests.test_supervisor import ScriptedProvider


FIX='def repair_target():\n    return 42\n'
REGRESSION=('import unittest\nfrom auto_moniter.supervisor import repair_target\n'
            'class RepairRegression(unittest.TestCase):\n'
            ' def test_result(self): self.assertEqual(repair_target(),42)\n')


class EvolutionWorker(ScriptedProvider):
    def _command(self, session, prompt, read_only):
        command=super()._command(session,prompt,read_only)
        script=Path(command.argv[-1])
        fix=(f"Path('tests/test_evolution_fix.py').write_text({REGRESSION!r})\n"
             "import subprocess\n"
             f"before=subprocess.run([{sys.executable!r},'-m','unittest','tests.test_evolution_fix'],capture_output=True)\n"
             "assert before.returncode!=0, 'regression must expose the old bug'\n"
             "print('before_fix_exit='+str(before.returncode),flush=True)\n"
             f"Path('auto_moniter/supervisor.py').write_text({FIX!r})\n")
        script.write_text(script.read_text().replace('sys.exit(',fix+'sys.exit(',1))
        return command


class EvolutionReviewer(ScriptedProvider):
    def _command(self, session, prompt, read_only):
        match=re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt.read_text())
        if match:
            self.scripts[0]['verdict']['evidence'].append(match[1])
        return super()._command(session,prompt,read_only)


class IgnoredDependencyWorker(EvolutionWorker):
    def _command(self,session,prompt,read_only):
        command=super()._command(session,prompt,read_only)
        script=Path(command.argv[-1])
        runtime=('def repair_target():\n    from pathlib import Path\n'
                 '    return int((Path(__file__).parents[1]/".auto-moniter/value.txt").read_text())\n')
        extra=("Path('.auto-moniter/value.txt').write_text('42')\n"
               f"Path('auto_moniter/supervisor.py').write_text({runtime!r})\n")
        script.write_text(script.read_text().replace('sys.exit(',extra+'sys.exit(',1))
        return command


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.source=self.root/'source';self.source.mkdir()
        env=patch.dict(os.environ,{'AUTO_MONITER_STATE_DIR':str(self.root/'state')})
        env.start();self.addCleanup(env.stop)
        for name in ('auto_moniter','console','tests'):
            (self.source/name).mkdir()
            (self.source/name/'__init__.py').touch()
        (self.source/'auto_moniter/supervisor.py').write_text('def repair_target():\n    return missing_name\n')
        (self.source/'console/tasks.py').write_text('TASKS = []\n')
        (self.source/'auto_moniter/__main__.py').write_text('print(\'{"passed":true}\')\n')
        (self.source/'tests/test_baseline.py').write_text(
            'import unittest\nfrom auto_moniter.supervisor import repair_target\n'
            'class Baseline(unittest.TestCase):\n def test_api(self): self.assertTrue(callable(repair_target))\n')
        (self.source/'.gitignore').write_text('.auto-moniter/\n__pycache__/\n*.pyc\n')
        git(self.source,'init','-q');git(self.source,'add','.')
        git(self.source,'-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','baseline')
        self.base=git(self.source,'rev-parse','HEAD')
        self.evidence=self.root/'evidence';run=self.evidence/'fault';run.mkdir(parents=True)
        write_json(run/'state.json',{'status':'failed','cwd':str(self.source),'rounds':[]})
        write_json(run/'report.json',{'status':'failed','rounds':1,
            'summary':"NameError: name 'missing_name' is not defined in auto_moniter/supervisor.py",
            'stop_reason':'failed'})
        (run/'supervisor.lock').touch()
        task=final_task(file='auto_moniter/supervisor.py')
        task.update(symbol='repair_target',test_files=['tests/test_evolution_fix.py'],check_ids=['auto:1'])
        planning=plan(task)
        payload=json.loads(planning['text']);payload['function_plan']=[task]
        planning['text']=json.dumps(payload)
        submitted=result('F1','done',status='done',file='auto_moniter/supervisor.py')
        approved=result('F1','accepted',status='done',file='auto_moniter/supervisor.py')
        self.worker=EvolutionWorker('worker',[submitted])
        self.reviewer=EvolutionReviewer('reviewer',[planning,approved])
        self.config=RunConfig('',self.source,self.worker,reviewer=self.reviewer,
            max_runtime_s=90,max_agent_calls=8,retry_delay_s=0)
        self.engine=Evolution(self.config,evidence_roots=[self.evidence],directory=self.root/'evolution',log=lambda _:None)

    def test_plan_only_reads_faults_without_writing_or_calling_models(self):
        before=git(self.source,'status','--porcelain')
        result=self.engine.run(plan_only=True)
        self.assertEqual(result['phase'],'planned')
        self.assertFalse(self.engine.directory.exists())
        self.assertFalse(self.worker.calls or self.reviewer.calls)
        self.assertEqual(git(self.source,'status','--porcelain'),before)

    def test_real_reviewed_runtime_fix_passes_old_controller_checks_before_promotion(self):
        validate=self.engine._validate
        observations=[]
        def inspect(cwd,stage):
            observations.append((stage,git(self.source,'rev-parse','HEAD')))
            return validate(cwd,stage)
        with patch.object(self.engine,'_validate',side_effect=inspect):
            record=self.engine.run()
        self.assertEqual(record['phase'],'promoted',record.get('summary'))
        self.assertEqual([head for _,head in observations],[self.base,self.base])
        self.assertEqual((len(self.worker.calls),len(self.reviewer.calls)),(1,2))
        self.assertTrue(all(call['read_only'] for call in self.reviewer.calls))
        self.assertEqual((self.source/'auto_moniter/supervisor.py').read_text(),FIX)
        self.assertTrue((self.source/'tests/test_evolution_fix.py').is_file())
        self.assertEqual(git(self.source,'rev-parse','HEAD'),record['promoted_commit'])
        self.assertEqual(record['previous_commit'],self.base)
        self.assertTrue(all(row['exit_code']==0 for row in record['candidate_checks']))
        self.assertEqual(git(self.source,'rev-list','--count','HEAD'),'2')

    def test_candidate_regression_failure_preserves_original_runtime(self):
        validate=self.engine._validate
        def inject(cwd,stage):
            if stage=='candidate':
                (cwd/'tests/test_hidden_regression.py').write_text(
                    'import unittest\nclass Hidden(unittest.TestCase):\n def test_fail(self): self.fail("hidden integration regression")\n')
            return validate(cwd,stage)
        with patch.object(self.engine,'_validate',side_effect=inject):
            record=self.engine.run()
        self.assertEqual(record['phase'],'candidate_failed',record.get('summary'))
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)
        self.assertIn('missing_name',(self.source/'auto_moniter/supervisor.py').read_text())
        self.assertTrue(Path(record['workspace']).is_dir())
        self.assertTrue(any(row['exit_code']!=0 for row in record['candidate_checks']))

    def test_clean_validation_rejects_candidate_depending_on_uncommitted_ignored_files(self):
        self.config.provider=IgnoredDependencyWorker('worker',list(self.worker.scripts))
        record=self.engine.run()
        self.assertEqual(record['monitor_status'],'done',record.get('summary'))
        self.assertEqual(record['phase'],'candidate_failed',record.get('summary'))
        self.assertTrue((Path(record['workspace'])/'.auto-moniter/value.txt').is_file())
        self.assertFalse((Path(record['validation_workspace'])/'.auto-moniter/value.txt').exists())
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)

    def test_local_source_changes_during_evolution_are_preserved(self):
        validate=self.engine._validate
        def changed(cwd,stage):
            passed=validate(cwd,stage)
            if stage=='candidate':
                (self.source/'user-note.txt').write_text('keep my work')
            return passed
        with patch.object(self.engine,'_validate',side_effect=changed):
            record=self.engine.run()
        self.assertEqual(record['phase'],'deferred',record.get('summary'))
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)
        self.assertEqual((self.source/'user-note.txt').read_text(),'keep my work')

    def test_interrupted_completed_promotion_recovers_without_duplicate_commit(self):
        from auto_moniter import evolution
        original=evolution.git
        def interrupt(cwd,*args):
            value=original(cwd,*args)
            if Path(cwd)==self.source and args[:1]==('merge',):
                raise OSError('fixture interruption after fast-forward')
            return value
        with patch('auto_moniter.evolution.git',side_effect=interrupt):
            record=self.engine.run()
        self.assertEqual(record['phase'],'recovery_required',record.get('summary'))
        recovered=self.engine.recover(self.engine.path.parent)
        self.assertEqual(recovered['phase'],'promoted')
        self.assertEqual(git(self.source,'rev-list','--count','HEAD'),'2')
        self.assertEqual((len(self.worker.calls),len(self.reviewer.calls)),(1,2))

    def test_changed_promotion_evidence_is_not_trusted_on_recovery(self):
        def interrupt(candidate):
            self.engine._save('promoting',candidate=candidate)
            raise OSError('fixture interruption before fast-forward')
        with patch.object(self.engine,'_promote',side_effect=interrupt):
            record=self.engine.run()
        self.assertEqual(record['phase'],'recovery_required')
        (self.engine.path.parent/'candidate-01.log').write_text('substituted evidence')
        with self.assertRaisesRegex(ValueError,'evidence changed'):
            self.engine.recover(self.engine.path.parent)
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)

    def test_dirty_source_is_deferred_without_starting_models(self):
        (self.source/'note.txt').write_text('keep')
        record=self.engine.run()
        self.assertEqual(record['phase'],'deferred')
        self.assertFalse(self.worker.calls or self.reviewer.calls)
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)

    def test_no_reviewer_cannot_self_approve(self):
        self.config.no_review=True
        with self.assertRaisesRegex(ValueError,'independent reviewer'):
            self.engine.run()

    def test_budget_exhausted_after_baseline_never_starts_models(self):
        def exhausted(*args):
            self.engine.started-=100
            return True
        with patch.object(self.engine,'_validate',side_effect=exhausted):
            record=self.engine.run()
        self.assertEqual(record['phase'],'stopped')
        self.assertFalse(self.worker.calls or self.reviewer.calls)
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)

    def test_repeating_failed_candidate_is_not_selected_on_an_unchanged_base(self):
        def reject(cwd,stage):
            return stage=='baseline'
        with patch.object(self.engine,'_validate',side_effect=reject):
            record=self.engine.run()
        self.assertEqual(record['phase'],'candidate_failed',record.get('summary'))
        again=Evolution(self.config,evidence_roots=[self.evidence],directory=self.engine.directory,log=lambda _:None)
        follow_up=again.run(plan_only=True)
        self.assertNotEqual(follow_up['issue']['id'],record['issue']['id'])
        self.assertEqual(follow_up['issue']['kind'],'proactive_review')

    def test_existing_assertions_cannot_be_changed_by_an_evolution_candidate(self):
        original=self.engine._candidate
        def weaken(cwd):
            (cwd/'tests/test_baseline.py').write_text('import unittest\n')
            git(cwd,'add','tests/test_baseline.py')
            git(cwd,'-c','user.name=Fixture','-c','user.email=fixture@local','commit','-qm','weaken tests')
            return original(cwd)
        with patch.object(self.engine,'_candidate',side_effect=weaken):
            record=self.engine.run()
        self.assertEqual(record['phase'],'failed',record.get('summary'))
        self.assertIn('cannot weaken',record['summary'])
        self.assertEqual(git(self.source,'rev-parse','HEAD'),self.base)

    def test_failed_activation_restores_old_tree_without_rewriting_commit_history(self):
        with patch.object(self.engine,'_activate',side_effect=[False,True]) as activate:
            record=self.engine.run()
        self.assertEqual(record['phase'],'rolled_back',record.get('summary'))
        self.assertEqual(activate.call_count,2)
        self.assertEqual(git(self.source,'rev-parse','HEAD^{tree}'),git(self.source,'rev-parse',self.base+'^{tree}'))
        self.assertEqual(git(self.source,'rev-list','--count','HEAD'),'3')
        self.assertEqual(git(self.source,'merge-base',self.base,'HEAD'),self.base)

    def test_user_changes_after_activation_failure_prevent_destructive_rollback(self):
        def failed():
            (self.source/'user-note.txt').write_text('keep my changes')
            return False
        with patch.object(self.engine,'_activate',side_effect=failed):
            record=self.engine.run()
        self.assertEqual(record['phase'],'recovery_required')
        self.assertEqual((self.source/'user-note.txt').read_text(),'keep my changes')
        self.assertEqual(git(self.source,'rev-parse','HEAD'),record['candidate'])

    def test_source_changes_during_successful_restart_do_not_falsely_confirm_upgrade(self):
        def changed():
            (self.source/'user-note.txt').write_text('keep my changes')
            return True
        with patch.object(self.engine,'_activate',side_effect=changed):
            record=self.engine.run()
        self.assertEqual(record['phase'],'recovery_required')
        directory=self.engine.path.parent
        recovered=self.engine.recover(directory)
        self.assertEqual(recovered['phase'],'recovery_required')
        (self.source/'user-note.txt').unlink()
        self.assertEqual(self.engine.recover(directory)['phase'],'promoted')

    def test_cli_cycles_share_call_budget_instead_of_resetting_each_iteration(self):
        from auto_moniter.cli import main
        configurations=[]
        outcomes=[{'phase':'rejected','agent_calls':2},{'phase':'promoted','agent_calls':3}]
        class FakeEvolution:
            def __init__(inner,config,**_kwargs):
                configurations.append(config)
            def run(inner,**_kwargs):
                return outcomes.pop(0)
        with patch('auto_moniter.evolution.Evolution',FakeEvolution), \
             patch('auto_moniter.cli.resolve_provider',return_value=self.worker), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code=main(['evolve','--cwd',str(self.source),'--cycles','8','--max-agent-calls','5'])
        self.assertEqual(code,0)
        self.assertEqual([config.max_agent_calls for config in configurations],[5,3])
        self.assertEqual(json.loads(output.getvalue())['agent_calls'],5)

    def test_no_fault_history_still_plans_an_evidence_driven_code_review(self):
        engine=Evolution(self.config,evidence_roots=[self.root/'empty'],directory=self.root/'other-evolution')
        record=engine.run(plan_only=True)
        self.assertEqual(record['issue']['kind'],'proactive_review')
        self.assertFalse(engine.directory.exists())
        self.assertFalse(self.worker.calls or self.reviewer.calls)

    def test_checks_cannot_change_the_validated_commit_and_still_report_success(self):
        from auto_moniter import evolution
        directory=self.root/'verification';directory.mkdir()
        self.engine.path=directory/'evolution.json'
        self.engine.record={'base':{'commit':self.base}}
        original=evolution.run_command
        def change(prepared,cwd,**kwargs):
            git(cwd,'-c','user.name=Fixture','-c','user.email=fixture@local','commit','--allow-empty','-qm','changed during checks')
            return original(prepared,cwd,**kwargs)
        with patch('auto_moniter.evolution.run_command',side_effect=change):
            self.assertFalse(self.engine._validate(self.source,'candidate'))
        self.assertTrue(self.engine.record['candidate_checks'][0]['source_changed'])

    def test_active_runs_and_product_check_failures_are_not_monitor_upgrade_evidence(self):
        from auto_moniter.workspace import file_lock
        run=self.evidence/'fault'
        with file_lock(run/'supervisor.lock'):
            self.assertEqual(issues([self.evidence]),[])
        report=json.loads((run/'report.json').read_text());report['summary']='Application assertion failed'
        write_json(run/'report.json',report)
        self.assertEqual(issues([self.evidence]),[])
