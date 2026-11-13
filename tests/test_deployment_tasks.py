"""Deployment tasks run operational work and require run-specific remote evidence."""

import copy
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter import deployment
from auto_moniter.cli import build_parser
from auto_moniter.cli import _run
from auto_moniter.requirements_review import requirement_hash
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.preflight import validate_task_type
from console.acceptance import scan
from console.tasks import TaskError, TaskStore
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class DeploymentTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.run = self.root / 'run'
        self.requirement = 'Build on build.example in /srv/build and deploy to target.example'
        self.env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = TaskStore(self.root / 'console', self.project)

    def report(self):
        path = deployment.evidence_path(self.project, self.run)
        path.parent.mkdir(parents=True, exist_ok=True)
        for stage in deployment.STAGES:
            (path.parent / (stage + '.log')).write_text('Fixture command exited 0; observed result\n')
        value = {
            'run_id': self.run.name, 'requirement_sha256': requirement_hash(self.requirement),
            'source_version': 'fixture-commit',
            'artifact': {'name': 'fixture.tar.gz', 'sha256': 'a' * 64},
            'target': {'host': 'target.example', 'install_dir': '/srv/app', 'version': 'fixture-commit'},
            'stages': [{'id': s, 'status': 'passed', 'summary': 'Fixture success', 'evidence': [s + '.log']}
                       for s in deployment.STAGES],
        }
        path.write_text(json.dumps(value))
        return path, value

    def create(self, **extra):
        return self.store.create({'title': 'Deploy project', 'requirement': self.requirement,
                                  'task_type': 'deployment', 'verification_mode': 'executable', **extra})

    def test_creation_without_local_checks_disables_code_dispatch_and_persists_type(self):
        task = self.create(function_dispatch=True, full_function_plan=True)
        self.assertEqual(task['task_type'], 'deployment')
        self.assertFalse(task['function_dispatch'])
        self.assertEqual(task['checks'], [])
        meta = self.store._get(task['id'])
        self.assertFalse(meta['full_function_plan'])
        args = build_parser().parse_args(self.store._new_args(meta))
        self.assertEqual(args.task_type, 'deployment')
        self.assertEqual(args.git_history, 'off')
        self.assertFalse(args.function_dispatch)
        self.assertFalse(args.full_function_plan)
        restored = TaskStore(self.store.data_dir, self.project).detail(task['id'])
        self.assertEqual(restored['task_type'], 'deployment')

    def test_guided_deployment_does_not_require_or_recommend_unrelated_local_tests(self):
        (self.project / 'test_app.mjs').write_text("import {test} from 'node:test'; test('ok',()=>{});")
        with patch('subprocess.Popen', side_effect=AssertionError('Scan must not execute anything')):
            found = scan(self.project, 'deployment')
        self.assertTrue(found['checks'])
        self.assertTrue(all(not c['recommended'] for c in found['checks']))
        plan = {**found, 'criteria': 'Target runs the requested version', 'selected_ids': []}
        task = self.create(acceptance_plan=plan)
        self.assertEqual(self.store._get(task['id'])['acceptance'], plan['criteria'])
        self.assertEqual(task['checks'], [])
        # Older clients may carry the category only in the scan plan.
        inferred = self.store.create({'title': 'Deploy', 'requirement': self.requirement,
                                     'acceptance_plan': plan, 'verification_mode': 'executable'})
        self.assertEqual(inferred['task_type'], 'deployment')

    def test_cli_deployment_disables_default_function_dispatch_and_requires_review(self):
        args = build_parser().parse_args(['run', self.requirement, '--cwd', str(self.project),
                                         '--task-type=deployment', '--reviewer=claude'])
        with patch('auto_moniter.cli.resolve_provider', return_value=ScriptedProvider('worker', [])), \
                patch('auto_moniter.cli.Supervisor') as runner, contextlib.redirect_stdout(io.StringIO()):
            runner.return_value.run.return_value.status = 'done'
            runner.return_value.run.return_value.stop_reason = 'done'
            runner.return_value.run.return_value.rounds = 1
            runner.return_value.run.return_value.summary = 'Fixture deployment'
            self.assertEqual(_run(args), 0)
            config = runner.call_args.args[0]
        self.assertEqual(config.task_type, 'deployment')
        self.assertFalse(config.function_dispatch)
        validate_task_type(config)
        self.assertEqual(config.git_history, 'off')
        config.no_review = True
        with self.assertRaisesRegex(ValueError, 'independent reviewer'):
            validate_task_type(config)

    def test_invalid_categories_and_conflicting_execution_modes_are_rejected_before_save(self):
        for extra in ({'task_type': 'unknown'}, {'task_type': []}, {'workflow': 'legacy'},
                      {'parallel_project': True}, {'verification_mode': 'planning'},
                      {'verification_mode': 'review_only'}):
            with self.subTest(extra=extra), patch.object(self.store, '_save') as save:
                with self.assertRaises(TaskError):
                    self.create(**extra)
                save.assert_not_called()
        found = scan(self.project, 'code')
        with self.assertRaisesRegex(TaskError, '不一致'):
            self.create(acceptance_plan={**found, 'criteria': 'done', 'selected_ids': []})

    def test_receipt_requires_every_phase_current_run_and_nonempty_confined_logs(self):
        path, valid = self.report()
        self.assertEqual(deployment.validate(self.project, self.run, self.requirement), valid)
        mutations = []
        for stage in deployment.STAGES:
            value = copy.deepcopy(valid)
            value['stages'][deployment.STAGES.index(stage)]['status'] = 'failed'
            mutations.append(value)
        for stage in ('preflight', 'deploy', 'verify'):
            value = copy.deepcopy(valid)
            value['stages'][deployment.STAGES.index(stage)]['status'] = 'skipped'
            mutations.append(value)
        mutations += [[], {**valid, 'run_id': 'another-run'}, {**valid, 'requirement_sha256': 'b' * 64},
                      {**valid, 'artifact': {'name': 'package', 'sha256': 'unknown'}},
                      {**valid, 'target': {'host': 'target.example'}}]
        for value in mutations:
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    deployment.validate(self.project, self.run, self.requirement)
        path.write_text(json.dumps(valid))
        log = path.parent / 'verify.log'
        for replacement in ('empty', 'symlink', 'missing'):
            log.unlink(missing_ok=True)
            if replacement == 'empty':
                log.touch()
            elif replacement == 'symlink':
                outside = self.root / 'unrelated.log'
                outside.write_text('Unrelated evidence')
                log.symlink_to(outside)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                deployment.validate(self.project, self.run, self.requirement)

    def run_deployment(self, *, receipt=True, supplemental=None, resume=False, task_type='deployment', tamper_log=False):
        case = self
        class Worker(ScriptedProvider):
            def _command(self, *args):
                command = super()._command(*args)
                if receipt:
                    case.report()
                else:
                    (case.project / 'deployment-note.md').write_text('Fixture agent claims success\n')
                return command
        rel = str(deployment.evidence_path(self.project, self.run).relative_to(self.project))
        if not receipt:
            rel = 'deployment-note.md'
        worker = Worker('worker', [{'verdict': _verdict('done', evidence=[rel])}])
        ready = resume and json.loads((self.run / 'state.json').read_text()).get('requirements_review', {}).get('status') == 'ready'
        review_scripts = [] if ready else [{'verdict': {}, 'text': json.dumps(BRIEF)}]
        review_scripts.append({'verdict': _verdict('done', evidence=[rel])})
        class Reviewer(ScriptedProvider):
            def _command(self, *args):
                if tamper_log and self.calls:
                    log = deployment.evidence_path(case.project, case.run).parent / 'verify.log'
                    log.write_text('The service stopped after candidate checks\n')
                return super()._command(*args)
        reviewer = Reviewer('reviewer', review_scripts)
        supervisor = Supervisor(RunConfig(
            self.requirement, self.project, worker, reviewer=reviewer, task_type=task_type,
            workflow='auto' if resume else 'review-first', run_dir=self.run,
            max_rounds=1, retry_delay_s=0, checks=supplemental or [], resume=resume), log=lambda _: None)
        return supervisor.run(), worker, reviewer

    def test_operational_delivery_reaches_worker_and_review_without_source_function_or_git(self):
        result, worker, reviewer = self.run_deployment()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertTrue(all(call['read_only'] for call in reviewer.calls))
        self.assertIn('preflight -> build -> package -> deploy -> verify', worker.calls[0]['prompt'])
        self.assertIn('not a blocker', reviewer.calls[0]['prompt'])
        self.assertIn('watcher validates report structure', reviewer.calls[1]['prompt'])
        state = json.loads((self.run / 'state.json').read_text())
        self.assertEqual(state['task_type'], 'deployment')
        self.assertFalse(state['function_dispatch'])
        self.assertFalse((self.run / 'git-history.json').exists())
        self.assertEqual(list(self.project.glob('*.py')), [])

    def test_missing_remote_evidence_cannot_pass_even_if_both_agents_claim_done(self):
        # Existing source is valid file evidence, but is not proof of deployment.
        self.report()
        deployment.evidence_path(self.project, self.run).unlink()
        result, _, _ = self.run_deployment(receipt=False, supplemental=['true'])
        self.assertNotEqual(result.status, 'done')
        state = json.loads((self.run / 'state.json').read_text())
        failures = [gap for row in state['rounds'] for gap in row.get('gaps', [])]
        self.assertTrue(any('Deployment acceptance blocked' in gap for gap in failures), failures)

    def test_fixed_supplemental_failure_still_prevents_delivery(self):
        result, _, _ = self.run_deployment(supplemental=['false'])
        self.assertNotEqual(result.status, 'done')

    def test_changed_git_ignored_deployment_log_invalidates_candidate_approval(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / '.gitignore').write_text('*.log\n')
        result, _, _ = self.run_deployment(tamper_log=True)
        self.assertNotEqual(result.status, 'done')
        state = json.loads((self.run / 'state.json').read_text())
        gaps = [gap for row in state['rounds'] for gap in row.get('gaps', [])]
        self.assertTrue('Workspace changed during read-only review' in result.summary
                        or any('stale' in gap for gap in gaps), (result.summary, gaps))

    def test_resume_inherits_deployment_category_and_rejects_changing_it(self):
        supervisor = Supervisor(RunConfig(
            self.requirement, self.project, ScriptedProvider('worker', []),
            reviewer=ScriptedProvider('reviewer', []), task_type='deployment',
            workflow='review-first', run_dir=self.run), log=lambda _: None)
        supervisor.request_stop()
        self.assertEqual(supervisor.run().status, 'stopped')
        failed, worker, _ = self.run_deployment(resume=True, task_type='code')
        self.assertIn(failed.status, ('invalid', 'recovery_failed'))
        self.assertIn('Task type cannot change', failed.summary)
        self.assertEqual(worker.calls, [])
        result, worker, _ = self.run_deployment(resume=True, task_type='auto')
        self.assertEqual(result.status, 'done', result.summary)
        self.assertIn('Deployment task:', worker.calls[0]['prompt'])
