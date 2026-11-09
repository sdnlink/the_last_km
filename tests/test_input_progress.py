"""Progress about authorization must not terminate an active reviewer."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from auto_moniter.providers import PreparedCommand
from auto_moniter.supervisor import run_command
from auto_moniter.verdict import input_request


class InputProgressTests(unittest.TestCase):
    def test_review_progress_does_not_cross_sentence_boundaries(self):
        progress = '请求测试。角色切换代码仍有两处具体疑点：旧检查点的默认字段未同步到授权记录。我正在核对完整调用链和证据哈希。'
        self.assertEqual(input_request(progress), '')
        for text in ['需要确认角色恢复逻辑，我会继续核对。', '已检查授权记录，现在执行回归。']:
            self.assertEqual(input_request(text), '')

    def test_explicit_human_requests_are_still_detected(self):
        for text in ['请确认是否继续', '请您先确认配置', '等待用户的授权', '需要你提供凭据', 'Would you like me to continue?']:
            self.assertTrue(input_request(text), text)

    def test_progress_stream_does_not_interrupt_work_between_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            event={'type':'item.completed','item':{'type':'agent_message','text':'请求测试。正在核对授权记录。'}}
            code='import json,time; print('+repr(json.dumps(event,ensure_ascii=False))+',flush=True);time.sleep(0.15);print("finished",flush=True)'
            result=run_command(PreparedCommand([sys.executable,'-c',code]),Path(tmp),input_grace_s=0.02,timeout_s=3)
            self.assertEqual(result.exit_code,0)
            self.assertEqual(result.interruption,'')
            self.assertIn('finished',result.stdout)
