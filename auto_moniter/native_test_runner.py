"""Execute real Swift tests; missing platform or zero/all-skipped tests fail honestly."""
import json
from pathlib import Path
import platform
import re
import subprocess
import tempfile


def run(args, root, path):
    manifest = root / args.manifest
    if not manifest.is_relative_to(root) or manifest.resolve() != manifest or not manifest.exists():
        print('Native test project is missing or outside the workspace.', flush=True)
        return 1
    classes = re.findall(r'\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?XCTestCase\b', path.read_text())
    if not classes:
        print('No XCTestCase classes found in the declared Swift test file.', flush=True)
        return 1
    if args.runner == 'swift':
        package = root / args.manifest
        selection = '|'.join(re.escape(name) for name in dict.fromkeys(classes))
        return stream([args.executable, 'test', '--package-path', str(package.parent),
                       '--skip-update', '--filter', selection])
    if platform.system() != 'Darwin':
        print('Native XCTest requires macOS and Xcode; implementation can continue independently, '
              'but this check is unavailable and cannot establish acceptance.', flush=True)
        return 127
    try:
        inventory = subprocess.run(['xcrun', 'simctl', 'list', 'devices', 'available', '--json'],
                                   capture_output=True, text=True, check=True, timeout=30)
        candidates = []
        for runtime, devices in json.loads(inventory.stdout).get('devices', {}).items():
            version = re.fullmatch(r'com\.apple\.CoreSimulator\.SimRuntime\.iOS-(\d+)-(\d+)(?:-(\d+))?', runtime)
            if not version:
                continue
            for device in devices:
                ident = device.get('udid', '')
                if (device.get('isAvailable') is True and device.get('name', '').startswith('iPhone')
                        and re.fullmatch(r'[0-9A-Fa-f-]{36}', ident)):
                    candidates.append((tuple(int(n or 0) for n in version.groups()), ident))
        if not candidates:
            print('No available iPhone simulator; native behavior remains unverified.', flush=True)
            return 127
        device = sorted(candidates)[-1][1]
        with tempfile.TemporaryDirectory(prefix='auto-moniter-xctest-') as directory:
            bundle = Path(directory) / 'Tests.xcresult'
            argv = [args.executable, '-project', str(root / args.manifest), '-scheme', args.scheme,
                    '-configuration', 'Debug', '-destination', 'platform=iOS Simulator,id=' + device,
                    '-derivedDataPath', str(Path(directory) / 'DerivedData'), '-resultBundlePath', str(bundle),
                    'CODE_SIGNING_ALLOWED=YES', 'CODE_SIGN_IDENTITY=-', 'AD_HOC_CODE_SIGNING_ALLOWED=YES',
                    '-parallel-testing-enabled', 'NO',
                    *['-only-testing:' + args.target + '/' + name for name in dict.fromkeys(classes)], 'test']
            code = stream(argv)
            if code:
                return code
            summary = subprocess.run(['xcrun', 'xcresulttool', 'get', 'test-results', 'summary', '--path', str(bundle)],
                                     capture_output=True, text=True, check=True, timeout=30)
            result = json.loads(summary.stdout)
            counts = [result.get(k) for k in ('totalTestCount', 'passedTests', 'failedTests', 'skippedTests')]
            if (any(type(n) is not int for n in counts) or counts[1] <= 0 or counts[2] or counts[3]
                    or counts[0] != counts[1]):
                print('XCTest requires nonzero passing tests and no failures/skips.', flush=True)
                return 1
            return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Native XCTest unavailable or failed: ' + type(error).__name__, flush=True)
        return 127


def stream(argv):
    passed = 0
    try:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
            for line in child.stdout:
                print(line, end='', flush=True)
                match = re.search(r'Executed (\d+) tests?, with 0 failures|Test run with (\d+) tests? passed', line)
                if match:
                    passed += int(match[1] or match[2])
            code = child.wait()
    except OSError:
        print('Native test executable not available: ' + str(argv[0]), flush=True)
        return 127
    if code:
        return code if code > 0 else 1
    if not passed:
        print('No real native tests passed; compilation or source matching alone is insufficient.', flush=True)
        return 1
    return 0
