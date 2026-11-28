"""Watcher-owned test commands derived from reviewer-approved file paths."""
from pathlib import Path
import hashlib
import os
import shlex
import shutil
import sys
import tomllib

from auto_moniter.quality import test_asset


def command(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str):
        raise ValueError('Local test path must be text')
    relative = Path(name)
    path = root / relative
    if (relative.is_absolute() or '..' in relative.parts
            or str(relative) != name or any(p.startswith('.') for p in relative.parts)
            or path.resolve() != path or not test_asset(name)):
        raise ValueError('Local test must be an exact non-symlink test path: ' + str(name))
    helper = str(Path(__file__).with_name('local_test_runner.py').resolve())
    prefix = [sys.executable, helper, '--project', str(root), '--path', name]
    if path.suffix == '.py' and (path.name.startswith('test_') or path.stem.endswith('_test')):
        return shlex.join([*prefix, '--runner', 'unittest'])
    if path.suffix in {'.js', '.cjs', '.mjs'}:
        return shlex.join([*prefix, '--runner', 'node', '--executable', shutil.which('node') or 'node'])
    if path.suffix == '.rs' and 'tests' in relative.parts:
        # Cargo integration targets are direct tests/name.rs or tests/name/main.rs.
        index = len(relative.parts) - 1 - relative.parts[::-1].index('tests')
        tail = relative.parts[index + 1:]
        if len(tail) == 1:
            target = Path(tail[0]).stem
        elif len(tail) == 2 and tail[1] == 'main.rs':
            target = tail[0]
        else:
            return None
        manifest = root.joinpath(*relative.parts[:index], 'Cargo.toml')
        if (not manifest.is_file() or manifest.resolve() != manifest
                or manifest.stat().st_size > 512 * 1024):
            raise ValueError('Local Rust test needs its existing package Cargo.toml: ' + name)
        data = tomllib.loads(manifest.read_text(encoding='utf-8'))
        package = data.get('package', {})
        if not isinstance(package, dict) or not isinstance(package.get('name'), str):
            raise ValueError('Local Rust test must belong to a Cargo package: ' + name)
        return shlex.join([*prefix, '--runner', 'cargo', '--executable', shutil.which('cargo') or 'cargo',
                           '--manifest', str(manifest.relative_to(root)), '--target', target])
    if path.suffix == '.swift':
        from auto_moniter.native_tests import command as native_command
        return native_command(root, name, prefix)
    return None


def catalog(root, task):
    return {f'auto:{index}': value for index, name in enumerate(task.get('test_files', []), 1)
            if (value := command(root, name)) is not None}


def reuse_key(root, check):
    """Identify a monitor-generated Node test and the local runtime it will use."""
    if '--runner node ' not in check or os.environ.get('NODE_PATH') or os.environ.get('NODE_OPTIONS'):
        return None

    try:
        args = shlex.split(check)
        if len(args) != 10 or args[2:9:2] != ['--project', '--path', '--runner', '--executable']:
            return None
        if command(root, args[5]) != check:
            return None
        test_dir = (root / args[5]).parent
        if any((path / 'node_modules').exists() for path in (test_dir, *test_dir.parents)):
            return None
        files = [Path(name).resolve(strict=True) for name in (args[0], args[1], args[9], '/bin/sh')]
        runtime = []
        for path in files:
            stat = path.stat()
            runtime.append((str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns))
        environment = hashlib.sha256(repr(sorted(os.environ.items())).encode()).digest()
        return runtime, environment
    except (OSError, RuntimeError, ValueError):
        return None


def bound_reuse_key(root, check, inputs):
    """Fingerprint an operator-approved, self-contained Git-tree check."""
    from auto_moniter.check_git_tree import registered_runner
    from auto_moniter.workspace import _file_token

    args = registered_runner(root, check)
    if not args or not args.git_tree or args.runner not in {'unittest', 'cargo', 'node'}:
        return None
    tools = [sys.executable, str(Path(__file__).with_name('local_test_runner.py')),
             str(Path(__file__).with_name('check_git_tree.py')), '/bin/sh']
    tools.extend(name for name in (args.executable, shutil.which('git'), shutil.which('cargo'), shutil.which('rustc')) if name)
    try:
        runtime = []
        for name in tools:
            resolved = Path(shutil.which(name) or name).resolve(strict=True)
            stat = resolved.stat()
            runtime.append((str(resolved), stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns))
        files = []
        for name in inputs:
            path = root / name
            if path.resolve() != path or not path.is_file():
                return None
            files.append((name, _file_token(path)))
        key = (check, runtime, files, sorted(os.environ.items()))
        return hashlib.sha256(repr(key).encode()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None
