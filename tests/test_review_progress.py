"""Full reviewer output and neutral, actionable read-only boundary failures."""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auto_moniter.live import LiveOutput, public_events
from auto_moniter.storage import write_json
from auto_moniter.supervisor import RunConfig, Supervisor
from console.activity import ActivityReader
from console.diagnostics import diagnose
from console.server import ConsoleServer
from console.tasks import TaskError, TaskStore
from console.workflow import work_details
from tests.test_review_first import BRIEF
from tests.test_supervisor import ScriptedProvider, _verdict


class ReviewProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'

    def test_all_public_message_formats_identify_truncation_without_secret_leaks(self):
        text = '验' * 5000 + ' password=PRIVATE_CREDENTIAL'
        values = [
            {'type':'item.completed','item':{'type':'agent_message','text':text}},
            {'type':'event_msg','payload':{'type':'agent_message','message':text}},
            {'type':'assistant','message':{'content':[{'type':'text','text':text}]}},
            {'type':'end','structuredOutput':{'summary':text}},
            {'type':'result','result':text},
        ]
        for value in values:
            with self.subTest(format=value['type']):
                event = public_events(value)[0]
                self.assertTrue(event['truncated'])
                self.assertEqual(len(event['text']), 4000)
                self.assertGreater(event['text_length'], 5000)
                self.assertNotIn('PRIVATE_CREDENTIAL', json.dumps(event))
        self.assertNotIn('truncated', public_events({'type':'result','result':'complete'})[0])
        self.assertNotIn('truncated', public_events({'type':'result','result':'x'*4000})[0])
        self.run.mkdir()
        live = LiveOutput(self.run, 'reviewer', 0, 'test')
        live.feed(0, ('plain ' + 'x'*5000 + '\n').encode())
        live.feed(0, (json.dumps({'type':'text','data':text})+'\n').encode())
        live.close()
        events = [json.loads(line) for line in (self.run/'live.jsonl').read_text().splitlines()]
        self.assertTrue(all(e['truncated'] for e in events if e['kind'] in {'message','output'}))

    def test_long_needs_input_result_is_saved_in_full_and_does_not_start_worker(self):
        brief = {**BRIEF, 'status':'needs_input', 'summary':'验'*9000+'END_OF_SUMMARY',
                 'blockers':['Human must confirm the standard'], 'constraints':['Keep the complete scope']}
        text = json.dumps(brief, ensure_ascii=False)
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [{'verdict':{}, 'text':text}])
        result = Supervisor(RunConfig('task', self.root, worker, reviewer=reviewer,
            workflow='review-first', run_dir=self.run, git_history='off'), log=lambda _:None).run()
        self.assertEqual(result.status, 'action_required')
        self.assertEqual(worker.calls, [])
        self.assertEqual(json.loads((self.run/'000-reviewer-text.md').read_text()), brief)
        state = json.loads((self.run/'state.json').read_text())
        self.assertEqual(state['requirements_review']['status'], 'needs_input')
        self.assertFalse((self.run/'requirements-workspace-change.json').exists())
        activity = ActivityReader().snapshot(self.run, state, {}, result.status, 'test')
        message = next(e for e in activity['events'] if e.get('truncated'))
        self.assertEqual(message['text_file'], '000-reviewer-text.md')
        self.assertEqual(message['text_length'], len(text))

    def test_external_changes_list_additions_deletions_content_and_permissions(self):
        (self.root/'content.py').write_text('before')
        (self.root/'mode.py').write_text('same bytes')
        (self.root/'mode.py').chmod(0o600)
        (self.root/'removed.py').write_text('remove me')
        worker = ScriptedProvider('worker', [])
        reviewer = ScriptedProvider('reviewer', [{'verdict':{}, 'text':json.dumps(BRIEF)}])
        def external_writer(message):
            if message.startswith('requirements:'):
                (self.root/'content.py').write_text('after!')
                (self.root/'mode.py').chmod(0o700)
                (self.root/'removed.py').unlink()
                (self.root/'added.py').write_text('external work')
        result = Supervisor(RunConfig('task', self.root, worker, reviewer=reviewer,
            workflow='review-first', run_dir=self.run, git_history='off'), log=external_writer).run()
        self.assertEqual(result.status, 'action_required')
        self.assertEqual(worker.calls, [])
        self.assertIn('Change source is unknown', result.summary)
        self.assertNotIn('reviewer changed', result.summary)
        state = json.loads((self.run/'state.json').read_text())
        brief = state['requirements_review']
        self.assertEqual(brief['status'], 'failed')
        self.assertFalse((self.run/'reviewed-requirement.json').exists())
        changes = json.loads((self.run/'requirements-workspace-change.json').read_text())
        self.assertEqual(changes['source'], 'unknown')
        self.assertFalse(changes['accepted'])
        self.assertNotEqual(changes['before'], changes['after'])
        self.assertEqual({e['file']:e['change'] for e in changes['files']},
                         {'added.py':'added','content.py':'modified','mode.py':'modified','removed.py':'removed'})
        self.assertEqual(json.loads((self.run/brief['output_file']).read_text()), BRIEF)
        self.assertEqual((self.root/'content.py').read_text(), 'after!')
        failure = diagnose('action_required',state,{}, {},{},self.run)
        self.assertEqual(failure['code'], 'requirement_workspace_changed')
        self.assertEqual(failure['workspace_changes']['count'],4)
        self.assertEqual(failure['review_output_file'],'000-reviewer-text.md')

    def test_unchanged_dirty_workspace_can_proceed_and_short_requirement_output_stays_visible(self):
        (self.root/'existing.py').write_text('pre-existing work')
        worker = ScriptedProvider('worker',[{'verdict':_verdict('continue',gaps=['more work'])}])
        reviewer = ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)}])
        result = Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            run_dir=self.run,git_history='off',max_rounds=1),log=lambda _:None).run()
        self.assertEqual(result.status,'max_rounds')
        self.assertEqual(len(worker.calls),1)
        self.assertEqual((self.root/'existing.py').read_text(),'pre-existing work')
        event = {'kind':'message','text':json.dumps({**BRIEF,'status':'needs_input'})}
        work = work_details({}, {}, {'events':[event]}, 'action_required', {}, {}, 'test','test')
        self.assertEqual(work['workflow'],[event])

    def test_change_report_keeps_every_file_beyond_the_inline_preview(self):
        worker = ScriptedProvider('worker',[])
        reviewer = ScriptedProvider('reviewer',[{'verdict':{},'text':json.dumps(BRIEF)}])
        def external_writer(message):
            if message.startswith('requirements:'):
                for number in range(55):
                    (self.root/f'external-{number:02d}.py').write_text('external work')
        result = Supervisor(RunConfig('task',self.root,worker,reviewer=reviewer,workflow='review-first',
            run_dir=self.run,git_history='off'),log=external_writer).run()
        self.assertEqual(result.status,'action_required')
        state = json.loads((self.run/'state.json').read_text())
        changes = state['requirements_review']['workspace_changes']
        self.assertEqual(changes['count'],55)
        self.assertEqual(len(changes['files']),50)
        report = json.loads((self.run/changes['report_file']).read_text())
        self.assertEqual(len(report['files']),55)
        self.assertEqual(report['files'][-1],{'file':'external-54.py','change':'added'})
        self.assertEqual(worker.calls,[])

    def test_legacy_previews_and_failures_get_full_output_links_without_rewriting_history(self):
        self.run.mkdir()
        entry = {'at':'2026-11-12T06:12:59Z','kind':'message','role':'reviewer','round':0,'text':'x'*4000}
        write_json(self.run/'live.jsonl', entry)
        (self.run/'000-reviewer-text.md').write_text('complete rejected review')
        before = (self.run/'live.jsonl').read_bytes()
        result = ActivityReader().snapshot(self.run, {}, {}, 'action_required','test')
        self.assertTrue(result['events'][0]['preview_only'])
        self.assertEqual(result['events'][0]['text_file'],'000-reviewer-text.md')
        failure = diagnose('action_required', {'summary':'Requirement reviewer changed the read-only workspace',
            'last_invocation':{'role':'reviewer','round':0}}, {}, {}, {}, self.run)
        self.assertEqual(failure['code'],'requirement_workspace_changed')
        self.assertEqual(failure['review_output_file'],'000-reviewer-text.md')
        self.assertNotIn('workspace_changes',failure)
        self.assertEqual((self.run/'live.jsonl').read_bytes(),before)
        (self.run/'000-reviewer-text.md').unlink()
        missing = ActivityReader().snapshot(self.run, {}, {}, 'action_required','test')
        self.assertNotIn('text_file',missing['events'][0])


class FullLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root/'data',self.root)
        self.task = self.store.create({'title':'review','requirement':'task'})
        self.run = Path(self.task['run_dir'])
        self.run.mkdir()

    def test_complete_single_line_http_response_remains_plain_text_and_redacted(self):
        text = '结论'*12000+' END password=PRIVATE_CREDENTIAL'
        (self.run/'000-reviewer-text.md').write_text(text)
        with ConsoleServer(('127.0.0.1',0),self.store) as server:
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            connection = HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            try:
                connection.request('GET',f"/api/tasks/{self.task['id']}/logs?file=000-reviewer-text.md&full=1")
                response = connection.getresponse()
                body = response.read().decode()
                self.assertEqual(response.status,200)
                self.assertEqual(response.getheader('Content-Type'),'text/plain; charset=utf-8')
                self.assertEqual(response.getheader('X-Content-Type-Options'),'nosniff')
                self.assertEqual(body,'结论'*12000+' END password=[REDACTED]')
            finally:
                connection.close()
                server.shutdown()
                thread.join()

    def test_full_log_selection_rejects_missing_unsafe_and_oversized_files(self):
        (self.run/'console.log').write_text('public')
        (self.run/'000-reviewer-text.md').symlink_to(self.run/'console.log')
        (self.run/'requirements-workspace-change.json').write_text('{"source":"unknown"}')
        self.assertEqual(self.store.log_text(self.task['id'],'requirements-workspace-change.json'),'{"source":"unknown"}')
        for name in ('','../console.log','state.json','000-reviewer-text.md','999-worker-text.md'):
            with self.subTest(name=name),self.assertRaises(TaskError):
                self.store.log_text(self.task['id'],name)
        with (self.run/'console.log').open('wb') as stream:
            stream.truncate(8*1024*1024+1)
        with self.assertRaises(TaskError) as error:
            self.store.log_text(self.task['id'],'console.log')
        self.assertEqual(error.exception.status,413)
