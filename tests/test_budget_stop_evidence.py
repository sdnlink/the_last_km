"""A budget stop must keep the interrupted round's captured logs and check snapshot."""
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from auto_moniter.providers import PreparedCommand, Provider, TurnResult, resolve_provider
from auto_moniter.supervisor import BudgetExceeded, RunConfig, Supervisor
from console.tasks import TaskStore, tail
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class SlowThenScript(Provider):
    def __init__(self, name, verdict, *, pause=0.05, touch=''):
        super().__init__(name, 'scripted', True)
        self.verdict = verdict
        self.pause = pause
        self.touch = touch
        self.calls = []

    def start_command(self, prompt_file, cwd, schema_file, *, model='', extra_args=(), read_only=False):
        self.calls.append(prompt_file.read_text(encoding='utf-8'))
        payload = {'type': 'result', 'session_id': self.name + '-session', 'result': json.dumps(self.verdict)}
        touch = ''
        if self.touch:
            touch = f"Path({str(prompt_file.parent.parent)!r}, {self.touch!r}).write_text('changed\\n')"
        script = prompt_file.with_suffix('.slow.py')
        script.write_text(textwrap.dedent(f"""
            import json, time
            from pathlib import Path
            print('partial diagnostic before budget', flush=True)
            print('secret=sk-secret-token', flush=True)
            time.sleep({self.pause})
            {touch}
            print(json.dumps({payload!r}))
        """), encoding='utf-8')
        return PreparedCommand(['python3', str(script)])

    def resume_command(self, *args, **kwargs):
        return self.start_command(*args, **kwargs)


class BudgetStopEvidenceTests(unittest.TestCase):
    def test_interrupted_roles_keep_logs_and_do_not_continue(self):
        roles = {
            'requirements': lambda root, run: self._requirements(root, run),
            'worker': lambda root, run: self._worker(root, run),
            'reviewer': lambda root, run: self._reviewer(root, run),
            'check': lambda root, run: self._check(root, run),
        }
        for name, build in roles.items():
            with self.subTest(role=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / 'run'
                report, expected = build(root, run)
                self.assertEqual(report.status, 'budget_exceeded', report.summary)
                self.assertFalse(any(record.verdict.status == 'done' and report.status == 'done' for record in report.records))
                ident = 'a' * 32
                store = TaskStore(root / 'console-data', root)
                (store.tasks_dir / f'{ident}.json').write_text(json.dumps({
                    'id': ident, 'run_dir': str(run), 'status': 'budget_exceeded', 'title': 't',
                }))
                logs = store.logs(ident)
                self.assertIn(expected, logs['files'])
                selected = store.logs(ident, expected)
                self.assertIn('partial diagnostic before budget', selected['content'])
                self.assertNotIn('sk-secret-token', selected['content'])
                self.assertLessEqual(len(selected['content'].encode()), selected['limit_bytes'])
                self.assertEqual(tail(run / expected), selected['content'])

    def _requirements(self, root, run):
        reviewer = SlowThenScript('reviewer', BRIEF, pause=0.05)
        worker = ScriptedProvider('worker', [])
        clock = {'now': 0.0}
        monitor = Supervisor(RunConfig(
            'original request', root, worker, reviewer=reviewer, workflow='review-first',
            run_dir=run, git_history='off', max_runtime_s=0.05, retry_delay_s=0,
        ), log=lambda _: None)
        monitor._elapsed = lambda: clock['now']
        original = monitor.run_command if False else __import__('auto_moniter.supervisor', fromlist=['run_command']).run_command

        def expire(prepared, cwd, **kwargs):
            # Advance the clock before the interrupted result returns, so the
            # budget exception is raised from the captured partial output.
            clock['now'] = 1
            kwargs = dict(kwargs)
            should_stop = kwargs.get('should_stop')

            def stop_mid_output():
                return True if should_stop is None else should_stop()

            kwargs['should_stop'] = stop_mid_output
            result = original(prepared, cwd, **kwargs)
            result.exit_code = 9
            result.interruption = 'runtime budget'
            result.stdout = 'partial diagnostic before budget\n' + result.stdout
            return result

        import auto_moniter.supervisor as supervisor_module
        supervisor_module.run_command = expire
        try:
            report = monitor.run()
        finally:
            supervisor_module.run_command = original
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        log = (run / '000-reviewer.log').read_text(encoding='utf-8')
        self.assertIn('exit=9', log)
        self.assertIn('interruption=runtime budget', log)
        self.assertFalse((run / 'acceptance-results.json').exists())
        return report, '000-reviewer.log'

    def _worker(self, root, run):
        reviewer = ScriptedProvider('reviewer', [{'verdict': {}, 'text': json.dumps(BRIEF)}])
        worker = SlowThenScript('worker', _verdict('done', evidence=['app.py']), pause=0.05, touch='app.py')
        clock = {'now': 0.0}
        monitor = Supervisor(RunConfig(
            'original request', root, worker, reviewer=reviewer, workflow='review-first',
            run_dir=run, git_history='off', max_runtime_s=10, retry_delay_s=0,
        ), log=lambda _: None)
        monitor._elapsed = lambda: clock['now']
        import auto_moniter.supervisor as supervisor_module
        original = supervisor_module.run_command
        seen = {'worker': 0}

        def expire(prepared, cwd, **kwargs):
            worker_turn = any('001-worker' in part for part in prepared.argv)
            kwargs = dict(kwargs)
            if worker_turn:
                seen['worker'] += 1
                clock['now'] = 10
                should_stop = kwargs.get('should_stop')

                def stop_mid_output():
                    return True if should_stop is None else should_stop()

                kwargs['should_stop'] = stop_mid_output
            result = original(prepared, cwd, **kwargs)
            if worker_turn:
                result.exit_code = 9
                result.interruption = 'runtime budget'
                result.stdout = 'partial diagnostic before budget\n' + result.stdout
            return result

        supervisor_module.run_command = expire
        try:
            report = monitor.run()
        finally:
            supervisor_module.run_command = original
        self.assertEqual(seen['worker'], 1)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 1)
        log = (run / '001-worker.log').read_text(encoding='utf-8')
        self.assertIn('exit=9', log)
        self.assertIn('interruption=runtime budget', log)
        self.assertFalse((run / 'acceptance-results.json').exists())
        self.assertFalse(any('check' in call['prompt'] for call in reviewer.calls[1:]))
        return report, '001-worker.log'

    def _reviewer(self, root, run):
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        reviewer = SlowThenScript('reviewer', _verdict('done', evidence=['app.py']), pause=0.05)
        clock = {'now': 0.0}
        monitor = Supervisor(RunConfig(
            'original request', root, worker, reviewer=reviewer, workflow='legacy',
            run_dir=run, git_history='off', max_runtime_s=10, retry_delay_s=0,
        ), log=lambda _: None)
        monitor._elapsed = lambda: clock['now']
        import auto_moniter.supervisor as supervisor_module
        original = supervisor_module.run_command

        def expire(prepared, cwd, **kwargs):
            reviewing = any('001-reviewer' in part for part in prepared.argv)
            kwargs = dict(kwargs)
            if reviewing:
                clock['now'] = 10
                should_stop = kwargs.get('should_stop')

                def stop_mid_output():
                    return True if should_stop is None else should_stop()

                kwargs['should_stop'] = stop_mid_output
            result = original(prepared, cwd, **kwargs)
            if reviewing:
                result.exit_code = 9
                result.interruption = 'runtime budget'
                result.stdout = 'partial diagnostic before budget\n' + result.stdout
            return result

        supervisor_module.run_command = expire
        try:
            report = monitor.run()
        finally:
            supervisor_module.run_command = original
        self.assertEqual(report.status, 'budget_exceeded', report.summary)
        self.assertEqual(len(worker.calls), 1, worker.calls)
        self.assertEqual(len(reviewer.calls), 1, reviewer.calls)
        log = (run / '001-reviewer.log').read_text(encoding='utf-8')
        self.assertIn('exit=9', log)
        self.assertIn('interruption=runtime budget', log)
        self.assertFalse((run / 'acceptance-results.json').exists())
        return report, '001-reviewer.log'

    def _check(self, root, run):
        old = {
            'round': 9, 'tree': 'stale', 'scope': 'requirement', 'complete': True,
            'checks': [{'command': 'true', 'status': 'passed', 'exit_code': 0}],
            'note': 'stale success',
        }
        # The supervisor creates the run directory; plant the stale snapshot via a hook.
        worker = ScriptedProvider('worker', [{'verdict': _verdict('done', evidence=['app.py']), 'touch': 'app.py'}])
        monitor = Supervisor(RunConfig(
            'task', root, worker, no_review=True, checks=['echo partial diagnostic before budget; sleep 2'],
            run_dir=run, git_history='off', max_runtime_s=0.3, retry_delay_s=0,
        ), log=lambda _: None)
        original = monitor._delivery_checks

        def plant(*args, **kwargs):
            (run / 'acceptance-results.json').write_text(json.dumps(old))
            return original(*args, **kwargs)

        monitor._delivery_checks = plant
        # Start the budget only after the check has emitted its diagnostic.
        # Worker startup time must not prevent reaching the boundary under test.
        clock = {'elapsed':0.0}
        monitor._elapsed = lambda: clock['elapsed']
        import auto_moniter.supervisor as supervisor_module
        original_command = supervisor_module.run_command
        def expire_after_output(prepared, cwd, **kwargs):
            if prepared.argv[:2] == ['sh','-c']:
                feed = kwargs['on_output']
                def output(stream, chunk):
                    feed(stream,chunk)
                    clock['elapsed'] = 1.0
                kwargs['on_output'] = output
            return original_command(prepared,cwd,**kwargs)
        started = time.monotonic()
        with patch('auto_moniter.supervisor.run_command',side_effect=expire_after_output):
            report = monitor.run()
        self.assertLess(time.monotonic() - started, 5)
        results = json.loads((run / 'acceptance-results.json').read_text())
        self.assertFalse(results['complete'])
        self.assertNotEqual(results.get('tree'), 'stale')
        self.assertTrue(results['checks'])
        self.assertNotEqual(results['checks'][0]['status'], 'passed')
        self.assertTrue((run / '001-check-01.log').is_file())
        self.assertIn('partial diagnostic before budget', (run / '001-check-01.log').read_text())
        return report, '001-check-01.log'

    def test_paseo_final_transcript_replaces_launch_log(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / 'run'
                clock = {'now': 0.0}
                provider = _OfflinePaseo(stopped=stopped)
                monitor = Supervisor(RunConfig(
                    'task', root, provider, no_review=True, workflow='legacy',
                    run_dir=run, git_history='off', max_runtime_s=10, retry_delay_s=0,
                    max_rounds=1, failure_limit=1,
                ), log=lambda _: None)
                monitor._elapsed = lambda: clock['now']
                import auto_moniter.supervisor as supervisor_module
                original_command = supervisor_module.run_command
                original_transcript = supervisor_module.paseo_transcript
                original_stop = supervisor_module._paseo_stop

                def offline(prepared, cwd, **kwargs):
                    if 'inspect' in prepared.argv:
                        provider.inspects += 1
                        if stopped:
                            clock['now'] = 10
                            return TurnResult('', stdout='{"status":"running"}', exit_code=0)
                        return TurnResult('', stdout='{"status":"completed"}', exit_code=0)
                    return original_command(prepared, cwd, **kwargs)

                supervisor_module.paseo_transcript = lambda *args, **kwargs: 'final paseo conclusion'
                supervisor_module._paseo_stop = lambda *args, **kwargs: True
                supervisor_module.run_command = offline
                try:
                    report = monitor.run()
                finally:
                    supervisor_module.run_command = original_command
                    supervisor_module.paseo_transcript = original_transcript
                    supervisor_module._paseo_stop = original_stop
                self.assertIs(supervisor_module._paseo_stop, original_stop)
                log = (run / '001-worker.log').read_text(encoding='utf-8')
                text = (run / '001-worker-text.md').read_text(encoding='utf-8')
                ident = 'b' * 32
                store = TaskStore(root / 'console-data', root)
                (store.tasks_dir / f'{ident}.json').write_text(json.dumps({
                    'id': ident, 'run_dir': str(run), 'status': report.status, 'title': 't',
                }))
                selected = store.logs(ident, '001-worker.log')
                self.assertIn('final paseo conclusion', log)
                self.assertIn('final paseo conclusion', text)
                self.assertIn('final paseo conclusion', selected['content'])
                self.assertNotIn('launch-only output', log)
                if stopped:
                    self.assertEqual(report.status, 'budget_exceeded')
                    self.assertIn('interruption=runtime budget', log)
                    self.assertIn('exit=-1', log)
                else:
                    self.assertIn('exit=0', log)
                    self.assertIn('interruption=-', log)
                self.assertEqual(provider.inspects, 1)

    def test_paseo_keeps_partial_transcript_when_later_read_is_empty(self):
        self._paseo_edge(empty_after_capture=True, stop_ok=True)

    def test_paseo_stop_failure_saves_captured_text_before_recovery_error(self):
        self._paseo_edge(empty_after_capture=False, stop_ok=False)

    def _paseo_edge(self, *, empty_after_capture, stop_ok):
        import auto_moniter.supervisor as supervisor_module
        before_stop = supervisor_module._paseo_stop
        before_transcript = supervisor_module.paseo_transcript
        before_command = supervisor_module.run_command
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run'
            clock = {'now': 0.0}
            provider = _OfflinePaseo()
            monitor = Supervisor(RunConfig(
                'task', root, provider, no_review=True, workflow='legacy',
                run_dir=run, git_history='off', max_runtime_s=10, retry_delay_s=0,
                max_rounds=1, failure_limit=1,
            ), log=lambda _: None)
            monitor._elapsed = lambda: clock['now']
            reads = {'n': 0}

            def transcript(*args, **kwargs):
                reads['n'] += 1
                if empty_after_capture and reads['n'] > 1:
                    return ''
                return 'partial paseo transcript before budget'

            def offline(prepared, cwd, **kwargs):
                if 'inspect' in prepared.argv:
                    provider.inspects += 1
                    if provider.inspects >= 1:
                        clock['now'] = 10
                    return TurnResult('', stdout='{"status":"running"}', exit_code=0)
                return before_command(prepared, cwd, **kwargs)

            supervisor_module.paseo_transcript = transcript
            supervisor_module._paseo_stop = lambda *args, **kwargs: stop_ok
            supervisor_module.run_command = offline
            try:
                report = monitor.run()
            finally:
                supervisor_module.run_command = before_command
                supervisor_module.paseo_transcript = before_transcript
                supervisor_module._paseo_stop = before_stop
            self.assertIs(supervisor_module._paseo_stop, before_stop)
            log = (run / '001-worker.log').read_text(encoding='utf-8')
            text = (run / '001-worker-text.md').read_text(encoding='utf-8')
            ident = 'c' * 32
            store = TaskStore(root / 'console-data', root)
            (store.tasks_dir / f'{ident}.json').write_text(json.dumps({
                'id': ident, 'run_dir': str(run), 'status': report.status, 'title': 't',
            }))
            selected = store.logs(ident, '001-worker.log')
            self.assertIn('partial paseo transcript before budget', log)
            self.assertIn('partial paseo transcript before budget', text)
            self.assertIn('partial paseo transcript before budget', selected['content'])
            self.assertNotIn('launch-only output', log)
            self.assertIn('interruption=', log)
            self.assertNotIn('exit=0', log)
            self.assertNotEqual(report.status, 'done')
            self.assertEqual(provider.calls if hasattr(provider, 'calls') else [], [])
            if not stop_ok:
                self.assertEqual(report.status, 'recovery_failed')
                self.assertIn('Cannot stop interrupted Paseo', report.summary)


class _OfflinePaseo(Provider):
    def __init__(self, *, stopped=False):
        super().__init__('paseo', 'scripted', True)
        self.stopped = stopped
        self.inspects = 0
        self.binary = 'paseo-offline'

    def start_command(self, prompt_file, cwd, schema_file, *, model='', extra_args=(), read_only=False):
        script = prompt_file.with_suffix('.launch.py')
        script.write_text('print(\'{"agentId": "11111111-1111-1111-1111-111111111111", "text": "launch-only output"}\')\n', encoding='utf-8')
        return PreparedCommand([sys.executable, str(script)])

    def resume_command(self, *args, **kwargs):
        return self.start_command(*args, **kwargs)


if __name__ == '__main__':
    unittest.main()
