"""Read-only syntax rules for optional function-scope parsers."""

from dataclasses import dataclass
from functools import lru_cache
import re


@dataclass(frozen=True)
class Rule:
    label: str
    suffixes: str
    functions: str
    scopes: str = ''


# Keep registration explicit. A grammar alone doesn't establish which node
# is an executable definition, or how to address that node unambiguously.
RULES = {
    'typescript': Rule('TypeScript', '.ts .mts .cts',
        'function_declaration generator_function_declaration method_definition',
        'internal_module class_declaration abstract_class_declaration'),
    'tsx': Rule('TSX', '.tsx', 'function_declaration generator_function_declaration method_definition',
        'internal_module class_declaration abstract_class_declaration'),
    'go': Rule('Go', '.go', 'function_declaration method_declaration'),
    'rust': Rule('Rust', '.rs', 'function_item', 'mod_item trait_item impl_item'),
    'java': Rule('Java', '.java', 'method_declaration constructor_declaration compact_constructor_declaration',
        'class_declaration interface_declaration enum_declaration record_declaration'),
    'c': Rule('C', '.c', 'function_definition'),
    'cpp': Rule('C++', '.cc .cpp .cxx .h .hh .hpp .hxx', 'function_definition',
        'namespace_definition class_specifier struct_specifier union_specifier'),
    'csharp': Rule('C#', '.cs', 'method_declaration constructor_declaration local_function_statement',
        'namespace_declaration class_declaration struct_declaration interface_declaration record_declaration'),
    'kotlin': Rule('Kotlin', '.kt .kts', 'function_declaration', 'class_declaration object_declaration'),
    'swift': Rule('Swift', '.swift', 'function_declaration', 'class_declaration protocol_declaration'),
    'ruby': Rule('Ruby', '.rb', 'method singleton_method', 'module class'),
    'php': Rule('PHP', '.php', 'function_definition method_declaration',
        'namespace_definition class_declaration trait_declaration interface_declaration enum_declaration'),
    'lua': Rule('Lua', '.lua', 'function_declaration'),
    'bash': Rule('Bash', '.sh .bash', 'function_definition'),
}
GRAMMARS = {suffix: name for name, rule in RULES.items() for suffix in rule.suffixes.split()}


@lru_cache(maxsize=len(RULES))
def _language(name):
    # The pinned extra ships local native grammars and never downloads them
    # at task time. No parser or plugin is loaded from the target repository.
    from tree_sitter_language_pack import get_language
    return get_language(name)


def parser(name):
    try:
        from tree_sitter import Parser
        return Parser(_language(name))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{RULES[name].label} parser unavailable; install auto-moniter[languages] "
                         "in the monitor Python environment") from exc


def availability():
    found = {}
    for name, rule in RULES.items():
        try:
            parser(name)
            available = True
        except ValueError:
            available = False
        found[rule.label] = {'extensions': rule.suffixes.split(), 'available': available}
    return found


def _text(node):
    return node.text.decode() if node is not None else ''


def _first(node, *types):
    return next((child for child in node.named_children if child.type in types), None)


def _names(name):
    parts = re.split(r'\s*(?:::|[.:\\])\s*', name)
    return tuple(parts) if all(re.fullmatch(r'[\w$~]+[!?=]?', part) for part in parts) else ()


def _type_name(node):
    while node is not None:
        if node.type in {'identifier', 'type_identifier', 'scoped_type_identifier', 'qualified_identifier'}:
            return _names(_text(node))
        node = node.child_by_field_name('type') or _first(node, 'type_identifier', 'generic_type')
    return ()


def _declarator_name(node):
    while node is not None:
        if node.type in {'identifier', 'field_identifier', 'qualified_identifier', 'destructor_name'}:
            return _names(_text(node))
        node = node.child_by_field_name('declarator')
    return ()


def _swift_conditional_tree(engine, data, tree):
    # The bundled Swift grammar misparses conditional computed properties in
    # enums. Retain every branch and byte offset, blanking only directives
    # already recognized by the parser. This is structural scope discovery,
    # not a substitute for the project's Swift compiler checks.
    directives = []
    stack = [tree.root_node]
    depth = []
    while stack:
        node = stack.pop()
        if node.type == 'directive':
            keyword = node.children[0].type if node.children else ''
            if keyword in {'#if', '#elseif', '#else', '#endif'}:
                if node.has_error:
                    return tree
                if keyword == '#if':
                    depth.append(False)
                elif not depth:
                    return tree
                elif keyword == '#endif':
                    depth.pop()
                elif depth[-1]:
                    return tree
                elif keyword == '#else':
                    depth[-1] = True
                directives.append(node)
        stack.extend(reversed(node.named_children))
    if not directives or depth:
        return tree
    masked = bytearray(data)
    for node in directives:
        for index in range(node.start_byte, node.end_byte):
            if masked[index] not in (10, 13):
                masked[index] = 32
    return engine.parse(bytes(masked))


def _rust_reference_tree(engine, data, tree):
    # The pinned grammar interprets `&raw)` as an unfinished raw-pointer
    # expression, although `raw` is a legal identifier. Mask only this exact
    # error in reference-expression positions, retaining every byte offset.
    masked = bytearray(data)
    changed = False
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if (node.type == 'ERROR' and re.fullmatch(rb'&\s*raw', node.text)
                and data[node.end_byte:].lstrip().startswith((b')', b',', b';', b']', b'}', b'.', b'?'))):
            masked[node.end_byte - 3:node.end_byte] = b'r_w'
            changed = True
        stack.extend(node.named_children)
    return engine.parse(bytes(masked)) if changed else tree


def spans(source, symbol, name):
    data = source.encode()
    engine = parser(name)
    tree = engine.parse(data)
    if name == 'rust' and tree.root_node.has_error:
        tree = _rust_reference_tree(engine, data, tree)
    if name == 'swift' and tree.root_node.has_error:
        tree = _swift_conditional_tree(engine, data, tree)
    if tree.root_node.has_error:
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == 'ERROR' or node.is_missing:
                raise ValueError(f'Cannot parse valid {RULES[name].label} source for function scope verification '
                                 f'at line {node.start_point.row + 1}, column {node.start_point.column + 1}')
            stack.extend(reversed(node.children))
        raise ValueError(f'Cannot parse valid {RULES[name].label} source for function scope verification')
    wanted = _names(symbol)
    rule = RULES[name]
    functions = set(rule.functions.split())
    scopes = set(rule.scopes.split())
    matches = []
    stack = [(tree.root_node, ())]
    while stack:
        node, prefix = stack.pop()
        kind = node.type
        body = node.child_by_field_name('body')
        field = node.child_by_field_name('name')
        declared = _names(_text(field))
        child_prefix = prefix
        definition = node
        if kind in scopes:
            if kind == 'impl_item':
                declared = _type_name(node.child_by_field_name('type'))
            elif name == 'kotlin':
                declared = _names(_text(_first(node, 'type_identifier', 'simple_identifier')))
            if not declared:
                # Anonymous containers do not expose addressable named members.
                continue
            child_prefix += declared
        if kind in functions:
            if name in {'c', 'cpp'}:
                declared = _declarator_name(node.child_by_field_name('declarator'))
                if node.parent.type == 'template_declaration':
                    definition = node.parent
            elif name == 'go' and kind == 'method_declaration':
                receiver = node.child_by_field_name('receiver')
                parameter = _first(receiver, 'parameter_declaration') if receiver else None
                owner = _type_name(parameter.child_by_field_name('type')) if parameter else ()
                declared = owner + declared if owner else ()
            elif name == 'kotlin':
                declared = _names(_text(_first(node, 'simple_identifier')))
                body = _first(node, 'function_body')
            elif name == 'ruby':
                # Ruby also permits empty method definitions.
                body = node
                if kind == 'singleton_method':
                    owner = _names(_text(node.child_by_field_name('object')))
                    declared = (owner + declared if owner != ('self',) else declared) if owner else ()
            if not declared:
                continue
            child_prefix = prefix + declared
            if body is not None and child_prefix == wanted:
                matches.append(definition)
        elif name in {'typescript', 'tsx'} and kind in {'variable_declarator', 'public_field_definition'}:
            value = node.child_by_field_name('value')
            if declared and value is not None and value.type in {'arrow_function', 'function_expression', 'generator_function'}:
                child_prefix = prefix + declared
                parent = node.parent
                if kind == 'variable_declarator' and sum(c.type == kind for c in parent.named_children) == 1:
                    definition = parent
                if child_prefix == wanted:
                    matches.append(definition)
        children = []
        for child in node.named_children:
            if name == 'csharp' and child.type == 'file_scoped_namespace_declaration':
                child_prefix = prefix + _names(_text(child.child_by_field_name('name')))
            elif name == 'php' and child.type == 'namespace_definition' and child.child_by_field_name('body') is None:
                child_prefix = prefix + _names(_text(child.child_by_field_name('name')))
            else:
                children.append((child, child_prefix))
        stack.extend(reversed(children))
    # Only a unique candidate is needed; avoid work proportional to repeated names.
    return [(len(data[:node.start_byte].decode()), len(data[:node.end_byte].decode())) for node in matches[:2]]
