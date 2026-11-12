"""Run a single declared local regression; an empty/entirely skipped run fails."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--path', required=True)
    parser.add_argument('--runner', choices=['unittest', 'node', 'cargo'], required=True)
    parser.add_argument('--executable')
    parser.add_argument('--manifest')
    parser.add_argument('--target')
    args = parser.parse_args()
    root = args.project.resolve()
    path = root / args.path
    if (not path.is_file() or path.resolve() != path or not path.is_relative_to(root)):
        print('Declared local regression is missing or is not a regular project file: ' + args.path, flush=True)
        return 1
    os.chdir(root)
    if args.runner == 'unittest':
        sys.path.insert(0, str(root))
        # Never load timestamp-cached code from a previous fast repair turn.
        # Keep imports fresh without deleting or rewriting project caches.
        sys.dont_write_bytecode = True
        with tempfile.TemporaryDirectory(prefix='auto-moniter-test-imports-') as cache:
            sys.pycache_prefix = cache
            suite = unittest.defaultTestLoader.discover(str(path.parent), pattern=path.name)
            count = suite.countTestCases()
            if not count:
                print('No unittest test cases discovered in ' + args.path, flush=True)
                return 1
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        if result.testsRun == len(result.skipped):
            print('All declared test cases were skipped; behavior remains unverified.', flush=True)
            return 1
        return 0 if result.wasSuccessful() else 1
    argv = ([args.executable, '--test', '--experimental-test-isolation=none', args.path] if args.runner == 'node' else
            [args.executable, 'test', '--locked', '--offline', '--manifest-path', args.manifest,
             '--test', args.target])
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
