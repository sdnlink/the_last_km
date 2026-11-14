"""Document analysis reaches execution without unrelated source-function gates."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter.cli import build_parser, _run
from auto_moniter.preflight import validate_task_type
from auto_moniter.supervisor import RunConfig, Supervisor
from console.acceptance import scan
from console.tasks import TaskError, TaskStore
from tests.test_supervisor import ScriptedProvider, _verdict


class DocumentationTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'docs').mkdir()
        self.original = self.project / 'docs/requirements.md'
        self.original.write_text('# Handshake\nThe handshake conforms.\n')
        self.run = self.root / 'run'
        env = patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR': str(self.root / 'state')})
        env.start()
        self.addCleanup(env.stop)
        self.store = TaskStore(self.root / 'console', self.project)
        self.payload = {'title': 'Document completeness analysis', 'task_type': 'documentation',
                        'requirement': 'Analyze requirements.md; recommend corrections. Do not modify the input.'}

    def test_web_and_cli_disable_function_gate_even_with_stale_checked_checkbox(self):
        task = self.store.create({**self.payload, 'verification_mode': 'executable',
                                  'function_dispatch': True, 'full_function_plan': True})
        meta = self.store._get(task['id'])
        self.assertFalse(meta['function_dispatch'])
        self.assertFalse(meta['full_function_plan'])
        self.assertEqual(meta['checks'], [])
        self.assertEqual(meta['verification_mode'], 'executable')
        self.assertIn('不代表', meta['acceptance_warning'])
        args = build_parser().parse_args(self.store._new_args(meta))
        self.assertEqual(args.task_type, 'documentation')
        self.assertFalse(args.function_dispatch)
        self.assertFalse(args.full_function_plan)

    def test_guided_documentation_with_no_test_entry_preserves_optional_checks(self):
        (self.project / 'test_app.mjs').write_text("import {test} from 'node:test';test('ok',()=>{});")
        found = scan(self.project, 'documentation')
        self.assertFalse(next(c for c in found['checks'] if c['id'] == 'node-test')['recommended'])
        plan = {**found, 'criteria': 'Report explains the applicable requirements and gaps', 'selected_ids': []}
        task = self.store.create({**self.payload, 'acceptance_plan': plan, 'verification_mode': 'executable'})
        self.assertFalse(task['function_dispatch'])
        with_checks = self.store.create({**self.payload, 'acceptance_plan': plan,
                                        'checks': ['test -f docs/requirements.md'], 'verification_mode': 'executable'})
        self.assertEqual(with_checks['checks'], ['test -f docs/requirements.md'])
        legacy_client = self.store.create({k: v for k, v in {**self.payload, 'acceptance_plan': plan}.items()
                                          if k != 'task_type'})
        self.assertEqual(legacy_client['task_type'], 'documentation')
        self.assertFalse(legacy_client['function_dispatch'])

    def test_planning_and_semantic_only_boundaries_remain_explicit(self):
        for mode in ('planning', 'review_only'):
            task = self.store.create({**self.payload, 'verification_mode': mode, 'function_dispatch': True})
            self.assertFalse(task['function_dispatch'])
            self.assertEqual(task['verification_mode'], mode)
            self.assertEqual(task['checks'], [])
        with self.assertRaisesRegex(TaskError, '不能附带执行检查'):
            self.store.create({**self.payload, 'verification_mode': 'review_only', 'checks': ['true']})

    def test_cli_defaults_documentation_to_document_delivery_and_rejects_explicit_function_gate(self):
        args = build_parser().parse_args(['run', self.payload['requirement'], '--cwd', str(self.project),
                                         '--task-type=documentation', '--reviewer=claude'])
        with patch('auto_moniter.cli.resolve_provider', return_value=ScriptedProvider('worker', [])), \
                patch('auto_moniter.cli.Supervisor') as runner, contextlib.redirect_stdout(io.StringIO()):
            result = runner.return_value.run.return_value
            result.status, result.stop_reason, result.rounds, result.summary = 'done', 'done', 1, 'Fixture'
            self.assertEqual(_run(args), 0)
            config = runner.call_args.args[0]
        self.assertEqual(config.task_type, 'documentation')
        self.assertFalse(config.function_dispatch)
        validate_task_type(config)
        config.function_dispatch = True
        with self.assertRaisesRegex(ValueError, 'document deliverables'):
            validate_task_type(config)

    def execute(self, *, checks=(), history='off', brief_status='ready', missing_report=False, resume=False):
        case = self
        text = ('# Analysis\nInput: docs/requirements.md, Handshake section.\n'
                'Handshake conformance does not establish conformance with all device requirements.\n'
                'Recommendation: document applicable requirements and evidence for each.\n'
                'Product compliance remains unverified.\n')
        class Worker(ScriptedProvider):
            def _command(self, *args):
                if not missing_report:
                    (case.project / 'docs/analysis.md').write_text(text)
                return super()._command(*args)
        class Reviewer(ScriptedProvider):
            def _command(self, session, prompt_file, read_only):
                if self.calls and history == 'required':
                    checkpoint = re.search(r'Worker checkpoint: ([0-9a-f]{40,64})', prompt_file.read_text())
                    if checkpoint:
                        self.scripts[0]['verdict']['evidence'].append(checkpoint[1])
                return super()._command(session, prompt_file, read_only)
        brief = {'status': brief_status, 'summary': 'Analyze documents and deliver a referenced report',
                 'refined_requirement': self.payload['requirement'],
                 'acceptance_criteria': ['Preserve the input', 'Explain document coverage gaps with references'],
                 'constraints': ['Analysis only; do not implement product fixes'], 'assumptions': [],
                 'blockers': ['Required normative source is inaccessible'] if brief_status == 'needs_input' else []}
        worker = Worker('worker', [{'verdict': _verdict('done', evidence=['docs/analysis.md'])}])
        reviewer = Reviewer('reviewer', [{'verdict': {}, 'text': json.dumps(brief)},
                                        {'verdict': _verdict('done', evidence=['docs/analysis.md'])}])
        result = Supervisor(RunConfig(self.payload['requirement'], self.project, worker, reviewer=reviewer,
            task_type='auto' if resume else 'documentation', workflow='auto' if resume else 'review-first',
            run_dir=self.run, git_history=history, resume=resume,
            checks=list(checks), retry_delay_s=0, max_rounds=1), log=lambda _: None).run()
        return result, worker, reviewer

    def test_analysis_with_findings_is_delivered_without_modifying_input_or_creating_functions(self):
        before = self.original.read_bytes()
        result, worker, reviewer = self.execute()
        self.assertEqual(result.status, 'done', result.summary)
        self.assertEqual(self.original.read_bytes(), before)
        self.assertTrue((self.project / 'docs/analysis.md').is_file())
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertTrue(all(c['read_only'] for c in reviewer.calls))
        self.assertIn('not prerequisites', reviewer.calls[0]['prompt'])
        self.assertIn('separate analysis report', worker.calls[0]['prompt'])
        self.assertIn('not itself a reason to block delivery', reviewer.calls[1]['prompt'])
        state = json.loads((self.run / 'state.json').read_text())
        self.assertFalse(state['function_dispatch'])
        self.assertEqual(state['function_tasks'], {'current': None, 'accepted': []})

    def test_document_report_passes_web_git_publication_and_fixed_checks(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        result, _, _ = self.execute(checks=['test -f docs/analysis.md'], history='required')
        self.assertEqual(result.status, 'done', result.summary)
        self.assertTrue(json.loads((self.run / 'git-history.json').read_text())['head'])

    def test_fixed_failed_check_still_prevents_acceptance(self):
        result, _, _ = self.execute(checks=['false'])
        self.assertNotEqual(result.status, 'done')

    def test_missing_report_and_real_missing_sources_are_not_accepted(self):
        result, worker, _ = self.execute(brief_status='needs_input')
        self.assertEqual(result.status, 'action_required')
        self.assertEqual(worker.calls, [])

    def test_done_claim_without_a_delivered_report_is_rejected(self):
        result, _, _ = self.execute(missing_report=True)
        self.assertNotEqual(result.status, 'done')

    def test_resume_retains_document_delivery_without_function_dispatch(self):
        supervisor = Supervisor(RunConfig(self.payload['requirement'], self.project, ScriptedProvider('worker', []),
            reviewer=ScriptedProvider('reviewer', []), task_type='documentation', workflow='review-first',
            run_dir=self.run, git_history='off'), log=lambda _: None)
        supervisor.request_stop()
        self.assertEqual(supervisor.run().status, 'stopped')
        result, worker, _ = self.execute(resume=True)
        self.assertEqual(result.status, 'done', result.summary)
        self.assertIn('Documentation task:', worker.calls[0]['prompt'])
        self.assertFalse(json.loads((self.run / 'state.json').read_text())['function_dispatch'])
