"""Final watcher failures remain visible alongside successful worker claims."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.diagnostics import console_result, diagnose
from console.tasks import TaskStore
from tests.test_supervisor import ScriptedProvider, _verdict


class FailureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.run=self.root/'run';self.run.mkdir()

    def diagnostic(self,status='failed',summary='',**extra):
        state={'summary':summary,**extra}
        return diagnose(status,state,{}, {'health':'finished'}, {}, self.run)

    def test_external_head_failure_wins_over_worker_done_and_past_timeout(self):
        result=self.diagnostic(summary='GitHistoryError: Business HEAD changed outside watcher approval',
            records=[{'index':1,'role':'worker','failure':'timed out','exit_code':143},
                     {'index':3,'role':'worker','status':'done','summary':'Implemented everything'}],
            git_history={'business_head':{'commit':'a'*40}})
        self.assertEqual(result['code'],'business_head_changed')
        self.assertEqual(result['role'],'watcher');self.assertEqual(result['round'],3)
        self.assertEqual(result['expected_commit'],'a'*40)
        self.assertEqual(len(result['recent_failures']),1)
        self.assertIn('恢复执行',''.join(result['actions']))

    def test_network_idle_budget_and_strict_failures_have_distinct_causes(self):
        cases=[('action_required','Requirement reviewer failed: 503 Service Unavailable','model_service_unavailable'),
               ('failed','Worker turn timed out (no output).','idle_timeout'),
               ('budget_exceeded','Total active runtime budget exhausted','runtime_budget'),
               ('budget_exceeded','Agent call budget exhausted','call_budget'),
               ('invalid','Strict read-only review requires bwrap','isolation_unavailable')]
        for status,summary,code in cases:
            with self.subTest(code=code):
                result=self.diagnostic(status,summary)
                self.assertEqual(result['code'],code)
                self.assertTrue(result['actions']);self.assertTrue(result['reason'])
                if code=='isolation_unavailable':self.assertEqual(result['role'],'watcher')

    def test_active_and_done_tasks_do_not_show_stale_failure(self):
        for status in ('running','starting','paused','queued','done'):
            self.assertIsNone(self.diagnostic(status,'old authentication failure'))

    def test_program_exception_and_checkpoint_restore_error_are_actionable(self):
        result=self.diagnostic(summary="NameError: name 're' is not defined")
        self.assertEqual(result['code'],'supervisor_exception')
        self.assertIn('NameError',result['reason'])
        self.assertIn('不能修复',''.join(result['actions']))
        result=self.diagnostic('recovery_failed','Cannot resume checkpoint: NameError: re',resume_error=True)
        self.assertEqual(result['code'],'checkpoint_restore_failed')
        self.assertEqual(result['source'],'resume-error.json')
        self.assertIn('追加预算无法解决',''.join(result['actions']))

    def test_console_fallback_only_recognizes_monitor_result_and_redacts(self):
        log=self.run/'console.log'
        log.write_text('worker says done\n'+json.dumps({'status':'done','summary':'not a monitor result'})+'\n')
        self.assertEqual(console_result(self.run),{})
        log.write_text(log.read_text()+json.dumps({'status':'invalid','rounds':0,
            'summary':'Could not invoke: api_key=PRIVATE_TOKEN'})+'\n')
        result=console_result(self.run)
        self.assertEqual(result['status'],'invalid');self.assertNotIn('PRIVATE_TOKEN',result['summary'])
        self.assertEqual(diagnose('failed',{}, {},{}, {},self.run)['code'],'executable_unavailable')

    def test_final_summary_is_saved_in_state_without_replacing_agent_verdict(self):
        worker=ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['unfinished'])}])
        monitor=Supervisor(RunConfig('task',self.root,worker,no_review=True,git_history='off',
            run_dir=self.run,max_rounds=1,retry_delay_s=0),log=lambda _:None)
        result=monitor.run();state=json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['summary'],result.summary)
        self.assertEqual(state['stop_reason'],'max_rounds')
        self.assertEqual(state['last_worker']['status'],'continue')
        monitor._write_state([], 'running',0,'','')
        self.assertNotIn('summary',json.loads((self.run/'state.json').read_text()))

    def test_detail_list_and_progress_expose_final_failure_without_changing_report(self):
        store=TaskStore(self.root/'data',self.root)
        task=store.create({'title':'broken','requirement':'PRIVATE_REQUIREMENT','provider':'codex','reviewer':'codex'})
        run=Path(task['run_dir']);run.mkdir()
        record={'index':3,'role':'worker','status':'done','summary':'Ready for independent review'}
        history={'enabled':True,'business_head':{'commit':'a'*40}}
        write_json(run/'state.json',{'cwd':str(self.root),'status':'failed','last_worker':record,'rounds':[record],'git_history':history})
        report={'status':'failed','rounds':3,'summary':'GitHistoryError: Business HEAD changed outside watcher approval',
                'stop_reason':'failed','records':[record],'git_history':history}
        write_json(run/'report.json',report);(run/'supervisor.lock').touch()
        before=(run/'report.json').read_bytes()
        detail=store.detail(task['id']);listing=store.task_summaries()['tasks'][0];progress=store.progress(task['id'])
        for view in (detail,listing,progress):self.assertEqual(view['failure']['code'],'business_head_changed')
        self.assertNotIn('raw_summary',listing['failure'])
        self.assertEqual(detail['last_worker']['status'],'done')
        self.assertEqual((run/'report.json').read_bytes(),before)
        self.assertNotIn('resume',detail['actions'])

    def test_preflight_failure_without_state_uses_final_cli_output(self):
        store=TaskStore(self.root/'data',self.root)
        task=store.create({'title':'startup','requirement':'task'})
        meta=store._get(task['id']);meta.update(started_at='2026-11-09T00:00:00+00:00',status='starting',exit_code=2);store._save(meta)
        run=Path(task['run_dir']);run.mkdir()
        (run/'console.log').write_text(json.dumps({'status':'invalid','rounds':0,
            'summary':'Strict read-only review requires bwrap'}))
        detail=store.detail(task['id'])
        self.assertEqual(detail['status'],'failed')
        self.assertEqual(detail['failure']['code'],'isolation_unavailable')
        self.assertEqual(detail['failure']['exit_code'],2)
