"""Rejected repair candidates do not each require another full delivery suite."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter.directions import Directions
from auto_moniter.history import prune_files
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class RepairConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent/'project'
        self.root.mkdir()
        self.run = self.root/'run'
        self.counter = self.parent/'checks-count'
        self.check = shlex.join([sys.executable, '-B', '-c',
            'from pathlib import Path; '
            f'p=Path({str(self.counter)!r}); '
            'p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'])

    def worker(self, count):
        return ScriptedProvider('worker', [
            {'verdict':_verdict('done', evidence=['app.py']), 'touch':'app.py'} for _ in range(count)])

    def reviewer(self, *results):
        return ScriptedProvider('reviewer', [{'verdict':{}, 'text':json.dumps(BRIEF)}, *results])

    def done(self, *, scope='requirement', closed=()):
        return {'verdict':_verdict('done', scope=scope, evidence=['app.py'], closed=closed)}

    def rejected(self, message='main/app.py: incomplete failure handling'):
        return {'verdict':_verdict('continue', gaps=[message], evidence=['app.py'])}

    def monitor(self, worker, reviewer, **kwargs):
        return Supervisor(RunConfig('Implement the requirement', self.root, worker, reviewer=reviewer,
            workflow='review-first', git_history='off', run_dir=self.run, checks=[self.check],
            max_rounds=5, retry_delay_s=0, **kwargs), log=lambda _:None)

    def test_incomplete_repair_is_reviewed_before_expensive_checks(self):
        worker = self.worker(4)
        reviewer = self.reviewer(self.rejected(), self.rejected(), self.rejected(),
            self.done(), self.done(closed=['AUD-1']))
        monitor = self.monitor(worker, reviewer)
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(self.counter.read_text(), '3')  # Candidates 1, 2 and 4; never candidate 3.
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (4, 6))
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertNotIn('REPAIR PREFLIGHT ONLY', reviewer.calls[1]['prompt'])
        self.assertIn('REPAIR PREFLIGHT ONLY', reviewer.calls[3]['prompt'])
        self.assertIn('REPAIR PREFLIGHT ONLY', reviewer.calls[4]['prompt'])
        self.assertIn('checks executed BEFORE this review', reviewer.calls[5]['prompt'])
        self.assertFalse((self.run/'003-check-01.log').exists())
        self.assertTrue((self.run/'003-reviewer-preflight.log').is_file())
        self.assertTrue((self.run/'004-reviewer-preflight-text.md').is_file())
        state = json.loads((self.run/'state.json').read_text())
        worker3 = next(r for r in state['rounds'] if r['role']=='worker' and r['index']==3)
        self.assertEqual(worker3['watcher_result']['repair_preflight']['status'], 'changes_required')
        self.assertNotIn('candidate_checks', worker3['watcher_result'])
        worker4 = next(r for r in state['rounds'] if r['role']=='worker' and r['index']==4)
        self.assertTrue(worker4['watcher_result']['candidate_checks']['passed'])

    def test_preflight_ready_does_not_bypass_failure_or_independent_review(self):
        worker = self.worker(3)
        reviewer = self.reviewer(self.rejected(), self.rejected(), self.done(),
                                 self.done(closed=['AUD-1']))
        monitor = self.monitor(worker, reviewer)
        original = monitor._repair_preflight
        def after_screen(record, provider, last):
            result = original(record, provider, last)
            if record.index == 3:
                self.counter.write_text('environment now fails')
            return result
        with patch.object(monitor, '_repair_preflight', side_effect=after_screen):
            report = monitor.run()
        self.assertEqual(report.status, 'action_required', report.summary)
        self.assertEqual(len(worker.calls), 3)
        self.assertEqual(len(reviewer.calls), 5)
        self.assertFalse(any(r.delivery.get('status')=='accepted' for r in report.records))

    def test_preflight_read_only_violation_cannot_run_checks_or_publish(self):
        worker = self.worker(3)
        reviewer = self.reviewer(self.rejected(), self.rejected(), {**self.done(), 'touch':'reviewer-write.py'})
        report = self.monitor(worker, reviewer).run()
        self.assertNotEqual(report.status, 'done')
        self.assertEqual(self.counter.read_text(), '2')
        self.assertTrue((self.root/'reviewer-write.py').exists())  # Preserve evidence; never roll it back.

    def test_unchanged_module_to_final_reuses_checks_but_obtains_fresh_review(self):
        worker = ScriptedProvider('worker', [
            {**self.done(scope='module'), 'touch':'app.py'}, self.done()])
        reviewer = self.reviewer(self.done(scope='module'), self.done())
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(self.counter.read_text(), '1')
        self.assertEqual((len(worker.calls), len(reviewer.calls)), (2, 3))
        state = json.loads((self.run/'state.json').read_text())
        final = next(r for r in state['rounds'] if r['role']=='worker' and r['index']==2)
        receipt = final['watcher_result']['candidate_checks']
        self.assertEqual(receipt['reused_from']['round'], 1)
        self.assertTrue(receipt['context']['final'])
        self.assertEqual(receipt['checks'][0]['log'], '002-check-01.log')
        self.assertEqual((self.run/'001-check-01.log').read_bytes(), (self.run/'002-check-01.log').read_bytes())
        self.assertEqual(len([r for r in report.records if r.role=='reviewer' and r.index==1]),1)
        prune_files(self.run, 2)
        self.assertTrue((self.run/'002-check-01.log').is_file())

    def test_reuse_survives_local_commit_and_restart_without_reusing_final_approval(self):
        (self.root/'.gitignore').write_text('run/\n__pycache__/\n')
        subprocess.run(['git','init','-q',str(self.root)], check=True)
        subprocess.run(['git','-C',str(self.root),'add','.'], check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=fixture@local',
                        'commit','-qm','baseline'], check=True)
        class Reviewer(ScriptedProvider):
            def _command(inner, session, prompt_file, read_only):
                text = prompt_file.read_text()
                match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', text)
                if match: inner.scripts[0]['verdict']['evidence'].append(match[1])
                return super()._command(session, prompt_file, read_only)
        worker = ScriptedProvider('worker', [{**self.done(scope='module'), 'touch':'app.py'}])
        reviewer = Reviewer('reviewer', [{'verdict':{},'text':json.dumps(BRIEF)}, self.done(scope='module')])
        monitor = self.monitor(worker, reviewer)
        monitor.config.git_history = 'required'
        monitor.log = lambda line: monitor.request_stop() if 'module accepted;' in line else None
        self.assertEqual(monitor.run().status, 'stopped')
        committed = subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD']).strip()
        worker = ScriptedProvider('worker', [self.done()])
        reviewer = Reviewer('reviewer', [self.done()])
        monitor = self.monitor(worker, reviewer, resume=True)
        monitor.config.git_history = 'required'
        report = monitor.run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(self.counter.read_text(), '1')
        self.assertEqual((len(worker.calls),len(reviewer.calls)), (1,1))
        self.assertEqual(subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD']).strip(), committed)

    def test_preflight_rejection_history_survives_resume(self):
        case = RepairConvergenceTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        monitor = case.monitor(case.worker(2), case.reviewer(case.rejected(), case.rejected()))
        monitor.log = lambda line: monitor.request_stop() if 'round 2: reviewer status=continue' in line else None
        self.assertEqual(monitor.run().status, 'stopped')
        worker = case.worker(1)
        reviewer = ScriptedProvider('reviewer', [case.done(),case.done(closed=['AUD-1'])])
        report = case.monitor(worker,reviewer,resume=True).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertIn('REPAIR PREFLIGHT ONLY',reviewer.calls[0]['prompt'])
        self.assertEqual(case.counter.read_text(), '3')

    def test_preflight_artifacts_are_visible_and_pruned_with_the_round(self):
        from console.activity import ActivityReader
        worker = self.worker(3)
        reviewer = self.reviewer(self.rejected(),self.rejected(),self.done(),self.done(closed=['AUD-1']))
        report = self.monitor(worker,reviewer).run()
        self.assertEqual(report.status,'done',report.summary)
        activity = ActivityReader(self.parent/'no-native-sessions').snapshot(self.run,{}, {},'stopped','command')
        self.assertTrue(any('修复预检' in row['text'] for row in activity['events']))
        self.assertTrue((self.run/'003-reviewer-preflight.log').exists())
        prune_files(self.run,4)
        self.assertFalse((self.run/'003-reviewer-preflight.log').exists())
        self.assertFalse((self.run/'003-reviewer-preflight-text.md').exists())
        self.assertFalse((self.run/'003-reviewer-preflight-prompt.md').exists())

    def test_changed_code_does_not_reuse_module_checks(self):
        worker = ScriptedProvider('worker', [
            {**self.done(scope='module'), 'touch':'app.py'}, {**self.done(), 'touch':'new.py'}])
        reviewer = self.reviewer(self.done(scope='module'), self.done())
        # The final evidence must include its actual new source file.
        worker.scripts[1]['verdict']['evidence'].append('new.py')
        reviewer.scripts[-1]['verdict']['evidence'].append('new.py')
        report = self.monitor(worker, reviewer).run()
        self.assertEqual(report.status, 'done', report.summary)
        self.assertEqual(self.counter.read_text(), '2')

    def test_changed_context_or_tampered_log_forces_fresh_checks(self):
        for mutation in ['log','command','model','instructions','business','cycle']:
            with self.subTest(mutation=mutation):
                case = RepairConvergenceTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                worker = ScriptedProvider('worker', [{**case.done(scope='module'), 'touch':'app.py'}, case.done()])
                reviewer = case.reviewer(case.done(scope='module'), case.done())
                monitor = case.monitor(worker, reviewer)
                original = monitor._prepare_candidate_checks
                original_context = monitor._candidate_check_context
                def context(record):
                    value = original_context(record)
                    if mutation == 'cycle' and record.index == 2:
                        value['requirement_cycle'] += 1
                    return value
                def changed(record):
                    if record.index == 2:
                        if mutation == 'log': (case.run/'001-check-01.log').write_text('tampered')
                        if mutation == 'command': monitor.config.checks.append('true')
                        if mutation == 'model': monitor.config.model = 'changed-model'
                        if mutation == 'instructions': Directions(case.run).append('Verify this additional requirement')
                        if mutation == 'business': monitor._business_update_count += 1
                    return original(record)
                with patch.object(monitor, '_prepare_candidate_checks', side_effect=changed), \
                     patch.object(monitor, '_candidate_check_context', side_effect=context):
                    report = monitor.run()
                self.assertFalse(any(r.watcher_result.get('candidate_checks',{}).get('reused_from')
                                     for r in report.records if r.role=='worker'))
                if mutation != 'instructions':
                    self.assertEqual(report.status, 'done', report.summary)
                    self.assertEqual(case.counter.read_text(), '2')


if __name__ == '__main__':
    unittest.main()
