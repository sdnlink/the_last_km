"""Committed requirement acceptance continues only when the request asks for another requirement."""
import json
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from auto_moniter.requirements_review import requests_follow_up
from auto_moniter.supervisor import RunConfig, Supervisor
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


FOLLOW = '实现当前需求。reviewer 审核通过并由 watcher 提交后，继续下一轮功能需求补充。'
NEXT = {**BRIEF, 'summary': 'Next capability found',
        'refined_requirement': 'Add a boundary check in app.py for empty input and reject it.',
        'acceptance_criteria': ['empty input is rejected', 'existing accepted behavior stays']}


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class CitingReviewer(ScriptedProvider):
    def __init__(self, scripts, root):
        super().__init__('reviewer', scripts)
        self.root = root

    def _command(self, session, prompt_file, read_only):
        prompt = prompt_file.read_text(encoding='utf-8')
        match = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt)
        script = self.scripts[0] if self.scripts else {}
        if match and script.get('cite') and isinstance(script.get('verdict'), dict):
            verdict = script['verdict']
            verdict['evidence'] = ['app.py', match.group(1)]
            for item in verdict.get('issue_resolutions') or []:
                item['evidence'] = ['app.py', match.group(1)]
        return super()._command(session, prompt_file, read_only)


class RequirementFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'checkout', '-q', '-b', 'main'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'test@local'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'Fixture'], check=True)
        (self.root / 'app.py').write_text('print("base")\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', 'app.py'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'base'], check=True)
        self.base = _git(self.root, 'rev-parse', 'HEAD')

    def tearDown(self):
        self.temp.cleanup()

    def loop(self, worker, reviewer, requirement=FOLLOW, checks=None, **kwargs):
        return Supervisor(RunConfig(
            requirement, self.root, worker, reviewer=reviewer, workflow='review-first',
            run_dir=self.root / 'run', git_history='required', retry_delay_s=0,
            checks=['test -f app.py'] if checks is None else checks,
            acceptance='reviewer gives the requirement and watcher commits',
            **kwargs,
        ), log=lambda _message: None).run()

    def done(self, touch=None, cite=False):
        script = {'verdict': _verdict('done', evidence=['app.py'])}
        if touch:
            script['touch'] = touch
        if cite:
            script['cite'] = True
        return script

    def test_commit_then_reviewer_hands_next_requirement_to_worker(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
            {'verdict': _verdict('done', evidence=['app.py'])},
            {'verdict': _verdict('continue', ['next capability still open']), 'touch': 'next.py'},
        ])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            self.done(cite=True),
            {'verdict': {}, 'text': json.dumps(NEXT)},
            {'verdict': _verdict('continue', ['verify empty input']), 'cite': True},
        ], self.root)
        result = self.loop(worker, reviewer, max_rounds=3)
        self.assertEqual(result.status, 'max_rounds', result.summary)
        self.assertEqual(len(reviewer.calls), 4)
        self.assertTrue(reviewer.calls[2]['read_only'])
        self.assertIn('Accepted deliveries already committed', reviewer.calls[2]['prompt'])
        self.assertIn(NEXT['refined_requirement'], worker.calls[1]['prompt'])
        self.assertIn(FOLLOW, worker.calls[1]['prompt'])
        self.assertIn('reviewer gives the requirement', worker.calls[1]['prompt'])
        state = json.loads((self.root / 'run/state.json').read_text())
        self.assertEqual(state['requirement_cycles'][0]['status'], 'committed')
        self.assertEqual(state['requirements_review']['refined_requirement'], NEXT['refined_requirement'])
        head = _git(self.root, 'rev-parse', 'HEAD')
        self.assertNotEqual(head, self.base)
        self.assertEqual(state['requirement_cycles'][0]['handoff']['commit'], head)
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')
        self.assertIn('app.py', _git(self.root, 'show', '--name-only', '--format=', 'HEAD'))

    def test_review_rejection_does_not_start_the_next_requirement(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            {'verdict': _verdict('continue', ['behavior still missing']), 'cite': True},
        ], self.root)
        result = self.loop(worker, reviewer, max_rounds=1)
        self.assertEqual(result.status, 'max_rounds')
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), self.base)
        state = json.loads((self.root / 'run/state.json').read_text())
        self.assertEqual(state.get('requirement_cycles', []), [])

    def test_unready_next_requirement_does_not_start_worker(self):
        for value in ({}, {**NEXT, 'status': 'needs_input', 'blockers': ['A person must choose the next capability']}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as run_parent:
                self.root = Path(run_parent)
                subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
                subprocess.run(['git', '-C', str(self.root), 'checkout', '-q', '-b', 'main'], check=True)
                subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'test@local'], check=True)
                subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'Fixture'], check=True)
                (self.root / 'app.py').write_text('print("base")\n', encoding='utf-8')
                subprocess.run(['git', '-C', str(self.root), 'add', 'app.py'], check=True)
                subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'base'], check=True)
                root_head = _git(self.root, 'rev-parse', 'HEAD')
                worker = ScriptedProvider('worker', [
                    {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
                    {'verdict': _verdict('done', evidence=['app.py'])},
                ])
                reviewer = CitingReviewer([
                    {'verdict': {}, 'text': json.dumps(BRIEF)},
                    self.done(cite=True),
                    {'verdict': {}, 'text': json.dumps(value) if value else '{}'},
                ], self.root)
                result = self.loop(worker, reviewer, max_rounds=2)
                self.assertEqual(result.status, 'action_required', result.summary)
                self.assertEqual(len(worker.calls), 1)
                self.assertNotEqual(_git(self.root, 'rev-parse', 'HEAD'), root_head)
                self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_failed_check_does_not_commit_or_start_follow_up(self):
        worker = ScriptedProvider('worker', [
            {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
            {'verdict': _verdict('done', evidence=['app.py'])},
        ])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            self.done(cite=True),
            self.done(cite=True),
        ], self.root)
        result = self.loop(worker, reviewer, max_rounds=2, checks=['false'])
        self.assertEqual(result.status, 'action_required', result.summary)
        self.assertEqual(len(worker.calls),1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertNotIn('Accepted deliveries already committed', reviewer.calls[-1]['prompt'])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), self.base)
        self.assertNotIn('requirement_follow_up', (self.root / 'run/events.jsonl').read_text())

    def test_resume_keeps_ready_follow_up_and_does_not_republish(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            self.done(cite=True),
            {'verdict': {}, 'text': json.dumps(NEXT)},
        ], self.root)
        supervisor = Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='review-first',
            run_dir=self.root / 'run', git_history='required', retry_delay_s=0, checks=['test -f app.py'],
        ), log=lambda _message: None)
        supervisor.log = lambda message: supervisor.request_stop() if 'continuing next requirement elaboration' in message else None
        self.assertEqual(supervisor.run().status, 'stopped')
        committed = _git(self.root, 'rev-parse', 'HEAD')
        worker = ScriptedProvider('worker', [{'verdict': _verdict('continue', ['still implementing next']), 'touch': 'next.py'}])
        reviewer = CitingReviewer([
            {'verdict': _verdict('continue', ['next capability remains incomplete']), 'cite': True},
        ], self.root)
        result = Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='auto', resume=True,
            run_dir=self.root / 'run', git_history='required', retry_delay_s=0, max_rounds=2,
            checks=['test -f app.py'], review_every=1,
        ), log=lambda _message: None).run()
        self.assertEqual(result.status, 'max_rounds', result.summary)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertNotIn('must not rerun', reviewer.calls[0]['prompt'])
        self.assertIn(NEXT['refined_requirement'], worker.calls[0]['prompt'])
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), committed)
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_interrupted_follow_up_review_resumes_before_worker(self):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            self.done(cite=True),
            {'verdict': {}, 'text': json.dumps(NEXT)},
        ], self.root)
        supervisor = Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='review-first',
            run_dir=self.root / 'run', git_history='required', retry_delay_s=0, checks=['test -f app.py'],
        ), log=lambda _message: None)
        supervisor.log = lambda message: supervisor.request_stop() if 'elaborating next requirement' in message else None
        self.assertEqual(supervisor.run().status, 'stopped')
        state = json.loads((self.root / 'run/state.json').read_text())
        self.assertEqual(state['requirements_review']['status'], 'reviewing')
        committed = _git(self.root, 'rev-parse', 'HEAD')
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py'])}])
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(NEXT)}], self.root)
        result = Supervisor(RunConfig(
            FOLLOW, self.root, worker, reviewer=reviewer, workflow='auto', resume=True,
            run_dir=self.root / 'run', git_history='required', retry_delay_s=0, max_rounds=4,
            checks=['test -f app.py'],
        ), log=lambda _message: None)
        result.log = lambda message: result.request_stop() if 'worker running' in message else None
        report = result.run()
        self.assertEqual(report.status, 'stopped', report.summary)
        self.assertEqual(worker.calls, [])
        self.assertTrue(reviewer.calls[0]['read_only'])
        self.assertIn('Accepted deliveries already committed', reviewer.calls[0]['prompt'])
        saved = json.loads((self.root / 'run/state.json').read_text())
        self.assertEqual(saved['requirements_review']['status'], 'ready')
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), committed)

    def test_ordinary_requirement_still_finishes_after_commit(self):
        self.assertFalse(requests_follow_up('实现当前需求并退出'))
        self.assertTrue(requests_follow_up(FOLLOW))
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = CitingReviewer([
            {'verdict': {}, 'text': json.dumps(BRIEF)},
            self.done(cite=True),
            {'verdict': {}, 'text': json.dumps(NEXT)},
        ], self.root)
        result = self.loop(worker, reviewer, requirement='实现当前需求并退出', max_rounds=2)
        self.assertEqual(result.status, 'done')
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(len(worker.calls), 1)
        state = json.loads((self.root / 'run/state.json').read_text())
        self.assertEqual(state['status'], 'done')
        self.assertEqual(state.get('requirement_cycles', []), [])

    def test_negated_follow_up_does_not_authorize_another_requirement(self):
        for request in (
            '当前需求完成后，不要继续下一轮功能需求补充。',
            '不需要下一轮需求补充，实现当前功能后退出。',
            '禁止继续下一轮功能需求补充。',
            'Do not continue with another requirement.',
            "Don't continue with the next requirement.",
            'Implement this; never continue with another requirement.',
        ):
            with self.subTest(request=request):
                self.assertFalse(requests_follow_up(request))
        for request in (FOLLOW, 'Continue with another requirement.',
                        '完成需求，不要删除现有数据。继续下一轮需求补充。'):
            with self.subTest(request=request):
                self.assertTrue(requests_follow_up(request))

    def test_negated_request_finishes_without_extra_reviewer_call(self):
        worker = ScriptedProvider('worker', [self.done(touch='app.py')])
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(BRIEF)}, self.done(cite=True)], self.root)
        result = self.loop(worker, reviewer, requirement='实现当前需求，不要继续下一轮功能需求补充。', max_rounds=1)
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))

    def test_cycle_limit_stops_before_new_review_and_keeps_checkpoint_resumable(self):
        worker = ScriptedProvider('worker', [self.done(touch='app.py')])
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(BRIEF)}, self.done(cite=True)], self.root)
        with patch('auto_moniter.requirements_review.MAX_CYCLES', 1):
            result = self.loop(worker, reviewer)
            self.assertEqual(result.status, 'action_required', result.summary)
            self.assertEqual((len(worker.calls),len(reviewer.calls)),(1,2))
            saved = json.loads((self.root/'run/state.json').read_text())
            self.assertEqual(len(saved['requirement_cycles']),1)
            committed = _git(self.root,'rev-parse','HEAD')
            resumed_worker = ScriptedProvider('worker', [])
            resumed_reviewer = CitingReviewer([],self.root)
            resumed = self.loop(resumed_worker,resumed_reviewer,resume=True)
            self.assertEqual(resumed.status,'action_required',resumed.summary)
            self.assertEqual((len(resumed_worker.calls),len(resumed_reviewer.calls)),(0,0))
            self.assertEqual(_git(self.root,'rev-parse','HEAD'),committed)

    def test_resumed_follow_up_does_not_overwrite_initial_requirement_artifacts(self):
        worker = ScriptedProvider('worker', [self.done(touch='app.py')])
        reviewer = CitingReviewer([{'verdict': {}, 'text': json.dumps(BRIEF)}, self.done(cite=True)], self.root)
        supervisor = Supervisor(RunConfig(FOLLOW, self.root, worker, reviewer=reviewer,
            workflow='review-first', run_dir=self.root/'run', git_history='required',
            retry_delay_s=0), log=lambda _: None)
        supervisor.log = lambda message: supervisor.request_stop() if 'elaborating next requirement' in message else None
        self.assertEqual(supervisor.run().status, 'stopped')
        originals = {p.name:p.read_bytes() for p in (self.root/'run').glob('000-reviewer*') if p.is_file()}
        self.assertTrue(originals)
        resumed = Supervisor(RunConfig(FOLLOW, self.root, ScriptedProvider('worker', []),
            reviewer=CitingReviewer([{'verdict': {}, 'text': json.dumps(NEXT)}], self.root),
            workflow='auto', resume=True, run_dir=self.root/'run', git_history='required',
            retry_delay_s=0), log=lambda _: None)
        resumed.log = lambda message: resumed.request_stop() if 'worker running' in message else None
        self.assertEqual(resumed.run().status, 'stopped')
        for name, contents in originals.items():
            self.assertEqual((self.root/'run'/name).read_bytes(), contents, name)
        self.assertTrue((self.root/'run/900001-reviewer-text.md').is_file())


if __name__ == '__main__':
    unittest.main()
