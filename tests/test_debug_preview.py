from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from console import debug_preview


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for local preview tests')
class DebugPreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.runtime = root / 'runtime'
        self.workspace = root / 'source'
        self.workspace.mkdir()
        self.addCleanup(debug_preview.remove, self.runtime)
        self.workspace.joinpath('package.json').write_text('{}', encoding='utf-8')
        self.workspace.joinpath('vite.config.js').write_text('// Vite project', encoding='utf-8')
        vite = self.workspace / 'node_modules' / 'vite'
        vite.mkdir(parents=True)
        vite.joinpath('package.json').write_text('{"type":"module","exports":"./vite.js"}', encoding='utf-8')
        vite.joinpath('vite.js').write_text("""
import {createServer as http} from 'node:http';
import {readFileSync, writeFileSync} from 'node:fs';
export function loadEnv() {
  const backend = process.env.VITE_BASEURL ||
    readFileSync('.env.development', 'utf8').trim();
  return {VITE_APP_BASE_API: '/dev-api', VITE_BASEURL: backend};
}
export async function createServer({server}) {
  writeFileSync('generated.ts', 'generated in preview');
  const httpServer = http((request, response) => {
    if (request.url !== '/dev-api/') response.statusCode = 404;
    response.end();
  });
  return {
    httpServer,
    config: {server: {proxy: {'/dev-api': {}}}},
    listen: () => new Promise(resolve => httpServer.listen(0, server.host, resolve)),
    close: () => new Promise(resolve => httpServer.close(resolve)),
  };
}
""", encoding='utf-8')

    def wait_for_status(self, status):
        limit = time.monotonic() + 8
        while time.monotonic() < limit:
            result = debug_preview.status(self.runtime)
            if result['status'] != 'starting':
                self.assertEqual(result['status'], status, result)
                return result
            time.sleep(.05)
        self.fail('preview did not report its status')

    @patch.dict('os.environ', {'VITE_BASEURL': ''})
    def test_existing_repository_configuration_starts_without_input(self):
        (self.workspace / '.env.development').write_text('https://backend.example', encoding='utf-8')
        debug_preview.start(self.runtime, self.workspace, {})
        result = self.wait_for_status('ready')
        self.assertRegex(result['url'], r'^http://127\.0\.0\.1:\d+$')
        self.assertEqual(debug_preview.start(self.runtime, self.workspace, {}), result)
        self.assertFalse((self.workspace / 'generated.ts').exists())

    @patch.dict('os.environ', {'VITE_BASEURL': ''})
    def test_missing_backend_requests_only_its_address_then_reuses_it(self):
        (self.workspace / '.env.development').touch()
        debug_preview.start(self.runtime, self.workspace, {})
        self.assertEqual(self.wait_for_status('needs_input')['field'], 'backend')
        with self.assertRaisesRegex(ValueError, '账号'):
            debug_preview.start(self.runtime, self.workspace, {'backend': 'https://u:p@example.test'})
        debug_preview.start(self.runtime, self.workspace, {'backend': 'https://backend.example'})
        self.wait_for_status('ready')
        self.assertEqual(debug_preview.stop(self.runtime), {'status': 'idle'})
        debug_preview.start(self.runtime, self.workspace, {})
        self.wait_for_status('ready')
