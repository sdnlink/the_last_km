"""Bind an approved local test to immutable Git contents."""

import argparse
import hashlib
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile


def tree_entries(project, tree, test):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', tree):
        raise ValueError('候选必须使用完整 Git 树标识。')
    test_path = Path(test)
    if test_path.is_absolute() or '..' in test_path.parts or str(test_path) != test:
        raise ValueError('专项测试须使用候选内的相对路径。')
    git = ['git', '-C', str(project)]
    if subprocess.check_output([*git, 'cat-file', '-t', tree], stderr=subprocess.STDOUT,
                               timeout=15).strip() != b'tree':
        raise ValueError('指定 Git 对象不是候选树。')
    entries = {}
    raw = subprocess.check_output([*git, 'ls-tree', '-r', '-z', tree], timeout=30)
    for entry in raw.split(b'\0')[:-1]:
        metadata, name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError('候选含符号链接或子模块，当前检查无法验证完整源码。')
        entries[name.decode()] = (mode, oid)
    if test not in entries:
        raise ValueError(f'候选 {tree} 缺少专项测试 {test}。')
    return entries


def materialize(project, tree, destination, entries):
    with tempfile.TemporaryFile() as archive:
        subprocess.run(['git', '-C', str(project), 'archive', '--format=tar', tree],
                       stdout=archive, check=True, timeout=60)
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode='r:') as bundle:
            bundle.extractall(destination, filter='data')
    verify(destination, entries)


def verify(root, entries):
    for name, (mode, oid) in entries.items():
        path = root / name
        if path.resolve() != path or not path.is_file():
            raise ValueError(f'隔离副本缺少候选文件 {name}。')
        with path.open('rb') as stream:
            stat = path.stat()
            digest = hashlib.new('sha1' if len(oid) == 40 else 'sha256')
            digest.update(f'blob {stat.st_size}\0'.encode())
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != oid or bool(stat.st_mode & 0o100) != (mode == '100755'):
            raise ValueError(f'隔离副本中的文件 {name} 与候选树不符。')


def bindings(args):
    if not 1 <= len(args.git_tree) <= 8 or len(set(args.git_tree)) != len(args.git_tree):
        raise ValueError('每项检查可绑定 1 到 8 棵不同的候选树。')
    if args.expect_rust_test and args.runner != 'unittest':
        raise ValueError('具名 Rust 用例须由 unittest 入口采集。')
    result = []
    for tree in args.git_tree:
        entries = tree_entries(args.project, tree, args.path)
        if args.manifest and args.manifest not in entries:
            raise ValueError(f'候选 {tree} 缺少登记的清单 {args.manifest}。')
        blob = entries[args.path][1]
        if args.test_blob and blob != args.test_blob:
            raise ValueError(f'候选 {tree} 的专项测试对象与登记值不符。')
        result.append((tree, blob, entries))
    return result


def inspect_checks(project, checks, commands=None):
    from auto_moniter.local_test_runner import parser

    runner = Path(__file__).with_name('local_test_runner.py').resolve()
    reports = []
    for index, command in enumerate(checks, 1):
        if commands is not None and command not in commands:
            continue
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if (len(argv) < 2 or Path(argv[1]) != runner
                or not any(flag == '--git-tree' or flag.startswith('--git-tree=') for flag in argv[2:])):
            continue
        report = {'check_id': f'check:{index}'}
        try:
            args = parser(exit_on_error=False).parse_args(argv[2:])
            if args.project.resolve() != project:
                raise ValueError('候选树检查须从当前任务仓库读取 Git 对象。')
            report.update(status='ready', test=args.path, candidates=[
                {'git_tree': tree, 'test_blob': blob} for tree, blob, _ in bindings(args)])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report.update(status='blocked', reason=str(exc))
        except (SystemExit, argparse.ArgumentError):
            report.update(status='blocked', reason='候选树检查的参数无效。')
        reports.append(report)
    return reports


def run_bound(args, runner):
    try:
        candidates = bindings(args)
        flags = ['--path', args.path, '--runner', args.runner]
        for field in ('executable', 'manifest', 'target', 'scheme'):
            if value := getattr(args, field):
                flags.extend(['--' + field, value])
        for name in args.expect_rust_test:
            flags.extend(['--expect-rust-test', name])
        status = 0
        for tree, blob, entries in candidates:
            with tempfile.TemporaryDirectory(prefix='auto-moniter-check-tree-') as location:
                root = Path(location).resolve()
                materialize(args.project, tree, root, entries)
                print(f'git_tree={tree} test_blob={blob} project_root={root}', flush=True)
                result = subprocess.run([sys.executable, str(runner), '--project', str(root), *flags],
                                        cwd=root)
                verify(root, entries)
                print(f'git_tree={tree} exit_code={result.returncode} source_verified=true', flush=True)
                status = status or int(result.returncode != 0)
        return status
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f'无法执行候选树检查。{exc}', file=sys.stderr, flush=True)
        return 1
