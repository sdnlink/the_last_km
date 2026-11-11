import tempfile
import unittest
from pathlib import Path

from auto_moniter.workspace import acceptance_problems, evidence_paths, file_snapshot, preserve_tree, restore_reviewer_writes


class AcceptanceTests(unittest.TestCase):
    def test_existing_file_is_not_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("old\n", encoding="utf-8")
            baseline = file_snapshot(root)
            problems = acceptance_problems(root, ["README.md"], baseline=baseline)
        self.assertTrue(problems)

    def test_file_touched_during_run_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "app.py"
            target.write_text("old\n", encoding="utf-8")
            baseline = file_snapshot(root)
            target.write_text("new\n", encoding="utf-8")
            problems = acceptance_problems(root, ["app.py"], baseline=baseline, diff_text="app.py changed")
        self.assertEqual(problems, [])

    def test_changed_file_must_appear_in_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("old\n", encoding="utf-8")
            baseline = file_snapshot(root)
            target.write_text("new\n", encoding="utf-8")
            problems = acceptance_problems(root, ["notes.txt"], baseline=baseline, diff_text="only app.py")
        self.assertTrue(any("does not appear" in problem for problem in problems))

    def test_line_references_with_chinese_annotations_remain_file_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / '说明（视图）.md'
            target.write_text('before')
            baseline = file_snapshot(root)
            target.write_text('after')
            for evidence in ['说明（视图）.md:694：流程已核对。', '说明（视图）.md：694：已核对',
                             '说明（视图）.md:694同样要求校验', '当前变更文件 说明（视图）.md:694-700：已修复',
                             '`说明（视图）.md:694:2`：已修复']:
                with self.subTest(evidence=evidence):
                    self.assertEqual(acceptance_problems(root, [evidence], baseline=baseline), [])

    def test_annotations_do_not_allow_unchanged_or_outside_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            project.mkdir()
            (project / 'unchanged.md').write_text('old')
            (root / 'outside.md').write_text('outside')
            baseline = file_snapshot(project)
            for evidence in ['unchanged.md:12：已核对', '../outside.md:12：已修复',
                             'missing.md:12：已修复', '.auto-moniter/history/record.json:1：结论']:
                with self.subTest(evidence=evidence):
                    self.assertTrue(acceptance_problems(project, [evidence], baseline=baseline))

    def test_criterion_prefix_and_chinese_punctuation_preserve_file_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'preview' / 'input-state.js'
            target.parent.mkdir()
            target.write_text('before')
            baseline = file_snapshot(root)
            target.write_text('after')
            for text in [
                'QUOTES / QUOTES:3、req:1:3：preview/input-state.js:86 在证券校验后恢复合法失败文字；报价校验保持不变。',
                '开始与结束文件哈希对照确认：仅修改 preview/input-state.js、新增指定测试；restore 外代码一致。',
                'QUOTES:3、req:1:3：`preview/input-state.js:86–90`：已复核',
                '标准：' + str(target) + ':86:2；已修复',
            ]:
                with self.subTest(text=text):
                    self.assertIn(target, evidence_paths(root, [text], baseline=baseline))
                    self.assertEqual(acceptance_problems(root, [text], baseline=baseline), [])

    def test_prefixed_citations_still_require_changed_project_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            project.mkdir()
            (project / 'unchanged.js').write_text('same')
            (root / 'outside.js').write_text('outside')
            baseline = file_snapshot(project)
            texts = ['M:1、req:1:3：unchanged.js:86；已复核',
                     'M:1、req:1:3：../outside.js:86；已复核',
                     'M:1、req:1:3：missing.js:86；已复核']
            (project / 'link.js').symlink_to(root / 'outside.js')
            texts.append('M:1：link.js:86，已修复')
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(evidence_paths(project, [text], baseline=baseline), [])

    def test_reviewer_writes_are_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "app.py"
            original.write_text("worker\n", encoding="utf-8")
            before = preserve_tree(root, root / "unused")
            (root / "sneaky.txt").write_text("new\n", encoding="utf-8")
            restored = restore_reviewer_writes(root, before)
            self.assertIn("sneaky.txt", restored)
            self.assertEqual(original.read_text(encoding="utf-8"), "worker\n")
            self.assertFalse((root / "sneaky.txt").exists())


if __name__ == "__main__":
    unittest.main()
