"""Cargo-qualified targets must measure the same function before and after edits."""
import gzip
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.function_gate import baseline, scope_problems, source_state, target_state
from auto_moniter.workspace import file_snapshot


SOURCE = 'fn run() -> i32 { 123 }\nfn other() -> i32 { 456 }\n'


@unittest.skipUnless(importlib.util.find_spec('tree_sitter_language_pack'), 'Rust scope tests require language parsers')
class RustCrateFunctionScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'project'
        (self.root/'src').mkdir(parents=True)
        (self.root/'Cargo.toml').write_text('[package]\nname="video-security-catalog"\nversion="0.1.0"\n')
        self.file = self.root/'src/lib.rs'
        self.file.write_text(SOURCE)
        self.task = {'id':'F1', 'file':'src/lib.rs', 'symbol':'video_security_catalog::run', 'test_files':[]}
        self.saved = self.root.parent/'baseline.gz'

    def test_root_crate_and_crate_keyword_resolve_exact_function(self):
        expected = target_state(self.file,'run')
        self.assertIsNotNone(expected['body'])
        self.assertEqual(target_state(self.file,self.task['symbol']),expected)
        self.assertEqual(target_state(self.file,'crate::run'),expected)

    def test_nested_file_modules_and_inline_modules_keep_their_full_paths(self):
        file = self.root/'src/asset_report/catalog.rs'
        file.parent.mkdir()
        file.write_text('mod inner { fn run() -> i32 {123} }\nfn run() -> i32 {456}\n')
        self.assertEqual(target_state(file,'video_security_catalog::asset_report::catalog::inner::run'),
                         target_state(file,'inner::run'))
        self.assertEqual(target_state(file,'crate::asset_report::catalog::run'),target_state(file,'run'))
        self.assertIsNone(target_state(file,'other_crate::asset_report::catalog::run')['body'])
        self.assertIsNone(target_state(file,'video_security_catalog::wrong::catalog::run')['body'])

    def test_crate_prefix_does_not_accidentally_select_same_named_inline_module(self):
        self.file.write_text(SOURCE+'mod video_security_catalog {fn run() -> i32 {789}}\n')
        self.assertEqual(target_state(self.file,'video_security_catalog::run'),target_state(self.file,'run'))
        self.assertNotEqual(target_state(self.file,'crate::video_security_catalog::run'),target_state(self.file,'run'))

    def test_explicit_library_name_and_mod_rs_are_respected(self):
        (self.root/'Cargo.toml').write_text('[package]\nname="package-name"\nversion="0.1.0"\n[lib]\nname="public_api"\n')
        file = self.root/'src/nested/mod.rs'
        file.parent.mkdir()
        file.write_text(SOURCE)
        self.assertEqual(target_state(file,'public_api::nested::run'),target_state(file,'run'))
        self.assertIsNone(target_state(file,'package_name::nested::run')['body'])

    def test_changed_function_passes_but_outside_edit_still_fails(self):
        saved = baseline(self.saved,self.root,self.task,file_snapshot(self.root),missing_ok=True)
        self.file.write_text(SOURCE.replace('123','321'))
        gaps, hashes = scope_problems(self.root,self.task,saved,file_snapshot(self.root))
        self.assertEqual(gaps,[])
        self.assertIn('src/lib.rs',hashes)
        self.file.write_text(SOURCE.replace('123','321').replace('456','654'))
        gaps, hashes = scope_problems(self.root,self.task,saved,file_snapshot(self.root))
        self.assertIn('outside the assigned function',gaps[0])
        self.assertIsNone(hashes)

    def legacy_baseline(self):
        saved={'id':'F1','files':file_snapshot(self.root),'targets':{'src/lib.rs':{
            'whole':hashlib.sha256(SOURCE.encode()).hexdigest(),'outside':None,'body':None}}}
        with gzip.open(self.saved,'wt') as stream:json.dump(saved,stream)
        self.file.write_text(SOURCE.replace('123','321'))
        return saved

    def test_legacy_baseline_recovers_only_hash_matching_original_source(self):
        original = self.legacy_baseline()
        saved=baseline(self.saved,self.root,self.task,file_snapshot(self.root),missing_ok=False,
            historical_source=lambda file,digest:SOURCE)
        self.assertEqual(saved['files'],original['files'])
        self.assertEqual(saved['targets']['src/lib.rs'],source_state(self.file,self.task['symbol'],SOURCE))
        gaps, hashes=scope_problems(self.root,self.task,saved,file_snapshot(self.root))
        self.assertEqual(gaps,[])
        with gzip.open(self.saved,'rt') as stream:self.assertIsNotNone(json.load(stream)['targets']['src/lib.rs']['body'])

    def test_current_or_wrong_source_cannot_replace_original_baseline(self):
        original=self.legacy_baseline()
        for source in (None,self.file.read_text(),SOURCE.replace('456','999')):
            with self.subTest(source=source):
                saved=baseline(self.saved,self.root,self.task,file_snapshot(self.root),missing_ok=False,
                    historical_source=lambda file,digest:source)
                self.assertEqual(saved,original)
                gaps,_=scope_problems(self.root,self.task,saved,file_snapshot(self.root))
                self.assertTrue(gaps)

    def test_unmodified_function_is_still_not_an_implementation_delivery(self):
        saved=baseline(self.saved,self.root,self.task,file_snapshot(self.root),missing_ok=True)
        gaps,_=scope_problems(self.root,self.task,saved,file_snapshot(self.root))
        self.assertIn('did not change',gaps[0])


if __name__=='__main__':unittest.main()
