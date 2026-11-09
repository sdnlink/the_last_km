"""Grok 1.0.77 terminal structured output and public activity protocol."""
import json
import tempfile
import unittest
from pathlib import Path
from auto_moniter.providers import extract_text, extract_session_id
from auto_moniter.live import LiveOutput, public_events
from auto_moniter.metering import UsageMeter

class GrokStreamingTests(unittest.TestCase):
    def test_structured_terminal_wins_over_fragments_and_tool_content(self):
        verdict={'status':'continue','summary':'Verified actual work','gaps':['remaining']}
        events=[{'type':'text','data':'preliminary'}, {'type':'tool_call_update','content':'untrusted'},
                {'type':'end','structuredOutput':verdict,'sessionId':'01a1be71-1f36-75f0-91b8-fbfd08674560'}]
        stream='\n'.join(map(json.dumps,events))
        text,structured=extract_text(stream)
        self.assertTrue(structured);self.assertEqual(json.loads(text),verdict)
        self.assertEqual(extract_session_id(stream),'01a1be71-1f36-75f0-91b8-fbfd08674560')

    def test_incremental_text_fallback_is_joined_without_reasoning(self):
        stream='\n'.join(map(json.dumps,[{'type':'text','data':'{"status":'},
            {'type':'thinking','data':'private'}, {'type':'text','data':'"continue","summary":"ok"}'}]))
        text,structured=extract_text(stream)
        self.assertFalse(structured);self.assertEqual(json.loads(text)['summary'],'ok')

    def test_terminal_usage_counted_once(self):
        meter=UsageMeter();meter.record('\n'.join(map(json.dumps,[
            {'type':'usage','usage':{'input_tokens':20,'output_tokens':5}},
            {'type':'end','usage':{'input_tokens':20,'output_tokens':5,'cache_read_input_tokens':10},'total_cost_usd':0.03}])))
        self.assertEqual(meter.state['tokens'],35)
        self.assertEqual(meter.state['cost_usd'],0.03)
        self.assertEqual(meter.state['reported_turns'],1)

    def test_tool_calls_and_public_output_are_visible(self):
        call={'type':'tool_call','toolCallId':'c1','toolName':'run_terminal_command',
              'rawInput':{'command':'python -m unittest'}}
        event=public_events(call)[0]
        self.assertEqual((event['kind'],event['text']),('command','python -m unittest'))
        output={'type':'tool_call_update','toolCallId':'c1','status':'completed',
                'content':[{'type':'content','content':{'type':'text','text':'passed'}}]}
        self.assertEqual(public_events(output)[0]['text'],'passed')
        self.assertEqual(public_events({'type':'thinking','data':'private'}),[])

    def test_delta_public_output_is_batched_and_flushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);live=LiveOutput(path,'worker',1,'grok')
            for part in ['正在','检查','接口']:
                live.feed(0,(json.dumps({'type':'text','data':part})+'\n').encode())
            live.close();entries=[json.loads(v) for v in (path/'live.jsonl').read_text().splitlines()]
            self.assertEqual(entries[-1]['text'],'正在检查接口')
            self.assertEqual(len(entries),2)

    def test_grok_tool_execution_uses_prompt_contract_without_schema_generation(self):
        from auto_moniter.providers import resolve_provider
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);prompt=root/'prompt.md';schema=root/'schema.json'
            prompt.write_text('Do work, then return the required JSON conclusion.');schema.write_text('{}')
            provider=resolve_provider('grok')
            for command in [provider.start_command(prompt,root,schema),
                            provider.resume_command('01a1be71-1f36-75f0-91b8-fbfd08674560',prompt,root,schema)]:
                self.assertNotIn('--json-schema',command.argv)
                self.assertIn('--prompt-file',command.argv)
                self.assertIn('streaming-json',command.argv)

    def test_text_deltas_after_assistant_message_are_latest_conclusion(self):
        events=[{'type':'assistant','text':'Starting work'},
                {'type':'text','data':'{"status":"continue","summary":"new","gaps":[]}'}]
        self.assertEqual(json.loads(extract_text('\n'.join(map(json.dumps,events)))[0])['summary'],'new')
