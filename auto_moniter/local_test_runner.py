"""Run a single declared local regression; an empty/entirely skipped run fails."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


def parser(*, exit_on_error=True):
    parser = argparse.ArgumentParser(description=__doc__, exit_on_error=exit_on_error, allow_abbrev=False)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--path', required=True)
    parser.add_argument('--runner', choices=['unittest', 'node', 'cargo', 'swift', 'xcode'], required=True)
    parser.add_argument('--executable')
    parser.add_argument('--manifest')
    parser.add_argument('--target')
    parser.add_argument('--scheme')
    parser.add_argument('--git-tree', action='append', default=[], help='Run against each exact Git tree')
    parser.add_argument('--test-blob', help='Require this test Git blob in every tree')
    parser.add_argument('--expect-rust-test', action='append', default=[],
                        help='Keep and verify the named Cargo test passed during this unittest run')
    parser.add_argument('--record-cargo-root', action='store_true', help=argparse.SUPPRESS)
    return parser


def main():
    args = parser().parse_args()
    if args.git_tree:
        if __package__:
            from .check_git_tree import run_bound
        else:
            from check_git_tree import run_bound
        return run_bound(args, Path(__file__).resolve())
    if args.test_blob:
        parser().error('--test-blob requires --git-tree')
    if args.expect_rust_test or args.record_cargo_root:
        if args.runner != 'unittest':
            parser().error('--expect-rust-test requires --runner unittest')
        return run_with_cargo_evidence(args)
    return run_local(args)


def run_with_cargo_evidence(args):
    from unittest.mock import patch

    run = subprocess.run
    passed = set()

    def report_cargo(*positional, **options):
        result = run(*positional, **options)
        command = positional[0] if positional else options.get('args')
        if (isinstance(command, (list, tuple)) and len(command) >= 2
                and isinstance(command[0], (str, os.PathLike))
                and Path(command[0]).name == 'cargo'):
            if args.record_cargo_root:
                root = Path(options.get('cwd') or Path.cwd()).resolve()
                manifest = next((str(root / value.split('=', 1)[1]) for value in command[2:]
                                 if isinstance(value, str) and value.startswith('--manifest-path=')), '')
                if '--manifest-path' in command and command.index('--manifest-path') + 1 < len(command):
                    manifest = str(root / command[command.index('--manifest-path') + 1])
                print('cargo_execution=' + json.dumps({'cwd': str(root), 'manifest': manifest,
                      'subcommand': command[1], 'exit_code': result.returncode}), flush=True)
            if command[1] != 'test' or not args.expect_rust_test:
                return result
            for value in (result.stdout, result.stderr):
                if isinstance(value, bytes):
                    value = value.decode(errors='replace')
                if isinstance(value, str):
                    print(value, end='', flush=True)
                    if result.returncode == 0:
                        passed.update(re.findall(r'^test (\S+) \.\.\. ok$', value, re.M))
        return result

    with patch.object(subprocess, 'run', report_cargo):
        status = run_local(args)
    missing = set(args.expect_rust_test) - passed
    if missing:
        print('Missing passing Cargo test results: ' + ', '.join(sorted(missing)), flush=True)
    return status or int(bool(missing))


def run_local(args):
    root = args.project.resolve()
    path = root / args.path
    if (not path.is_file() or path.resolve() != path or not path.is_relative_to(root)):
        print('Declared local regression is missing or is not a regular project file: ' + args.path, flush=True)
        return 1
    os.chdir(root)
    if args.runner in {'swift', 'xcode'}:
        from native_test_runner import run
        return run(args, root, path)
    if args.runner == 'unittest':
        return run_unittest(root, path)
    argv = ([args.executable, '--test', '--experimental-test-isolation=none', args.path] if args.runner == 'node' else
            [args.executable, 'test', '--locked', '--offline', '--manifest-path', args.manifest,
             '--test', args.target])
    return run_native(argv)


def run_unittest(root, path):
    sys.path.insert(0, str(root))
    # Never load timestamp-cached code from a previous fast repair turn.
    # Keep imports fresh without deleting or rewriting project caches.
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='auto-moniter-test-imports-') as cache:
        sys.pycache_prefix = cache
        suite = unittest.defaultTestLoader.discover(str(path.parent), pattern=path.name)
        count = suite.countTestCases()
        if not count:
            print('No unittest test cases discovered in ' + str(path.relative_to(root)), flush=True)
            return 1
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == len(result.skipped):
        print('All declared test cases were skipped; behavior remains unverified.', flush=True)
        return 1
    return 0 if result.wasSuccessful() else 1


def run_native(argv):
    passed = 0
    try:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
            for line in child.stdout:
                print(line, end='', flush=True)
                match = re.search(r'^# pass (\d+)|^test result: ok\. (\d+) passed;', line)
                if match:
                    passed += int(match[1] or match[2])
            code = child.wait()
    except FileNotFoundError:
        print('Local test executable not found: ' + str(args.executable), flush=True)
        return 127
    if code:
        return code if code > 0 else 1
    if not passed:
        print('No passing test cases executed; empty or skipped tests cannot establish delivery.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
