"""Measure the assigned function and bind it to watcher-owned checks."""

import ast
import gzip
import hashlib
import json
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from auto_moniter.function_tasks import GATE_FIELDS, targets
from auto_moniter.function_languages import GRAMMARS, parser as language_parser, spans as language_spans
from auto_moniter.quality import test_asset


SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.html', '.htm', *GRAMMARS}
MAX_SOURCE = 2 * 1024 * 1024
JS_LOCATE = r"""
const fs = require('node:fs');
const acorn = require('internal/deps/acorn/acorn/dist/acorn');
const {source, symbol} = JSON.parse(fs.readFileSync(0, 'utf8'));
const options = {ecmaVersion: 'latest', allowHashBang: true};
let tree;
try { tree = acorn.parse(source, {...options, sourceType: 'script'}); }
catch { tree = acorn.parse(source, {...options, sourceType: 'module'}); }
const matches = [];
const types = new Set(['FunctionExpression', 'ArrowFunctionExpression']);
const property = n => n?.type === 'Identifier' ? n.name : n?.type === 'Literal' ? String(n.value) : '';
function visit(node, prefix = '') {
  if (!node?.type) return;
  let name, start = node, fn;
  if (node.type === 'FunctionDeclaration') { name = node.id?.name; fn = node; }
  if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier') {
    name = node.id.name;
    if (types.has(node.init?.type)) fn = node.init;
  }
  if (node.type === 'ClassDeclaration' || node.type === 'ClassExpression') name = node.id?.name;
  if (['MethodDefinition', 'Property', 'PropertyDefinition'].includes(node.type) && !node.computed) {
    name = property(node.key);
    if (types.has(node.value?.type)) fn = node.value;
  }
  if (node.type === 'VariableDeclaration' && node.declarations.length === 1) {
    const declaration = node.declarations[0];
    if (declaration.id.type === 'Identifier' && types.has(declaration.init?.type)) {
      name = declaration.id.name; fn = declaration.init;
      node = declaration;
    }
  }
  const qualified = name ? prefix + name : '';
  if (fn && qualified === symbol) matches.push([start.start, start.end]);
  // A named declaration supplies the enclosing namespace. Skip the function
  // expression as a separate declaration; still visit declarations in its body.
  const nested = qualified ? qualified + '.' : prefix;
  for (const [key, value] of Object.entries(node)) {
    if (key === 'id' || key === 'key') continue;
    if (Array.isArray(value)) value.forEach(child => visit(child, nested));
    else if (value?.type) visit(value, nested);
  }
}
visit(tree);
console.log(JSON.stringify(matches.map(([a, b]) =>
  [Array.from(source.slice(0, a)).length, Array.from(source.slice(0, b)).length])));
"""


class InlineScripts(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.offsets = [0, *(m.end() for m in re.finditer('\n', source))]
        self.scripts = []
        self.start = None
        self.feed(source)
        self.close()

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (tag == 'script' and 'src' not in attrs and
                (attrs.get('type') or '').strip().lower() in ('', 'module', 'text/javascript', 'application/javascript')):
            self.start = self.source_offset() + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == 'script' and self.start is not None:
            self.scripts.append((self.start, self.source_offset()))
            self.start = None


def _python_spans(source, symbol):
    offsets = [0, *(m.end() for m in re.finditer('\n', source)), len(source)]
    spans = []
    def visit(node, prefix=''):
        named = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        name = prefix + node.name if named else ''
        if name == symbol and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            last = offsets[node.end_lineno - 1]
            # AST columns are UTF-8 byte offsets. Preserve any suffix outside
            # the statement on its final line.
            end = last + len(source[last:].encode()[:node.end_col_offset].decode())
            spans.append((offsets[first - 1], end))
        for child in ast.iter_child_nodes(node):
            visit(child, name + '.' if named else prefix)
    visit(ast.parse(source))
    return spans


def _js_spans(source, symbol, html):
    sections = InlineScripts(source).scripts if html else [(0, len(source))]
    spans = []
    for start, end in sections:
        try:
            result = subprocess.run(['node', '--expose-internals', '-e', JS_LOCATE],
                input=json.dumps({'source': source[start:end], 'symbol': symbol}), cwd='/',
                text=True, capture_output=True, check=True, timeout=10,
                env={k: v for k, v in os.environ.items() if k not in {'NODE_OPTIONS', 'NODE_PATH'}})
            spans.extend((start + a, start + b) for a, b in json.loads(result.stdout))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise ValueError('Cannot parse JavaScript function; use Node 22 and valid JavaScript') from exc
    return spans


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _rust_file_symbol(path, symbol):
    """Resolve Cargo's implicit crate/file modules without dropping arbitrary prefixes."""
    import tomllib
    path = path.resolve()
    for source_root in path.parents:
        manifest = source_root.parent/'Cargo.toml'
        if source_root.name != 'src' or not manifest.is_file():
            continue
        try:
            if manifest.stat().st_size > 64*1024:
                return symbol
            data = tomllib.loads(manifest.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            return symbol
        package = data.get('package', {}).get('name', '')
        if not isinstance(package, str):
            return symbol
        library = data.get('lib', {}).get('name', package.replace('-', '_'))
        if not isinstance(library, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', library):
            return symbol
        relative = path.relative_to(source_root)
        modules = list(relative.parts[:-1])
        if relative.stem not in ('lib', 'main', 'mod'):
            modules.append(relative.stem)
        parts = re.split(r'::|\.', symbol)
        for crate in (library, 'crate'):
            prefix = [crate, *modules]
            if parts[:len(prefix)] == prefix and len(parts) > len(prefix):
                return '::'.join(parts[len(prefix):])
        return symbol
    return symbol


def source_state(path, symbol, source):
    """Measure current or immutable historical source with the same parser."""
    source = source.replace('\r\n', '\n')
    suffix = path.suffix.lower()
    if suffix == '.py':
        spans = _python_spans(source, symbol)
    elif suffix in {'.js', '.mjs', '.cjs', '.html', '.htm'}:
        spans = _js_spans(source, symbol, suffix in {'.html', '.htm'})
    elif suffix in GRAMMARS:
        resolved = _rust_file_symbol(path, symbol) if suffix == '.rs' else symbol
        try:
            spans = language_spans(source, resolved, GRAMMARS[suffix])
        except ValueError as exc:
            raise ValueError(f'{exc} ({path})') from exc
    else:
        raise ValueError('Function scope verification does not support: ' + suffix)
    if len(spans) > 1:
        raise ValueError('Assigned symbol is ambiguous: ' + symbol)
    if not spans:
        return {'whole': _digest(source), 'outside': None, 'body': None}
    start, end = spans[0]
    before, after = source[:start], source[end:]
    without = {before + after}
    if (not before or before.endswith('\n')) and (not after or after.startswith('\n')):
        without.add(before + after.removeprefix('\n'))
        without.add(before.removesuffix('\n') + after.removeprefix('\n'))
    return {'without': sorted(_digest(text) for text in without),
            'outside': [_digest(before), _digest(after)], 'body': _digest(source[start:end])}


def target_state(path, symbol):
    if not path.exists():
        return {'whole': _digest(''), 'outside': None, 'body': None}
    try:
        with path.open('rb') as handle:
            data = handle.read(MAX_SOURCE + 1)
        if len(data) > MAX_SOURCE:
            raise ValueError('Function target exceeds 2 MiB')
        return source_state(path, symbol, data.decode('utf-8'))
    except (OSError, SyntaxError, UnicodeError, RecursionError) as exc:
        raise ValueError('Cannot parse function target: ' + path.name) from exc


def criteria(modules, module_id):
    return {f'{module_id}:{i}': text
            for i, text in enumerate(modules[module_id]['acceptance'], 1)}


def requirement_criteria(brief):
    return {f"req:{brief.get('cycle', 1)}:{i}": text
            for i, text in enumerate(brief.get('acceptance_criteria', []), 1)}


def catalog(checks, modules, module_id=None, *, root=None, task=None, regressions=()):
    result = {f'check:{i}': command for i, command in enumerate(checks, 1)}
    for key, item in modules.items():
        if module_id is None or key == module_id:
            result.update({f'{key}:check:{i}': command
                           for i, command in enumerate(item.get('checks', []), 1)})
    if root is not None:
        from auto_moniter.local_tests import catalog as local_catalog
        if task:
            result.update(local_catalog(root, task))
        result.update({key.replace('auto:', 'regression:', 1): value for key, value in
                       local_catalog(root, {'test_files':list(regressions)}).items()})
    return result


def _path(root, name):
    relative = Path(name)
    path = root / relative
    if (relative.is_absolute() or str(relative) != name or '..' in relative.parts
            or any(p in {'.git', '.auto-moniter'} for p in relative.parts)
            or path.resolve() != path or '\0' in name):
        raise ValueError('Function files must be exact, non-symlink paths inside the project: ' + name)
    return path


def _target_path(root, file):
    path = _path(root, file)
    if path.suffix.lower() not in SUFFIXES:
        raise ValueError('Function scope verification does not support: ' + file)
    if path.suffix.lower() in GRAMMARS:
        language_parser(GRAMMARS[path.suffix.lower()])
    if path.exists() and test_asset(file):
        raise ValueError('Preserve existing regression assets: ' + file)
    return path


def validate_approvals(scopes, root, modules, checks):
    for scope in scopes:
        if scope['module_id'] not in modules:
            raise ValueError('Integration scope must reference a user-defined module: ' + scope['id'])
        if not set(scope['check_ids']) <= catalog(checks, modules, scope['module_id']).keys():
            raise ValueError('Integration scope must reference configured checks: ' + scope['id'])
        for target in scope['targets']:
            _target_path(root, target['file'])


def verify_integration_approval(task, approvals):
    approval = next((scope for scope in approvals if scope['id'] == task['integration_scope']), None)
    if (not approval or approval['module_id'] != task['module_id']
            or sorted(targets(task)) != sorted((t['file'], t['symbol']) for t in approval['targets'])
            or set(task['check_ids']) != set(approval['check_ids'])):
        raise ValueError('Integration requires a matching preapproved scope with the exact targets and checks: '
                         + task['integration_scope'])


def validate_task(task, root, modules, checks, brief, *, approvals=(), regressions=()):
    if not GATE_FIELDS <= task.keys():
        raise ValueError('Function assignment requires criterion_ids, check_ids and test_files')
    paths = {_target_path(root, file) for file, _ in targets(task)}
    if task.get('kind') == 'verification' and any(not path.is_file() for path in paths):
        raise ValueError('Verification-only targets must already exist')
    if task.get('kind') == 'integration':
        verify_integration_approval(task, approvals)
    module_criteria = criteria(modules, task['module_id'])
    ids = set(task['criterion_ids'])
    if not ids & module_criteria.keys() or not ids <= module_criteria.keys() | requirement_criteria(brief).keys():
        raise ValueError('Function assignment must reference its module criteria and only known requirement criteria')
    approved = catalog(checks, modules, task['module_id'], root=root, task=task, regressions=regressions)
    if not set(task['check_ids']) <= approved.keys():
        raise ValueError('Function assignment must select approved check IDs or auto:N for declared local test files, '
                         'or regression:N for accepted tests; no agent commands')
    if (all(re.fullmatch(r'\s*git\s+diff\s+--check\s*', approved[ident]) for ident in task['check_ids'])
            and not any(key.startswith('auto:') for key in approved)):
        raise ValueError('Function acceptance requires a behavioral check; git diff --check alone cannot verify functionality. '
                         'Ask the operator to append the missing approved check before dispatch.')
    for name in task['test_files']:
        test = _path(root, name).relative_to(root)
        if (root / test in paths or test.suffix.lower() not in SUFFIXES - {'.html', '.htm'}
                or any(part.startswith('.') for part in test.parts)
                or test.name in {'__init__.py', 'conftest.py'} or not test_asset(name)):
            raise ValueError('Additional function files must be focused regression test paths: ' + name)


def baseline(path, root, task, snapshot, *, missing_ok, historical_source=None):
    if path.is_file():
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            saved = json.load(handle)
        if saved['id'] == task['id']:
            if any((root / name).exists() and str(root / name) not in snapshot for name in task['test_files']):
                raise ValueError('Declared regression test is outside the monitored workspace')
            repaired = False
            for file, symbol in targets(task):
                previous = saved.get('targets', {}).get(file)
                if (not historical_source or not previous or previous.get('body') is not None
                        or not previous.get('whole')):
                    continue
                source = historical_source(file, previous['whole'])
                if source is None or _digest(source.replace('\r\n', '\n')) != previous['whole']:
                    continue
                measured = source_state(_path(root, file), symbol, source)
                if measured['body'] is not None:
                    saved['targets'][file] = measured
                    repaired = True
            if repaired:
                _write_baseline(path, saved)
            return saved
    if not missing_ok:
        raise ValueError('Saved function scope baseline is missing; refusing to reset it after implementation')
    for name in task['test_files']:
        if _path(root, name).exists():
            raise ValueError('Function tasks may add focused tests; preserve existing test assets: ' + name)
    saved = {'id': task['id'], 'files': snapshot,
             'targets': {file: target_state(_path(root, file), symbol) for file, symbol in targets(task)}}
    if task.get('kind') == 'verification' and any(t['body'] is None for t in saved['targets'].values()):
        raise ValueError('Verification-only assignment must name an existing, unambiguous function')
    _write_baseline(path, saved)
    return saved


def _write_baseline(path, saved):
    tmp = path.with_suffix('.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8') as handle:
        json.dump(saved, handle, separators=(',', ':'))
    with tmp.open('rb') as handle:
        os.fsync(handle.fileno())
    tmp.replace(path)


def scope_problems(root, task, saved, snapshot):
    before = saved['files']
    target_paths = {file: _path(root, file) for file, _ in targets(task)}
    allowed = {str(path) for path in target_paths.values()} | {str(_path(root, name)) for name in task['test_files']}
    changed = {p for p in before.keys() | snapshot.keys() if before.get(p) != snapshot.get(p)}
    if task.get('kind') == 'verification':
        if changed:
            return ['Verification-only task changed workspace files; restore the original candidate: '
                    + ', '.join(sorted(str(Path(p).relative_to(root)) for p in changed)[:20])], None
        hashes = {file: target_state(target_paths[file], symbol)['body'] for file, symbol in targets(task)}
        if any(h is None for h in hashes.values()):
            return ['Verification-only target no longer exists'], None
        return [], hashes
    extra = sorted(str(Path(p).relative_to(root)) for p in changed - allowed)
    if extra:
        return ['Restore changes outside this assignment: ' + ', '.join(extra[:20])], None
    if any(str(root / name) in before for name in task['test_files']):
        return ['Preserve existing regression test files'], None
    if any(str(root / name) not in snapshot for name in task['test_files']):
        return ['Add the declared focused regression tests in the monitored workspace'], None
    states = saved.get('targets')
    if states is None and task.get('kind') != 'integration':
        states = {task['file']: saved.get('target')}
    if not isinstance(states, dict) or set(states) != target_paths.keys() or any(not isinstance(v, dict) for v in states.values()):
        raise ValueError('Saved function scope baseline does not match the assigned targets')
    gaps, hashes = [], {}
    for file, symbol in targets(task):
        if str(target_paths[file]) not in changed:
            gaps.append('The assigned source file did not change: ' + file)
            continue
        old, new = states[file], target_state(target_paths[file], symbol)
        label = file + '::' + symbol
        if new['body'] is None or new['body'] == old['body']:
            gaps.append('Implement or change the assigned function: ' + label)
        elif (old['outside'] is not None and old['outside'] != new['outside']
                or old['outside'] is None and old['whole'] not in new['without']):
            gaps.append('Restore edits outside the assigned function: ' + label)
        else:
            hashes[file] = new['body']
    return gaps, hashes if not gaps else None


def coverage(ledger, modules, task, brief, root, *, provisional=None):
    module_ids = (list(modules) if task['delivery_scope'] == 'requirement' else [task['module_id']])
    required = {key for module_id in module_ids for key in criteria(modules, module_id)}
    if task['delivery_scope'] == 'requirement':
        required.update(requirement_criteria(brief))
    covered = set(task['criterion_ids'])
    if provisional:
        covered.update(provisional['criterion_ids'])
    current_hashes = {}
    def body(file, symbol):
        key = (file, symbol)
        if key not in current_hashes:
            try:
                current_hashes[key] = target_state(_path(root, file), symbol)['body']
            except ValueError:
                current_hashes[key] = None
        return current_hashes[key]
    for item in reversed(ledger.accepted):
        old = item['task']
        if not item.get('verification') or not required.intersection(old['criterion_ids']) - covered:
            continue
        accepted = item.get('target_hashes') if old.get('kind') == 'integration' else {old['file']: item['symbol_hash']}
        if all(body(file, symbol) == accepted[file] for file, symbol in targets(old)):
            covered.update(old['criterion_ids'])
    return sorted(required - covered)
