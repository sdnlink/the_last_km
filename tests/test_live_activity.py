import json
import tempfile
import unittest
from pathlib import Path

from auto_moniter.live import LiveOutput, public_events
from console.activity import ActivityReader


class LiveActivityTests(unittest.TestCase):
    def test_stream_decodes_split_unicode_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = LiveOutput(root, 'worker', 4, 'codex')
            event = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                               'text': '正在检查接口，password=example-secret'}}, ensure_ascii=False).encode() + b'\n'
            split = event.index('在'.encode()) + 1
            output.feed(0, event[:split])
            output.feed(0, event[split:])
            output.close()
            entries = [json.loads(line) for line in (root / 'live.jsonl').read_text().splitlines()]
            self.assertIn('正在检查接口', entries[-1]['text'])
            self.assertNotIn('example-secret', entries[-1]['text'])
            self.assertEqual(entries[-1]['round'], 4)

    def test_public_activity_omits_reasoning_and_encrypted_or_user_messages(self):
        records = [
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [{'text': 'private'}]}},
            {'type': 'event_msg', 'payload': {'type': 'agent_reasoning', 'text': 'private'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'private'}]}},
            {'type': 'response_item', 'payload': {'type': 'agent_message', 'content': [{'type': 'encrypted_content', 'encrypted_content': 'private'}]}},
        ]
        for record in records:
            with self.subTest(record=record):
                self.assertEqual(public_events(record), [])
        event = public_events({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'python -m unittest'}})[0]
        self.assertEqual((event['kind'], event['status']), ('command', 'running'))

    def test_match_current_root_session_and_refresh_without_round_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, sessions = root / 'run', root / 'sessions/2026/11/06'
            run.mkdir()
            sessions.mkdir(parents=True)
            started = '2026-11-06T03:50:11+00:00'
            (run / 'events.jsonl').write_text(json.dumps({'at': started, 'event': 'agent_start', 'round': '4', 'role': 'worker', 'provider': 'codex'}) + '\n')
            path = sessions / 'rollout-current.jsonl'
            meta = {'type': 'session_meta', 'payload': {'cwd': '/project', 'timestamp': '2026-11-06T03:50:12Z', 'source': 'exec'}}
            message = {'timestamp': '2026-11-06T03:51:00Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': [{'type': 'output_text', 'text': '正在修复登录接口'}]}}
            path.write_text(json.dumps(meta) + '\n' + json.dumps(message) + '\n')
            unrelated = {**meta, 'payload': {**meta['payload'], 'cwd': '/unrelated'}}
            child = {**meta, 'payload': {**meta['payload'], 'parent_thread_id': 'parent'}}
            (sessions / 'rollout-other.jsonl').write_text(json.dumps(unrelated) + '\n')
            (sessions / 'rollout-child.jsonl').write_text(json.dumps(child) + '\n')
            reader = ActivityReader(root / 'sessions')
            state = {'cwd': '/project', 'next_round': 4}
            health = {'activity': 'round 4: worker running'}
            before = reader.snapshot(run, state, health, 'running', 'codex')
            self.assertEqual(before['focus'], '正在修复登录接口')
            self.assertEqual(before['source'], 'codex_session')
            with path.open('a') as stream:
                stream.write(json.dumps({'timestamp': '2026-11-06T03:52:00Z', 'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'arguments': json.dumps({'cmd': 'python -m unittest tests.test_login'})}}) + '\n')
            after = reader.snapshot(run, state, health, 'running', 'codex')
            self.assertEqual(after['operation']['text'], 'python -m unittest tests.test_login')
            self.assertEqual(after['round'], 4)
            self.assertFalse((run / '004-worker.log').exists())

    def test_ambiguous_same_project_sessions_are_not_attributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / '2026/11/06'
            sessions.mkdir(parents=True)
            meta = {'type': 'session_meta', 'payload': {'cwd': '/project', 'timestamp': '2026-11-06T03:50:12Z', 'source': 'exec'}}
            for name in ('first', 'second'):
                (sessions / f'{name}.jsonl').write_text(json.dumps(meta) + '\n')
            self.assertIsNone(ActivityReader(root)._session('/project', '2026-11-06T03:50:11Z'))

    def test_tool_result_blocks_are_visible_but_encrypted_blocks_are_omitted(self):
        event = public_events({'type': 'response_item', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'call-1', 'output': [
                {'type': 'input_text', 'text': 'Ran 43 tests: OK'},
                {'type': 'encrypted_content', 'encrypted_content': 'private'},
            ]}})[0]
        self.assertEqual(event['text'], 'Ran 43 tests: OK')
        self.assertEqual(event['id'], 'call-1')
        self.assertEqual(event['kind'], 'result')

    def test_completed_task_does_not_claim_current_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = ActivityReader().snapshot(Path(tmp), {}, {}, 'done', 'codex')
            self.assertFalse(snapshot['is_running'])
            self.assertFalse(snapshot['has_output'])


if __name__ == '__main__':
    unittest.main()
