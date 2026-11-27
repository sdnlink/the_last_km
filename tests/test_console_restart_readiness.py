"""Exercise supervised restart with a slow task catalog."""

import json
import os
import sys
import unittest
from unittest.mock import patch

from tests import test_console_restart as fixtures
from auto_moniter.processes import process_identity
from auto_moniter.supervisor import PreparedCommand, run_command


class ConsoleRestartReadinessTests(unittest.TestCase):
    def test_supervised_restart_is_ready_while_task_listing_is_slow(self):
        console = fixtures.ConsoleRestartTests()
        self.addCleanup(console.doCleanups)
        console.setUp()
        package = console.project / 'console'
        package.mkdir()
        (package / '__init__.py').write_text(
            f"__path__.append({str(fixtures.ROOT / 'console')!r})\n"
        )
        catalog_requested = console.root / 'catalog-requested'
        (package / '__main__.py').write_text(
            "from pathlib import Path\n"
            "import time\n"
            "from console.server import TaskStore, main\n"
            "original = TaskStore.task_summaries\n"
            "def slow_task_summaries(self):\n"
            f"    Path({str(catalog_requested)!r}).touch()\n"
            "    time.sleep(2)\n"
            "    return original(self)\n"
            "TaskStore.task_summaries = slow_task_summaries\n"
            "raise SystemExit(main())\n"
        )
        command = PreparedCommand([
            sys.executable, '-m', 'console.restart', '--cwd', str(console.project),
            '--data-dir', str(console.data), '--port', '0', '--supervised-activation',
        ])
        with patch.dict(os.environ, console.environment):
            result = run_command(command, console.root, timeout_s=5,
                                 keep_background_on_success=True, detect_input_requests=False)
        metadata = console.data / 'console-process.json'
        saved = json.loads(metadata.read_text()) if metadata.exists() else {}
        if saved:
            console.identities.append(saved['identity'])
        self.assertEqual(result.exit_code, 0, (result.interruption, result.stderr))
        self.assertFalse(result.timed_out)
        self.assertFalse(result.interruption)
        self.assertFalse(catalog_requested.exists())
        self.assertEqual(process_identity(saved['identity']['pid']), saved['identity'])
        self.assertEqual(console.config(saved['port'])['cwd'], str(console.project))
