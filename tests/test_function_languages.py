"""Behavioral scope checks, using native grammars without compiling project code."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auto_moniter import function_languages
from auto_moniter.function_gate import baseline, scope_problems, target_state, validate_task
from auto_moniter.workspace import file_snapshot


SAMPLES = {
    'typescript': ('sample.ts', 'Api.Service.run',
        'namespace Api { export class Service { run(x: number): number { return 123; } other() { return 456; } } }'),
    'tsx': ('sample.tsx', 'Item', 'export const Item = () => <div>123</div>; const other = 456;'),
    'go': ('sample.go', 'Service.Run',
        'package api\ntype Service[T any] struct{}\nfunc (s *Service[T]) Run() int { return 123 }; func Other() int { return 456 }'),
    'rust': ('sample.rs', 'api.Service.run',
        'mod api { impl<T> Service<T> { fn run(&self) -> i32 { 123 } fn other(&self) -> i32 { 456 } } }'),
    'java': ('Service.java', 'Service.run',
        'class Service { int run() { return 123; } int other() { return 456; } }'),
    'c': ('sample.c', 'run', 'int *run(void) { return (int *)123; } int other(void) { return 456; }'),
    'cpp': ('sample.cpp', 'api.Service.run',
        'namespace api { class Service { int run() { return 123; } int other() { return 456; } }; }'),
    'csharp': ('Service.cs', 'Api.Service.Run',
        'namespace Api; class Service { public int Run() => 123; public int Other() => 456; }'),
    'kotlin': ('Service.kt', 'Service.run',
        'class Service {\n fun run(): Int { return 123 }\n fun other(): Int = 456\n}\n'),
    'swift': ('Service.swift', 'Service.run',
        'struct Service { func run() -> Int { return 123 }; func other() -> Int { return 456 } }'),
    'ruby': ('sample.rb', 'Api.Service.run',
        'module Api; class Service; def run; 123; end; def other; 456; end; end; end'),
    'php': ('sample.php', 'Api.Service.run',
        '<?php namespace Api; class Service { function run() { return 123; } function other() { return 456; } }'),
    'lua': ('sample.lua', 'Service.run',
        'function Service.run() return 123 end\nfunction Service.other() return 456 end'),
    'bash': ('sample.sh', 'run', 'run() { printf 123; }; other() { printf 456; }'),
}


@unittest.skipUnless(importlib.util.find_spec('tree_sitter_language_pack'),
                     'Install auto-moniter[languages] to verify optional grammar rules')
class LanguageScopeTests(unittest.TestCase):
    def test_every_registered_language_preserves_outside_code(self):
        self.assertEqual(set(SAMPLES), set(function_languages.RULES))
        with tempfile.TemporaryDirectory() as directory:
            for language, (file, symbol, source) in SAMPLES.items():
                with self.subTest(language=language):
                    path = Path(directory) / file
                    path.write_text(source, encoding='utf-8')
                    old = target_state(path, symbol)
                    self.assertIsNotNone(old['body'])
                    path.write_text(source.replace('123', '12345'), encoding='utf-8')
                    changed = target_state(path, symbol)
                    self.assertNotEqual(old['body'], changed['body'])
                    self.assertEqual(old['outside'], changed['outside'])
                    path.write_text(source.replace('456', '45678'), encoding='utf-8')
                    outside = target_state(path, symbol)
                    self.assertEqual(old['body'], outside['body'])
                    self.assertNotEqual(old['outside'], outside['outside'])

    def test_watcher_rejects_outside_edits_for_every_registered_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            for language, (file, symbol, source) in SAMPLES.items():
                with self.subTest(language=language):
                    path = root / file
                    path.write_text(source)
                    task = {'id':language, 'file':file, 'symbol':symbol, 'test_files':[]}
                    saved = baseline(Path(directory)/'baseline.gz', root, task,
                                     file_snapshot(root), missing_ok=True)
                    path.write_text(source.replace('123', '12345'))
                    gaps, hashes = scope_problems(root, task, saved, file_snapshot(root))
                    self.assertEqual(gaps, [])
                    self.assertEqual(set(hashes), {file})
                    path.write_text(source.replace('123', '12345').replace('456', '45678'))
                    gaps, hashes = scope_problems(root, task, saved, file_snapshot(root))
                    self.assertTrue(gaps)
                    self.assertIn('outside the assigned function', gaps[0])
                    self.assertIsNone(hashes)
                    path.unlink()

    def test_overloads_and_duplicate_names_cannot_select_arbitrary_function(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Service.java'
            path.write_text('class Service { int run() {return 1;} int run(int x) {return x;} }')
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                target_state(path, 'Service.run')

    def test_syntax_error_anywhere_in_file_refuses_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.go'
            path.write_text('package api\nfunc Run() int {return 1}\nfunc Other( {')
            with self.assertRaisesRegex(ValueError, 'Cannot parse valid Go'):
                target_state(path, 'Run')

    def test_unicode_offsets_and_templates_preserve_prefix_and_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.cpp'
            source = '// 中文 😀\nnamespace api { template<class T> T run(T x) { return x; } int other(){return 0;} }'
            path.write_text(source)
            old = target_state(path, 'api.run')
            path.write_text(source.replace('return x;', 'return x + x;'))
            new = target_state(path, 'api.run')
            self.assertNotEqual(old['body'], new['body'])
            self.assertEqual(old['outside'], new['outside'])

    def test_declarations_without_implementation_are_not_deliverables(self):
        cases = [('Service.java', 'Service.run', 'abstract class Service { abstract int run(); }'),
                 ('sample.cpp', 'run', 'int run(int x);'),
                 ('sample.rs', 'Contract.run', 'trait Contract { fn run(&self); }')]
        with tempfile.TemporaryDirectory() as directory:
            for file, symbol, source in cases:
                with self.subTest(file=file):
                    path = Path(directory) / file
                    path.write_text(source)
                    self.assertIsNone(target_state(path, symbol)['body'])


class ParserAvailabilityTests(unittest.TestCase):
    def test_missing_parser_has_actionable_message_without_fallback(self):
        with patch.dict('sys.modules', {'tree_sitter': None}):
            with self.assertRaisesRegex(ValueError, r'Go parser unavailable.*auto-moniter\[languages\]'):
                function_languages.parser('go')

    def test_missing_parser_rejects_a_new_target_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', {'tree_sitter': None}):
            task = {'file':'new.go', 'symbol':'Run', 'criterion_ids':['M:1'],
                    'check_ids':['check:1'], 'test_files':[], 'module_id':'M'}
            with self.assertRaisesRegex(ValueError, 'Go parser unavailable'):
                validate_task(task, Path(directory), {'M':{'acceptance':['Works']}}, ['true'], {})

    def test_unregistered_extension_is_not_parsed_as_javascript(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.unknown'
            path.write_text('function run() {}')
            with self.assertRaisesRegex(ValueError, 'does not support'):
                target_state(path, 'run')
