"""Module delivery must run the configured fixed checks before local publication."""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from auto_moniter.providers import TurnResult
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.verification import outcome as check_outcome
from auto_moniter.workspace import workspace_fingerprint
from tests.test_modules import PLAN, outcome
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class Reviewer(ScriptedProvider):
    def __init__(self, root, scripts):
        super().__init__('reviewer', scripts)
        self.root = root

    def _command(self, session, prompt, read_only):
        checkpoint=re.search(r'Worker checkpoint: ([0-9a-f]{40,64})',prompt.read_text())
        if checkpoint:
            self.scripts[0]['verdict']['evidence'].append(checkpoint.group(1))
        return super()._command(session, prompt, read_only)


class ModuleDeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.auto-moniter/\n')
        (self.root / 'app.py').write_text('baseline\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@local', 'commit', '-qm', 'initial'], check=True)
        self.before = git(self.root, 'rev-parse', 'HEAD')
        self.before_audit = git(self.root, 'show-ref') if False else ''
        self.run = self.root / 'run'

    def _run(self, checks, worker_turns=None, reviewer_turns=None, modules=None):
        worker = ScriptedProvider('worker', worker_turns or [{'verdict': outcome(['A'], plan=PLAN), 'touch': 'app.py'}])
        reviewer = Reviewer(self.root, reviewer_turns or [
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': outcome(['A'])},
        ])
        report = Supervisor(RunConfig(
            'task', self.root, worker, reviewer=reviewer, workflow='review-first',
            git_history='required', run_dir=self.run, max_rounds=1, retry_delay_s=0,
            checks=checks, modules=modules if modules is not None else PLAN,
        ), log=lambda _: None).run()
        return report, worker, reviewer

    def _refs(self):
        head = git(self.root, 'rev-parse', 'HEAD')
        audit = ''
        history = self.run / 'git-history.json'
        if history.exists():
            audit = json.loads(history.read_text()).get('head', '')
        return head, audit

    def test_failed_fixed_check_blocks_module_publication(self):
        report, worker, reviewer = self._run(['false'])
        self.assertNotEqual(report.status, 'done')
        head, audit = self._refs()
        self.assertEqual(head, self.before)
        self.assertFalse(audit)
        state = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(state['modules'][0]['status'], 'pending')
        results = json.loads((self.run / 'acceptance-results.json').read_text())
        self.assertEqual(results['scope'], 'module')
        self.assertFalse(results['complete'])
        self.assertEqual(results['checks'][0]['status'], 'check_failed')
        self.assertTrue(results['checks'][0]['command'])
        self.assertTrue((self.run / '001-check-01.log').is_file())
        self.assertIn('Acceptance check failed', ' '.join(report.records[-1].verdict.gaps))

    def test_successful_fixed_check_precedes_publication_of_same_candidate(self):
        report, _, _ = self._run(['test -f app.py'])
        self.assertIn(report.status, {'stopped', 'done', 'max_rounds'})
        head, audit = self._refs()
        self.assertNotEqual(head, self.before)
        self.assertTrue(audit)
        results = json.loads((self.run / 'acceptance-results.json').read_text())
        self.assertTrue(results['complete'])
        self.assertEqual(results['checks'][0]['status'], 'passed')
        self.assertEqual(results['tree'], workspace_fingerprint(self.root, ignore_dir=self.run))
        state = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(state['modules'][0]['status'], 'accepted')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), head)

    def test_module_approval_does_not_run_the_final_requirement_gate(self):
        worker=ScriptedProvider('worker',[
            {'verdict':outcome(['A','B'],'requirement'),'touch':'app.py'}])
        reviewer=Reviewer(self.root,[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':outcome(['A'])}])
        monitor=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            git_history='required',run_dir=self.run,max_rounds=2,retry_delay_s=0,modules=PLAN,
            checks=['git diff --check'],quality_policy={'requirements':[
                {'id':'R1','text':'B works','module_ids':['B'],'evidence_paths':['app.py']}]}),log=lambda _:None)
        monitor.log=lambda message:monitor.request_stop() if 'module accepted;' in message else None
        report=monitor.run()
        self.assertEqual(report.status,'stopped',report.summary)
        self.assertNotEqual(git(self.root,'rev-parse','HEAD'),self.before)
        self.assertEqual([(item['id'],item['status']) for item in monitor.modules.export()],
                         [('A','accepted'),('B','pending')])
        worker=ScriptedProvider('worker',[{'verdict':outcome(['B'],'requirement'),'touch':'app.py'}])
        reviewer=Reviewer(self.root,[{'verdict':outcome(['B'],'requirement')}])
        monitor=Supervisor(RunConfig('',self.root,worker,reviewer=reviewer,resume=True,
            workflow='review-first',git_history='required',run_dir=self.run,max_rounds=2,
            retry_delay_s=0),log=lambda _:None)
        report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,1))
        self.assertTrue(all(item['status']=='accepted' for item in monitor.modules.export()))

    def test_final_coverage_citation_failure_can_resume_with_reviewer_only(self):
        (self.root/'extra.txt').write_text('Requirement evidence\n')
        submitted=outcome(['A','B'],'requirement')
        submitted['evidence'].append('extra.txt')
        worker=ScriptedProvider('worker',[{'verdict':submitted,'touch':'app.py'}])
        reviewer=Reviewer(self.root,[{'verdict':{},'text':json.dumps(BRIEF)},
            {'verdict':outcome(['A','B'],'requirement')}])
        monitor=Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            git_history='required',run_dir=self.run,max_rounds=1,retry_delay_s=0,modules=PLAN,
            checks=['git diff --check'],quality_policy={'requirements':[
                {'id':'R1','text':'A and B work','module_ids':['A','B'],'evidence_paths':['extra.txt']}]}),log=lambda _:None)
        report=monitor.run()
        self.assertEqual(report.status,'action_required',report.summary)
        self.assertIn('lacks joint cited evidence',report.summary)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.before)
        approved=outcome(['A','B'],'requirement')
        approved['evidence'].append('extra.txt')
        worker=ScriptedProvider('worker',[])
        reviewer=Reviewer(self.root,[{'verdict':approved}])
        monitor=Supervisor(RunConfig('',self.root,worker,reviewer=reviewer,resume=True,
            workflow='review-first',git_history='required',run_dir=self.run,max_rounds=1,
            retry_delay_s=0),log=lambda _:None)
        report=monitor.run()
        self.assertEqual(report.status,'done',report.summary)
        self.assertFalse(worker.calls)
        self.assertEqual(len(reviewer.calls),1)
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(event['event']=='check_finished' for event in events),1)

    def test_timeout_missing_environment_and_source_change_do_not_publish(self):
        cases = [
            (['sleep 30'], 'timed_out', {'check_timeout_s': 0.2}),
            (['definitely-missing-acceptance-tool-xyz --strict'], 'environment_unavailable', {}),
            (['printf mutated > app.py'], 'source', {}),
        ]
        for checks, kind, extra in cases:
            with self.subTest(kind=kind):
                self.setUp()
                root_before = git(self.root, 'rev-parse', 'HEAD')
                worker = ScriptedProvider('worker', [{'verdict': outcome(['A'], plan=PLAN), 'touch': 'app.py'}])
                reviewer = Reviewer(self.root, [
                    {'verdict': {}, 'text': json.dumps(BRIEF)},
                    {'verdict': outcome(['A'])},
                ])
                report = Supervisor(RunConfig(
                    'task', self.root, worker, reviewer=reviewer, workflow='review-first',
                    git_history='required', run_dir=self.run, max_rounds=1, retry_delay_s=0,
                    checks=checks, modules=PLAN, **extra,
                ), log=lambda _: None).run()
                self.assertNotEqual(report.status, 'done')
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), root_before)
                history = json.loads((self.run / 'git-history.json').read_text())
                self.assertFalse(history.get('head'))
                results = json.loads((self.run / 'acceptance-results.json').read_text())
                self.assertFalse(results['complete'])
                self.assertEqual(results['scope'], 'module')
                if kind == 'timed_out':
                    self.assertEqual(results['checks'][0]['status'], 'timed_out')
                elif kind == 'environment_unavailable':
                    self.assertEqual(results['checks'][0]['status'], 'environment_unavailable')
                    self.assertIn('Acceptance environment unavailable', report.summary)
                else:
                    self.assertTrue(results['source_changed'])
                    self.assertIn('changed workspace source',report.summary)
                    self.assertEqual(len(reviewer.calls),1)  # Requirements review only; candidate is stale.

    def test_requirement_checks_and_user_module_checks_still_run(self):
        modules = [
            {**PLAN[0], 'checks': ['test -f app.py']},
            PLAN[1],
        ]
        report, _, _ = self._run(['true'], modules=modules)
        results = json.loads((self.run / 'acceptance-results.json').read_text())
        self.assertEqual([item['status'] for item in results['checks']], ['passed', 'passed'])
        self.assertTrue(results['complete'])
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        self.assertNotEqual(report.status, 'failed')

    def test_successful_command_mentioning_missing_module_stays_passed(self):
        command = 'python3 -c "assert \'ModuleNotFoundError: No module named example\'"'
        literal = TurnResult(text='', stdout='', stderr='', exit_code=0, command=['sh', '-c', command])
        self.assertEqual(check_outcome(literal), 'passed')
        failed = TurnResult(
            text='', stdout='', stderr='ModuleNotFoundError: No module named example\n',
            exit_code=1, command=['sh', '-c', command],
        )
        self.assertEqual(check_outcome(failed), 'environment_unavailable')
        timed = TurnResult(
            text='', stdout='', stderr='ModuleNotFoundError: No module named example\n',
            exit_code=1, timed_out=True, interruption='turn limit', command=['sh', '-c', command],
        )
        self.assertEqual(check_outcome(timed), 'environment_unavailable')
        stopped = TurnResult(
            text='', stdout='partial diagnostic before budget\n', stderr='',
            exit_code=1, interruption='stop requested', command=['sh', '-c', 'sleep 30'],
        )
        self.assertEqual(check_outcome(stopped), 'interrupted')

        report, _, _ = self._run([command])
        self.assertIn(report.status, {'stopped', 'done', 'max_rounds'})
        self.assertNotEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        results = json.loads((self.run / 'acceptance-results.json').read_text())
        self.assertEqual(results['checks'][0]['status'], 'passed')
        self.assertTrue(results['complete'])
        self.assertNotIn('Acceptance environment unavailable', report.summary)


if __name__ == '__main__':
    unittest.main()
