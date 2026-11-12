import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from auto_moniter.local_tests import catalog, command


class LocalTestTests(unittest.TestCase):
    def test_unittest_runner_requires_real_non_skipped_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'test_behavior.py'
            for source, code in [
                ('value=42\n',1),
                ('import unittest\nclass Test(unittest.TestCase):\n @unittest.skip("no environment")\n def test_missing(self): pass\n',1),
                ('import unittest\nclass Test(unittest.TestCase):\n def test_result(self): self.assertEqual(6*7,42)\n',0),
                ('import unittest\nclass Test(unittest.TestCase):\n def test_result(self): self.assertEqual(6*7,41)\n',1)]:
                path.write_text(source)
                result=subprocess.run(command(root,path.name),shell=True,capture_output=True,text=True)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)

    def test_paths_cannot_inject_shell_or_escape_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for value in ['../test_escape.py','/tmp/test_escape.py','.git/test_hidden.py','tests/../test_escape.py']:
                with self.subTest(value=value), self.assertRaises(ValueError):command(root,value)
            odd='test_$(touch injected).py'
            (root/odd).write_text('import unittest\nclass Test(unittest.TestCase):\n def test_real(self): self.assertTrue(True)\n')
            result=subprocess.run(command(root,odd),shell=True,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/'injected').exists())
            (root/'tests').symlink_to('/tmp',target_is_directory=True)
            with self.assertRaises(ValueError):command(root,'tests/test_external.py')

    def test_cargo_command_is_derived_from_existing_package_and_is_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=root/'crates/catalog';package.mkdir(parents=True)
            (package/'Cargo.toml').write_text('[package]\nname="catalog"\nversion="0.1.0"\n')
            commands=catalog(root,{'test_files':['crates/catalog/tests/receiver.rs']})
            self.assertIn('--runner cargo',commands['auto:1'])
            self.assertIn('--target receiver',commands['auto:1'])
            self.assertIsNone(command(root,'crates/catalog/tests/support/helper.rs'))
            self.assertIsNone(command(root,'StockTianjiApp/Tests/QuoteTests.swift'))

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_node_uses_real_assertions_and_rejects_entirely_skipped_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'test_behavior.cjs'
            for body,code in [("test('real',()=>assert.equal(6*7,42));",0),
                              ("test('real',()=>assert.equal(6*7,41));",1),
                              ("test.skip('missing',()=>{});",1)]:
                path.write_text("const test=require('node:test'),assert=require('node:assert/strict');\n"+body)
                result=subprocess.run(command(root,path.name),shell=True,capture_output=True,text=True)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)

