"""Requirement notes must not become approved commands or corrupt saved catalogs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console.acceptance import validate_commands
from console.tasks import TaskError, TaskStore


class NumberedCheckInputTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.project=self.root/'project';self.project.mkdir()
        self.store=TaskStore(self.root/'console',self.project)

    def test_pasted_numbered_notes_are_rejected_before_save_or_launch(self):
        for command in ('1、实际引擎环境说明','2、验收范围按设备角色确定','1. cargo test',' 2．pytest -q'):
            with self.subTest(command=command),patch.object(self.store,'_save') as save,patch.object(self.store,'_launch') as launch:
                with self.assertRaisesRegex(TaskError,'实际可执行命令'):
                    self.store.create({'title':'Input validation','requirement':'Keep the full requirement',
                        'checks':[command]})
                save.assert_not_called();launch.assert_not_called()

    def test_module_checks_cannot_hide_numbered_notes(self):
        with self.assertRaisesRegex(TaskError,'实际可执行命令'):
            self.store.create({'title':'Module checks','requirement':'Keep the original scope',
                'modules':[{'id':'M1','title':'Behavior','acceptance':['Works'],
                            'checks':['2、按设备角色验收']}]})

    def test_real_commands_with_unicode_paths_and_numbered_arguments_remain_unchanged(self):
        commands=['cargo test --workspace','python3 "测试/1、回归.py"','echo "2、说明"','./1、测试.sh','v1.2 --check']
        validate_commands(commands)
        self.assertEqual(commands[0],'cargo test --workspace')
        task=self.store.create({'title':'Real command','requirement':'Test the behavior','checks':commands})
        self.assertEqual(self.store._get(task['id'])['checks'],commands)

    def test_invalid_addition_preserves_existing_ids_and_checkpoint(self):
        task=self.store.create({'title':'Saved catalog','requirement':'Original','checks':['git diff --check']})
        run=Path(task['run_dir']);run.mkdir()
        (run/'requirement.md').write_text('Original')
        state={'cwd':str(self.project),'status':'stopped','checks':['git diff --check']}
        (run/'state.json').write_text(json.dumps(state))
        previous=(run/'state.json').read_bytes()
        with patch.object(self.store,'_resume_args',return_value=['run','--resume']):
            with self.assertRaisesRegex(TaskError,'实际可执行命令'):
                self.store.action(task['id'],'checks',{'additional_checks':['2、设备角色验收']})
        self.assertEqual((run/'state.json').read_bytes(),previous)
        self.assertEqual(self.store._get(task['id'])['checks'],state['checks'])


if __name__=='__main__':unittest.main()
