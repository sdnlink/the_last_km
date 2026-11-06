import shutil
import subprocess
import unittest
from pathlib import Path


class ConsoleRequestCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for browser request regression tests')
    def test_browser_request_compatibility(self):
        script = Path(__file__).with_suffix('.mjs')
        result = subprocess.run(['node', '--test', str(script)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

