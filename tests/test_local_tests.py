import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter.check_git_tree import inspect_checks
from auto_moniter.local_tests import bound_reuse_key, catalog, command, reuse_key


class LocalTestTests(unittest.TestCase):
    def test_bound_reuse_key_changes_with_declared_input_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            test = root / 'test_behavior.py'
            test.write_text('import unittest\n')
            path = root / 'input.json'
            path.write_text('{"value": 1}')
            check = command(root, test.name) + ' --git-tree ' + 'a' * 40
            key = bound_reuse_key(root, check, [path.name])
            self.assertIsNotNone(key)
            with patch.dict('os.environ', {'TEST_REUSE_ENVIRONMENT': 'changed'}):
                self.assertNotEqual(key, bound_reuse_key(root, check, [path.name]))
            path.write_text('{"value": 2}')
            self.assertNotEqual(key, bound_reuse_key(root, check, [path.name]))
            self.assertIsNone(bound_reuse_key(root, command(root, test.name), [path.name]))

    def test_bound_python_check_records_nested_cargo_execution_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            git = ['git', '-C', str(root)]
            subprocess.run([*git, 'init', '-q'], check=True)
            (root / 'Cargo.toml').write_text('[package]\nname="sample"\nversion="0.1.0"\n')
            cargo = root / 'cargo'
            cargo.write_text('#!/bin/sh\nexit 0\n')
            cargo.chmod(0o755)
            (root / 'test_behavior.py').write_text(
                'import subprocess, unittest\nfrom pathlib import Path\n'
                'class Check(unittest.TestCase):\n'
                ' def test_cargo_root(self):\n'
                '  root = Path(__file__).parent\n'
                '  result = subprocess.run([str(root / "cargo"), "check", "--manifest-path", "Cargo.toml"], cwd=root)\n'
                '  self.assertEqual(result.returncode, 0)\n')
            subprocess.run([*git, 'add', '.'], check=True)
            tree = subprocess.check_output([*git, 'write-tree'], text=True).strip()
            result = subprocess.run([*shlex.split(command(root, 'test_behavior.py')), '--git-tree', tree],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            project_root = next(line.split('project_root=', 1)[1] for line in result.stdout.splitlines()
                                if line.startswith('git_tree=') and 'project_root=' in line)
            observed = next(json.loads(line.split('=', 1)[1]) for line in result.stdout.splitlines()
                            if line.startswith('cargo_execution='))
            self.assertEqual(observed, {'cwd': project_root, 'manifest': project_root + '/Cargo.toml',
                                        'subcommand': 'check', 'exit_code': 0})

    def test_git_tree_check_runs_both_exact_candidates_and_rejects_wrong_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

            git('init', '-q')
            source = root / 'app.py'
            test = root / 'test_behavior.py'
            test.write_text(
                'import unittest\nfrom app import value\n'
                'class Test(unittest.TestCase):\n'
                ' def test_result(self): self.assertIn(value, (41, 42))\n')
            trees = []
            for version in (41, 42):
                source.write_text(f'value = {version}\n')
                git('add', 'app.py', test.name)
                trees.append(git('write-tree'))
            source.write_text('raise AssertionError("The dirty workspace was used")\n')
            blob = git('hash-object', test.name)
            argv = shlex.split(command(root, test.name))
            argv.extend(['--git-tree', trees[0], '--git-tree', trees[1], '--test-blob', blob])
            report, = inspect_checks(root, [shlex.join(argv)])
            self.assertEqual(report['status'], 'ready')
            self.assertEqual([item['git_tree'] for item in report['candidates']], trees)
            result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('source_verified=true'), 2)
            self.assertIn('dirty workspace', source.read_text())
            argv[-1] = '0' * len(blob)
            report, = inspect_checks(root, [shlex.join(argv)])
            self.assertEqual(report['status'], 'blocked')
            result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('test_result', result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_node_reuse_requires_the_same_known_local_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            check = command(root, 'test_behavior.cjs')
            key = reuse_key(root, check)
            self.assertIsNotNone(key)
            self.assertIsNone(reuse_key(root, check + ' && true'))
            with patch.dict('os.environ', {'TEST_REUSE_ENVIRONMENT': 'changed'}):
                self.assertNotEqual(reuse_key(root, check), key)
            (root / 'node_modules').mkdir()
            self.assertIsNone(reuse_key(root, check))

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

