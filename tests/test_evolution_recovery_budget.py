"""Exercise recovery budget boundaries through Evolution.run and the evolve CLI."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from auto_moniter.evolution import Evolution, git
from auto_moniter.processes import _members, _stat, process_identity
from auto_moniter.providers import PreparedCommand
from auto_moniter.storage import write_json
from auto_moniter.supervisor import run_command
from tests import test_console_restart as console_fixtures
from tests import test_evolution_cli_recovery as cli_fixtures
from tests.test_evolution_phase_execution import PowerLoss


class EvolutionRecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.cli = cli_fixtures.EvolutionCLIRecoveryTests()
        self.addCleanup(self.cli.doCleanups)
        self.cli.setUp()
        self.fixture = self.cli.fixture

    def child_checkpoint(self):
        monitor, state, _ = self.cli.phases.running_child_with_heartbeat(self.fixture, 0)
        path = self.fixture.engine.path
        outer = json.loads(path.read_text())
        outer['elapsed_s'] = outer['usage']['elapsed_s'] = 10
        write_json(path, outer)
        state['usage'].update(max_runtime_s=84.75, elapsed_s=80)
        child = monitor / 'state.json'
        write_json(child, state)
        return path, child, state['usage']['agent_calls']

    def invoke_with_preflight_time(self, method, seconds):
        started = now = 1000
        clock = SimpleNamespace(monotonic=lambda: now)
        original = getattr(Evolution, method)

        def advance(*args, **kwargs):
            nonlocal now
            now = started + seconds
            return original(*args, **kwargs)

        with patch('auto_moniter.cli.time', clock), patch('auto_moniter.evolution.time', clock), \
             patch.object(Evolution, method, advance):
            return self.cli.invoke()

    def assert_source_and_cycle(self, path, head, count):
        self.assertEqual(git(self.fixture.source, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.fixture.source, 'rev-list', '--count', 'HEAD'), str(count))
        self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])

    def test_child_history_plus_preflight_exhausts_original_grant(self):
        path, child, calls = self.child_checkpoint()
        child_bytes = child.read_bytes()
        with patch('auto_moniter.evolution.Supervisor') as supervisor:
            code, cycles = self.invoke_with_preflight_time('_recovery_monitor', 6)
        supervisor.assert_not_called()
        self.assertEqual(code, 1, cycles)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]['phase'], 'stopped')
        self.assertEqual(cycles[0]['elapsed_s'], 91)
        self.assertEqual(cycles[0]['usage']['max_runtime_s'], 89.75)
        self.assertEqual(cycles[0]['agent_calls'], calls)
        self.assertEqual(child.read_bytes(), child_bytes)
        self.assert_source_and_cycle(path, self.fixture.base, 1)

    def test_second_interruption_only_charges_new_preflight_time(self):
        path, child, calls = self.child_checkpoint()
        child_bytes = child.read_bytes()
        original = Evolution._save

        def interrupt(engine, phase, **fields):
            original(engine, phase, **fields)
            if phase == 'implementing':
                raise PowerLoss('recovery accounting persisted')

        with patch.object(Evolution, '_save', interrupt), \
             patch('auto_moniter.evolution.Supervisor') as supervisor:
            for duration, expected in ((1, 86), (2, 88)):
                with self.subTest(duration=duration), self.assertRaises(PowerLoss):
                    self.invoke_with_preflight_time('_recovery_monitor', duration)
                saved = json.loads(path.read_text())
                self.assertEqual(saved['elapsed_s'], expected)
                self.assertEqual(saved['agent_calls'], calls)
        supervisor.assert_not_called()
        self.assertEqual(child.read_bytes(), child_bytes)
        self.assert_source_and_cycle(path, self.fixture.base, 1)

    def test_preflight_exhaustion_stops_activation_of_fast_forwarded_candidate(self):
        path = self.cli.phases.interrupt(self.fixture, 'activating')
        saved = json.loads(path.read_text())
        saved['usage']['elapsed_s'] = saved['elapsed_s'] = 84
        write_json(path, saved)
        self.assert_source_and_cycle(path, saved['candidate'], 2)

        with patch.object(Evolution, '_activate', return_value=True) as activate, \
             patch('auto_moniter.evolution.Supervisor') as supervisor:
            code, cycles = self.invoke_with_preflight_time('_confirm_review', 6)
        supervisor.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(code, 1, cycles)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]['phase'], 'stopped')
        self.assertEqual(cycles[0]['resume_phase'], 'activating')
        self.assertEqual(cycles[0]['elapsed_s'], 90)
        self.assertEqual(cycles[0]['agent_calls'], saved['agent_calls'])
        self.assert_source_and_cycle(path, saved['candidate'], 2)

    def test_direct_run_counts_time_spent_selecting_the_cycle(self):
        path, child, calls = self.child_checkpoint()
        child_bytes = child.read_bytes()
        engine, _ = self.cli.phases.resumer(self.fixture)
        original = engine._pending_cycle
        now = 1000
        clock = SimpleNamespace(monotonic=lambda: now)

        def select(base, *, locking_monitor=None):
            nonlocal now
            result = original(base, locking_monitor=locking_monitor)
            if locking_monitor is None:
                now += 6
            return result

        with patch('auto_moniter.evolution.time', clock), \
             patch.object(engine, '_pending_cycle', side_effect=select), \
             patch('auto_moniter.evolution.Supervisor') as supervisor:
            result = engine.run()
        supervisor.assert_not_called()
        self.assertEqual(result['phase'], 'stopped', result)
        self.assertEqual(result['elapsed_s'], 91)
        self.assertEqual(result['usage']['max_runtime_s'], 89.75)
        self.assertEqual(result['agent_calls'], calls)
        self.assertEqual(child.read_bytes(), child_bytes)
        self.assert_source_and_cycle(path, self.fixture.base, 1)

    def test_ready_recovery_stops_activation_if_fast_forward_uses_remaining_budget(self):
        path = self.cli.phases.interrupt(self.fixture, 'ready')
        saved = json.loads(path.read_text())
        saved['usage']['elapsed_s'] = saved['elapsed_s'] = 88.75
        write_json(path, saved)
        self.assert_source_and_cycle(path, self.fixture.base, 1)
        now = 1000
        clock = SimpleNamespace(monotonic=lambda: now)

        def spending_merge(cwd, *args):
            nonlocal now
            result = git(cwd, *args)
            if cwd == self.fixture.source and args == ('merge', '--ff-only', '--no-edit', saved['candidate']):
                now += 1
            return result

        with patch('auto_moniter.cli.time', clock), patch('auto_moniter.evolution.time', clock), \
             patch('auto_moniter.evolution.git', side_effect=spending_merge), \
             patch.object(Evolution, '_activate', return_value=True) as activate, \
             patch('auto_moniter.evolution.Supervisor') as supervisor:
            code, cycles = self.cli.invoke()
        supervisor.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(code, 1, cycles)
        self.assertEqual(len(cycles), 1)
        result = cycles[0]
        self.assertEqual(result['phase'], 'stopped')
        self.assertEqual(result['resume_phase'], 'activating')
        self.assertEqual(result['elapsed_s'], 89.75)
        self.assertEqual(result['promoted_commit'], saved['candidate'])
        self.assertEqual(result['candidate_checks'], saved['candidate_checks'])
        self.assertEqual(result['agent_calls'], saved['agent_calls'])
        self.assertEqual(json.loads(path.read_text())['resume_phase'], 'activating')
        self.assert_source_and_cycle(path, saved['candidate'], 2)

    def test_recovery_restart_uses_original_balance_and_still_rolls_back_on_failure(self):
        path = self.cli.phases.interrupt(self.fixture, 'activating')
        saved = json.loads(path.read_text())
        saved['usage']['elapsed_s'] = saved['elapsed_s'] = 88.75
        write_json(path, saved)
        now = 1000
        clock = SimpleNamespace(monotonic=lambda: now)
        observed = []

        def restart(command, cwd, *, timeout_s, should_stop, **kwargs):
            nonlocal now
            self.assertEqual(command.argv[1], '-c')
            self.assertIn('--supervised-activation', command.argv)
            self.assertEqual(cwd, self.fixture.source)
            already_stopped = should_stop()
            now += 1
            observed.append((timeout_s, already_stopped, should_stop()))
            # A success racing with the deadline must not confirm promotion.
            return SimpleNamespace(stdout='restart', stderr='', exit_code=0,
                                   timed_out=False, interruption='')

        from auto_moniter import evolution
        original_lock = evolution._lock_held

        def lock_held(lock, **kwargs):
            if lock.name == 'console.lock':
                return True
            return original_lock(lock, **kwargs)

        with patch('auto_moniter.cli.time', clock), patch('auto_moniter.evolution.time', clock), \
             patch('auto_moniter.evolution._lock_held', side_effect=lock_held), \
             patch('auto_moniter.evolution.run_command', side_effect=restart) as command, \
             patch('auto_moniter.evolution.Supervisor') as supervisor:
            code, cycles = self.cli.invoke()
        supervisor.assert_not_called()
        command.assert_called_once()
        self.assertEqual(observed, [(1, False, True)])
        self.assertEqual(code, 1, cycles)
        self.assertEqual(len(cycles), 1)
        result = cycles[0]
        self.assertEqual(result['phase'], 'rollback_activation_failed')
        self.assertEqual(result['resume_phase'], 'rolling_back')
        self.assertEqual(result['elapsed_s'], 89.75)
        self.assertEqual(result['candidate'], saved['candidate'])
        self.assertEqual(result['candidate_checks'], saved['candidate_checks'])
        self.assertEqual(result['agent_calls'], saved['agent_calls'])
        self.assertTrue((path.parent / 'activation.log').is_file())
        self.assertFalse((path.parent / 'rollback-activation.log').exists())
        self.assert_source_and_cycle(path, result['rollback_commit'], 3)
        self.assertEqual(git(self.fixture.source, 'rev-parse', 'HEAD^{tree}'),
                         git(self.fixture.source, 'rev-parse', self.fixture.base + '^{tree}'))

    def unready_console(self):
        source = self.fixture.source
        data = source / '.auto-moniter' / 'console'
        data.mkdir(parents=True)
        marker = data / 'replacement.json'
        root = Path(__file__).resolve().parents[1]
        package = source / 'console'
        (source / 'auto_moniter' / '__init__.py').write_text(
            f"__path__.append({str(root / 'auto_moniter')!r})\n")
        (package / 'server.py').write_text(
            "from argparse import ArgumentParser\n"
            "from pathlib import Path\n"
            "def build_parser():\n"
            "    parser = ArgumentParser()\n"
            "    parser.add_argument('--cwd', type=Path)\n"
            "    parser.add_argument('--data-dir', type=Path)\n"
            "    parser.add_argument('--port', type=int, default=0)\n"
            "    parser.add_argument('--runs-dir', type=Path, action='append', default=[])\n"
            "    return parser\n"
        )
        (package / '__main__.py').write_text(
            "import fcntl, os, time\n"
            "from auto_moniter.processes import process_identity\n"
            "from auto_moniter.storage import write_json\n"
            "from console.server import build_parser\n"
            "data = build_parser().parse_args().data_dir\n"
            "with (data / 'console.lock').open('a') as lock:\n"
            "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "    if 'FIRST_CONSOLE' in os.environ:\n"
            "        (data / 'old-ready').touch()\n"
            "    else:\n"
            "        write_json(data / 'replacement.json', process_identity(os.getpid()))\n"
            "    time.sleep(60)\n"
        )
        git(source, 'add', 'auto_moniter/__init__.py', 'console')
        git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
            'commit', '--amend', '--no-edit')
        self.fixture.base = git(source, 'rev-parse', 'HEAD')
        env = {**os.environ, 'PYTHONPATH': str(root)}
        env.pop('PYTHONSAFEPATH', None)
        old = subprocess.Popen([sys.executable, '-m', 'console', '--cwd', str(source),
                                '--data-dir', str(data)], cwd=package.parent,
                               env={**env, 'FIRST_CONSOLE': '1'})

        def cleanup():
            if old.poll() is None:
                old.terminate()
            old.wait(timeout=5)
            if marker.exists():
                saved_identity = json.loads(marker.read_text())
                pid = saved_identity['pid']
                if process_identity(pid) == saved_identity:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (data / 'old-ready').exists():
            self.assertIsNone(old.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)

        return marker, {**env, 'PYTHONSAFEPATH': '1'}

    def test_recovery_cancels_unready_replacement_before_rolling_back(self):
        marker, env = self.unready_console()
        path = self.cli.phases.interrupt(self.fixture, 'activating')
        saved = json.loads(path.read_text())
        saved['usage']['elapsed_s'] = saved['elapsed_s'] = 84.75
        write_json(path, saved)
        source = self.fixture.source
        clock = SimpleNamespace(monotonic=lambda: 1005 if marker.exists() else 1000)
        with patch.dict(os.environ, env), patch('auto_moniter.cli.time', clock), \
             patch('auto_moniter.evolution.time', clock), \
             patch('auto_moniter.evolution.run_command', wraps=run_command) as restart:
            code, cycles = self.cli.invoke()
        restart.assert_called_once()
        replacement = json.loads(marker.read_text())
        state = _stat(replacement['pid'])
        self.assertTrue(state is None or state['state'] == 'Z', state)
        self.assertEqual(code, 1, cycles)
        result = cycles[0]
        self.assertEqual(result['phase'], 'rollback_activation_failed')
        self.assertEqual(result['resume_phase'], 'rolling_back')
        self.assertEqual(result['candidate_checks'], saved['candidate_checks'])
        self.assert_source_and_cycle(path, result['rollback_commit'], 3)
        self.assertEqual(git(source, 'rev-parse', 'HEAD^{tree}'),
                         git(source, 'rev-parse', self.fixture.base + '^{tree}'))

    def test_sigterm_to_cli_stops_unready_publication_and_prevents_resume(self):
        marker, env = self.unready_console()
        path = self.cli.phases.interrupt(self.fixture, 'activating')
        saved = json.loads(path.read_text())
        launch = (
            "import os, sys\n"
            "from unittest.mock import patch\n"
            "from auto_moniter.cli import main\n"
            "from tests.test_evolution import EvolutionWorker, EvolutionReviewer\n"
            "os.environ['AUTO_MONITER_STATE_DIR'] = sys.argv.pop(1)\n"
            "def fixture_provider(_name, *, role='worker'):\n"
            "    cls = EvolutionReviewer if role == 'reviewer' else EvolutionWorker\n"
            "    return cls(role, [])\n"
            "with patch('auto_moniter.cli.resolve_provider', side_effect=fixture_provider), "
            "patch('auto_moniter.evolution.Supervisor', side_effect=AssertionError('unexpected dispatch')):\n"
            "    raise SystemExit(main(sys.argv[1:]))\n"
        )
        args = ['evolve', '--cwd', str(self.fixture.source), '--evidence-root',
                str(self.fixture.evidence), '--max-runtime', '90s', '--max-agent-calls', '8']
        cli = subprocess.Popen([sys.executable, '-c', launch, env['AUTO_MONITER_STATE_DIR'], *args], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 12
            while not marker.exists() and cli.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            if not marker.exists():
                if cli.poll() is None:
                    cli.kill()
                stdout, stderr = cli.communicate()
                self.fail(f'CLI did not start the replacement: {stdout[:1000]}\n{stderr}')
            pid = json.loads(marker.read_text())['pid']
            group = _stat(pid)['group']
            cli.send_signal(signal.SIGTERM)
            stdout, stderr = cli.communicate(timeout=10)
        finally:
            if cli.poll() is None:
                cli.kill()
            cli.communicate()
        self.assertEqual(cli.returncode, 1, (stdout, stderr))
        self.assertEqual(_members(group), [])
        cycle = json.loads(stdout)['cycles'][0]
        self.assertEqual(cycle['phase'], 'stopped')
        self.assertEqual(cycle['resume_phase'], 'rolling_back')
        self.assertEqual(cycle['candidate_checks'], saved['candidate_checks'])
        self.assertFalse((path.parent / 'rollback-activation.log').exists())
        self.assert_source_and_cycle(path, cycle['rollback_commit'], 3)
        before = path.read_bytes()
        code, cycles = self.cli.invoke()
        self.assertEqual(code, 1, cycles)
        self.assertEqual(cycles[0]['phase'], 'stopped')
        self.assertEqual(path.read_bytes(), before)

    def test_supervised_restart_releases_ready_console_on_success(self):
        console = console_fixtures.ConsoleRestartTests()
        self.addCleanup(console.doCleanups)
        console.setUp()
        target = console_fixtures.ROOT
        argv = [sys.executable, '-m', 'console.restart', '--cwd', str(target),
                '--data-dir', str(console.data), '--port', '0', '--supervised-activation']
        with patch.dict(os.environ, console.environment):
            result = run_command(PreparedCommand(argv), console.root, timeout_s=10,
                                 keep_background_on_success=True, detect_input_requests=False)
        saved = json.loads((console.data / 'console-process.json').read_text())
        console.identities.append(saved['identity'])
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.interruption)
        self.assertEqual(process_identity(saved['identity']['pid']), saved['identity'])
        self.assertEqual(console.config(saved['port'])['cwd'], str(target))

    def test_recovery_controls_restart_when_target_has_legacy_restart_parser(self):
        for phase, terminal in (('activating', 'promoted'), ('rolling_back', 'rolled_back')):
            with self.subTest(phase=phase):
                cli = cli_fixtures.EvolutionCLIRecoveryTests()
                try:
                    cli.setUp()
                    self.check_legacy_restart_target(cli, phase, terminal)
                finally:
                    cli.doCleanups()

    def check_legacy_restart_target(self, cli, phase, terminal):
        fixture = cli.fixture
        source = fixture.source
        root = Path(__file__).resolve().parents[1]
        # This target only accepts the original parser options. Activation
        # must use the current controller's driver and this tree's server.
        (source / 'console' / 'restart.py').write_text(
            "from console.server import build_parser\n"
            "build_parser().parse_args()\n"
            "raise SystemExit('legacy restart driver should not run')\n"
        )
        (source / 'auto_moniter' / '__init__.py').write_text(
            f"__path__.append({str(root / 'auto_moniter')!r})\n")
        (source / 'console' / 'server.py').write_text(
            "from argparse import ArgumentParser\n"
            "from pathlib import Path\n"
            "def build_parser():\n"
            "    parser = ArgumentParser()\n"
            "    parser.add_argument('--cwd', type=Path)\n"
            "    parser.add_argument('--data-dir', type=Path)\n"
            "    parser.add_argument('--port', type=int)\n"
            "    parser.add_argument('--runs-dir', type=Path, action='append', default=[])\n"
            "    return parser\n"
        )
        (source / 'console' / '__main__.py').write_text(
            "import fcntl, os, subprocess\n"
            "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
            "from auto_moniter.processes import process_identity\n"
            "from auto_moniter.storage import write_json\n"
            "from console.server import build_parser\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'[]')\n"
            "args = build_parser().parse_args()\n"
            "with (args.data_dir / 'console.lock').open('a') as lock:\n"
            "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "    with HTTPServer(('127.0.0.1', args.port), Handler) as server:\n"
            "        write_json(args.data_dir / 'console-process.json', {\n"
            "            'identity': process_identity(os.getpid()), 'port': server.server_port,\n"
            "            'module': __file__,\n"
            "            'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()})\n"
            "        server.serve_forever()\n"
        )
        git(source, 'add', 'auto_moniter/__init__.py', 'console')
        git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
            'commit', '--amend', '--no-edit')
        fixture.base = git(source, 'rev-parse', 'HEAD')
        with patch.object(fixture.engine, '_activate', return_value=False):
            path = cli.phases.interrupt(fixture, phase)
        saved = json.loads(path.read_text())
        competing = fixture.root / 'competing' / 'console'
        competing.mkdir(parents=True)
        for name in ('__init__.py', '__main__.py', 'server.py'):
            (competing / name).write_bytes((source / 'console' / name).read_bytes())
        data = source / '.auto-moniter' / 'console'
        data.mkdir(parents=True)
        process_file = data / 'console-process.json'
        old = subprocess.Popen([sys.executable, '-m', 'console', '--cwd', str(source),
                                '--data-dir', str(data), '--port', '0'], cwd=source,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not process_file.exists() and old.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(process_file.exists())
            environment = {'PYTHONSAFEPATH': '1',
                           'PYTHONPATH': os.pathsep.join((str(competing.parent), str(root)))}
            with patch.dict(os.environ, environment):
                code, cycles = cli.invoke()
            cycle = cycles[0]
            self.assertEqual(cycle['phase'], terminal, cycle)
            self.assertEqual(code, 0 if terminal == 'promoted' else 1, cycles)
            self.assertEqual(cycle['usage']['max_runtime_s'], saved['usage']['max_runtime_s'])
            self.assertEqual(cycle['agent_calls'], saved['agent_calls'])
            self.assertEqual(cycle['candidate_checks'], saved['candidate_checks'])
            expected = saved['candidate'] if phase == 'activating' else saved['rollback_commit']
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), expected)
            self.assertEqual(git(source, 'rev-list', '--count', 'HEAD'), '2' if phase == 'activating' else '3')
            self.assertEqual(list(path.parent.parent.glob('*/evolution.json')), [path])
            started = json.loads(process_file.read_text())
            identity = started['identity']
            self.assertNotEqual(identity['pid'], old.pid)
            self.assertEqual(Path(f"/proc/{identity['pid']}/cwd").resolve(strict=True), source)
            self.assertEqual(Path(started['module']).resolve(), source / 'console' / '__main__.py')
            self.assertEqual(started['head'], expected)
        finally:
            if old.poll() is None:
                old.terminate()
            old.wait(timeout=5)
            if process_file.exists():
                identity = json.loads(process_file.read_text())['identity']
                pid = identity['pid']
                if process_identity(pid) == identity:
                    os.kill(pid, signal.SIGTERM)
