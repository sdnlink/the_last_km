"""Apply an independently reviewed setup change to an isolated test copy."""

import hashlib
import re

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {**{key: {'type': 'string'} for key in ('test_blob', 'original', 'replacement')},
                   'rust_tests': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'}}},
    'required': ['test_blob', 'original', 'replacement', 'rust_tests'],
}


def valid(value):
    if not isinstance(value, dict) or set(value) - {'rust_tests'} != {'test_blob', 'original', 'replacement'}:
        return False
    names = value.get('rust_tests', [])
    return (isinstance(names, list) and len(names) <= 8
            and all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_:]{1,200}', name) for name in names)
            and len(set(names)) == len(names)
            and all(isinstance(value[key], str) and '\0' not in value[key]
                    for key in ('test_blob', 'original', 'replacement'))
            and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['test_blob']) is not None
            and value['original'] != value['replacement']
            and all(0 < len(text) <= 2048 and text.strip()
                    and all(char.isprintable() or char in '\t\r\n' for char in text)
                    and not re.search(r'\b(?:assert\w*|skip\w*)\b', text, re.I)
                    for text in (value['original'], value['replacement'])))


def revised_source(source, revision, blob):
    if (not valid(revision) or blob != revision['test_blob'] or len(source) > 128 * 1024
            or object_id(source, blob) != blob):
        raise ValueError('测试准备修订与受保护测试对象不一致。')
    original = revision['original'].encode()
    replacement = revision['replacement'].encode()
    if source.count(original) != 1:
        raise ValueError('受保护测试中须恰好出现一处拟修订的准备代码。')
    position = source.index(original)
    end = position + len(original)
    if ((position and source[position - 1:position] != b'\n')
            or end < len(source) and not original.endswith(b'\n') and source[end:end + 1] not in (b'\r', b'\n')
            or original.endswith(b'\n') != replacement.endswith(b'\n')):
        raise ValueError('测试准备修订须替换完整代码行，并保留结尾换行。')
    return source.replace(original, replacement, 1)


def object_id(source, original_blob):
    digest = hashlib.new('sha1' if len(original_blob) == 40 else 'sha256')
    digest.update(f'blob {len(source)}\0'.encode())
    digest.update(source)
    return digest.hexdigest()
