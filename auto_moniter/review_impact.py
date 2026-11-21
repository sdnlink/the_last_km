"""Bounded Python impact hints for an immutable candidate diff."""

import ast
import os

from auto_moniter.function_gate import python_functions


def _parse_blob(git, revision, path):
    ref = f'{revision}:./{path}'
    size = git('cat-file', '-s', ref, check=False).strip()
    if not size.isdigit() or int(size) > 512 * 1024:
        return None
    try:
        return ast.parse(git('cat-file', 'blob', ref, check=False))
    except (ValueError, SyntaxError, RecursionError):
        return None


def _definitions(git, revision, path):
    tree = _parse_blob(git, revision, path)
    return dict(python_functions(tree)) if tree is not None else {}


def python_impact(git, base, head):
    raw = git('diff', '--relative', '--name-only', '--no-renames', '-z',
              base, head, '--', '*.py', ':(exclude).auto-moniter')
    files = [os.fsdecode(item) for item in raw.split(b'\0') if item]
    targets = []
    for path in files[:12]:
        before = _definitions(git, base, path)
        after = _definitions(git, head, path)
        for name in sorted(before.keys() | after.keys()):
            if name not in before or name not in after or ast.dump(before[name]) != ast.dump(after[name]):
                node = after.get(name)
                location = f':{node.lineno}' if node else ' (removed)'
                targets.append((name.rsplit('.', 1)[-1], f'{path}{location} {name}'))
        if len(targets) >= 12:
            break
    if not targets:
        return ''

    targets = targets[:12]
    names = {name for name, _ in targets}
    patterns = [part for name in sorted(names) for part in ('-e', name)]
    matched = git('grep', '-F', '-l', '-z', *patterns, head,
                  '--', '*.py', ':(exclude).auto-moniter', check=False)
    paths = [os.fsdecode(item.split(b':', 1)[1]) for item in matched.split(b'\0') if b':' in item]
    paths.sort(key=lambda path: (not any(part == 'tests' or part.startswith('test_')
                                         for part in path.split('/')), path not in files, path))
    callers = []
    for path in paths[:20]:
        tree = _parse_blob(git, head, path)
        if tree is None:
            continue
        functions = list(python_functions(tree))
        for call in sorted((node for node in ast.walk(tree) if isinstance(node, ast.Call)),
                           key=lambda node: node.lineno):
            callee = call.func
            name = callee.id if isinstance(callee, ast.Name) else callee.attr if isinstance(callee, ast.Attribute) else ''
            if name not in names:
                continue
            caller = next((symbol for symbol, function in reversed(functions)
                           if function.lineno <= call.lineno <= function.end_lineno), '<module>')
            callers.append(f'{path}:{call.lineno} {caller} calls {name}')
            if len(callers) >= 20:
                break
        if len(callers) >= 20:
            break
    return '\n'.join([
        'Python impact hints from the candidate diff. Verify name matches and consider other dependencies as needed.',
        'Changed symbols:', *(location for _, location in targets),
        'Possible direct callers and tests:', *(callers or ['No direct call sites found within the scan limits.']),
    ])
