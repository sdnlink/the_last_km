"""A Guardian child can exit between the waiting report and terminal checkpoint."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from auto_moniter import candidate_checks
from auto_moniter.directions import Directions
from auto_moniter.guardian import Guardian
from auto_moniter.health import read_run_snapshot, run_health
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.workspace import file_lock
from tests.test_requirement_follow_up import BRIEF, FOLLOW, NEXT, CitingReviewer, _git
from tests.test_supervisor import ScriptedProvider, _verdict


NEEDS_INPUT = {**NEXT, 'status': 'needs_input',
               'summary': 'Choose a supported input format',
               'blockers': ['A person must choose the input format']}


class RecordingReviewer(CitingReviewer):
    def __init__(self, run_dir, scripts):
        super().__init__(scripts, run_dir.parent)
        self.call_log = run_dir / 'fixture-reviewer.jsonl'
        self.fixture_scripts = scripts

    def _command(self, session, prompt_file, read_only):
        count = len(self.call_log.read_text().splitlines()) if self.call_log.exists() else 0
        self.scripts = [self.fixture_scripts[count]]
        with self.call_log.open('a') as log:
            log.write(json.dumps({'prompt': prompt_file.read_text(), 'read_only': read_only}) + '\n')
        return super()._command(session, prompt_file, read_only)


class RecordingWorker(ScriptedProvider):
    def _command(self, session, prompt_file, read_only):
        with (prompt_file.parent / 'fixture-worker.jsonl').open('a') as log:
            log.write(json.dumps({'prompt': prompt_file.read_text(), 'read_only': read_only}) + '\n')
        return super()._command(session, prompt_file, read_only)


def crash_during_waiting_finish(config, sender, *, before_report=False):
    """Run Supervisor in the real child, exiting at the selected waiting write."""
    run_dir = config.run_dir
    crash_state = run_dir / 'fixture-crash-state.json'
    boundary = 'report.json' if before_report else 'state.json'

    def write(path, value, **kwargs):
        if (path == run_dir / boundary and value['status'] == 'action_required'
                and value['requirements_review']['status'] == 'needs_input' and not crash_state.exists()):
            crash_state.write_bytes((run_dir / 'state.json').read_bytes())
            if not before_report:
                (run_dir / 'fixture-crash-report.json').write_bytes((run_dir / 'report.json').read_bytes())
            os._exit(23)
        write_json(path, value, **kwargs)

    with patch('auto_moniter.supervisor.write_json', side_effect=write):
        result = Supervisor(config, log=lambda _: None).run()
    sender.send({key: getattr(result, key) for key in ('status', 'rounds', 'summary', 'stop_reason')})
    sender.close()


class GuardianFollowUpNeedsInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_dir = self.root / 'run'
        _git(self.root, 'init', '-q')
        _git(self.root, 'checkout', '-qb', 'main')
        _git(self.root, 'config', 'user.email', 'test@local')
        _git(self.root, 'config', 'user.name', 'Fixture')
        (self.root / 'app.py').write_text('base\n')
        _git(self.root, 'add', 'app.py')
        _git(self.root, 'commit', '-qm', 'base')
        self.config = RunConfig(
            FOLLOW, self.root, RecordingWorker('worker', [
                {'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'},
            ]), reviewer=RecordingReviewer(self.run_dir, [
                self.brief(BRIEF),
                {'verdict': _verdict('done', evidence=['app.py']), 'cite': True},
                self.brief(NEEDS_INPUT), self.brief(NEEDS_INPUT),
                self.brief(NEEDS_INPUT), self.brief(NEEDS_INPUT),
            ]), workflow='review-first', git_history='required', run_dir=self.run_dir,
            max_agent_calls=12, max_rounds=2, retry_delay_s=0,
            checks=['test -f app.py && echo check >> run/fixture-checks'],
        )

    @staticmethod
    def brief(brief):
        return {'verdict': {}, 'text': json.dumps(brief)}

    def saved(self, name):
        return json.loads((self.run_dir / name).read_text())

    def calls(self, role):
        return [json.loads(line) for line in (self.run_dir / f'fixture-{role}.jsonl').read_text().splitlines()]

    def guardian(self, config=None):
        return Guardian(config or self.config, delay_s=0, log=lambda _: None)

    def crash(self, *, before_report=False):
        monitor = partial(crash_during_waiting_finish, before_report=before_report)
        with patch('auto_moniter.guardian._monitor', monitor):
            return self.guardian().run()

    def test_lost_terminal_state_write_does_not_restart_waiting_review(self):
        result = self.crash()
        before = self.saved('fixture-crash-state.json')
        report = self.saved('fixture-crash-report.json')
        self.assertEqual(before['status'], 'running')
        self.assertEqual(before['requirements_review']['status'], 'needs_input')
        self.assertEqual(report['status'], 'action_required')
        self.assertEqual(result.status, 'action_required', result.summary)
        self.assertEqual((self.saved('guardian.json')['restarts'], len(self.calls('reviewer')),
                          len(self.calls('worker'))), (0, 4, 1))
        self.assertEqual(result.summary, report['summary'])
        self.assertIn(NEEDS_INPUT['summary'], result.summary)
        saved = self.saved('state.json')
        self.assertEqual(saved, before)
        handoff = saved['requirement_cycles'][0]['handoff']
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), handoff['commit'])
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual((self.run_dir / 'fixture-checks').read_text().splitlines(), ['check'])
        worker, review = saved['rounds']
        receipt = worker['watcher_result']['candidate_checks']
        self.assertTrue(candidate_checks.valid(receipt, self.run_dir, receipt['context']))
        self.assertEqual(review['watcher_result']['candidate_approval']['checks_digest'], receipt['digest'])
        self.assertEqual(handoff['checks_digest'], receipt['digest'])
        snapshot = read_run_snapshot(self.run_dir)
        self.assertTrue(snapshot['finished'])
        self.assertEqual(snapshot['state']['summary'], report['summary'])
        self.assertEqual(snapshot['state']['requirements_review']['blockers'], NEEDS_INPUT['blockers'])
        self.assertEqual(run_health(self.run_dir)['status'], 'action_required')
        with file_lock(self.run_dir / 'guardian.lock'):
            self.assertFalse(read_run_snapshot(self.run_dir)['finished'])

    def test_exit_without_waiting_report_still_restarts_and_preserves_usage(self):
        result = self.crash(before_report=True)
        self.assertEqual(result.status, 'action_required', result.summary)
        self.assertEqual(self.saved('guardian.json')['restarts'], 1)
        before = self.saved('fixture-crash-state.json')
        saved = self.saved('state.json')
        extra_calls = len(self.calls('reviewer')) - 4
        self.assertGreater(extra_calls, 0)
        self.assertEqual(saved['usage']['agent_calls'], before['usage']['agent_calls'] + extra_calls)
        self.assertEqual(len(self.calls('worker')), 1)
        self.assertEqual(saved['requirement_cycles'], before['requirement_cycles'])
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual((self.run_dir / 'fixture-checks').read_text().splitlines(), ['check'])

    def test_incomplete_or_mismatched_evidence_keeps_existing_recovery_paths(self):
        self.assertEqual(self.crash().status, 'action_required')
        guardian = self.guardian()
        state = self.saved('state.json')
        report = self.saved('report.json')
        state_path = self.run_dir / 'state.json'
        report_path = self.run_dir / 'report.json'
        self.assertIsNotNone(guardian._new_report(0))
        self.assertIsNone(guardian._new_report(report_path.stat().st_mtime_ns + 1))

        for name, replacement in (
            ('missing report', None),
            ('invalid report', '{'),
            ('unbound old report', json.dumps({key: value for key, value in report.items()
                                              if key != 'follow_up_wait_digest'})),
            ('changed reason', json.dumps({**report, 'summary': 'Another reason'})),
            ('other cycle', json.dumps({**report, 'requirements_review':
                                        {**report['requirements_review'], 'cycle': 3}})),
        ):
            with self.subTest(name=name):
                if replacement is None:
                    report_path.unlink()
                else:
                    report_path.write_text(replacement)
                self.assertIsNone(guardian._new_report(0))
                self.assertTrue(guardian._resumable_state())
                self.assertFalse(read_run_snapshot(self.run_dir)['finished'])
                write_json(report_path, report)

        for replacement in (None, '{', json.dumps({**state, 'updated_at': 'later checkpoint'}),
                            json.dumps({**state, 'requirement_cycles': []})):
            with self.subTest(state_replacement=replacement and replacement[:30]):
                if replacement is None:
                    state_path.unlink()
                else:
                    state_path.write_text(replacement)
                self.assertIsNone(guardian._new_report(0))
                self.assertFalse(read_run_snapshot(self.run_dir)['finished'])
                write_json(state_path, state)

        for name in ('process.json', 'publication.json', 'work-checkpoint.json'):
            with self.subTest(transaction=name):
                marker = self.run_dir / name
                marker.symlink_to(self.run_dir / 'missing-record')
                self.assertIsNone(guardian._new_report(0))
                self.assertTrue(guardian._resumable_state())
                self.assertFalse(read_run_snapshot(self.run_dir)['finished'])
                marker.unlink()

        other_run = self.run_dir / 'other'
        other_run.mkdir()
        (other_run / 'supervisor.lock').touch()
        write_json(other_run / 'state.json', state)
        write_json(other_run / 'report.json', report)
        other_guardian = self.guardian(replace(self.config, run_dir=other_run))
        self.assertIsNone(other_guardian._new_report(0))
        self.assertFalse(read_run_snapshot(other_run)['finished'])
        self.assertIsNotNone(guardian._new_report(0))
        self.assertTrue(read_run_snapshot(self.run_dir)['finished'])

    def test_explicit_guarded_resume_reviews_new_input_before_dispatch(self):
        self.assertEqual(self.crash().status, 'action_required')
        before = self.saved('state.json')
        instruction = 'Use JSON as the supported input format.'
        direction = Directions(self.run_dir).append(instruction)
        ready = {**NEXT, 'refined_requirement': 'Accept JSON input in app.py.'}
        self.config.reviewer.fixture_scripts[4] = self.brief(ready)
        self.config.provider.scripts = [{'verdict': _verdict('continue', ['still implementing'])}]
        result = self.guardian(replace(self.config, resume=True)).run()
        self.assertEqual(result.status, 'max_rounds', result.summary)
        calls = [json.loads(line) for line in (self.run_dir / 'events.jsonl').read_text().splitlines()]
        starts = [(event['role'], int(event['round'])) for event in calls if event['event'] == 'agent_start']
        self.assertEqual(starts[-2:], [('reviewer', 900001), ('worker', 2)])
        self.assertEqual((len(self.calls('reviewer')), len(self.calls('worker'))), (5, 2))
        review = self.calls('reviewer')[-1]
        self.assertTrue(review['read_only'])
        self.assertIn(instruction, review['prompt'])
        self.assertIn(direction['id'], review['prompt'])
        handoff = before['requirement_cycles'][0]['handoff']
        self.assertIn(handoff['commit'], review['prompt'])
        self.assertIn(ready['refined_requirement'], self.calls('worker')[-1]['prompt'])
        saved = self.saved('state.json')
        self.assertEqual(saved['requirements_review']['status'], 'ready')
        self.assertEqual(saved['requirements_review']['reviewed_direction_sha256'],
                         saved['last_worker_invocation']['requirement_revision']['sha256'])
        self.assertEqual(saved['requirement_cycles'], before['requirement_cycles'])
        self.assertEqual(saved['usage']['agent_calls'], before['usage']['agent_calls'] + 2)
        self.assertEqual(_git(self.root, 'rev-parse', 'HEAD'), handoff['commit'])
        self.assertEqual(_git(self.root, 'rev-list', '--count', 'HEAD'), '2')
