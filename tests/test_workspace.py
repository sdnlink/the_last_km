import tempfile
import unittest
from pathlib import Path

from auto_moniter.workspace import acceptance_problems, file_snapshot, preserve_tree, restore_reviewer_writes


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
