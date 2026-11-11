import json
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from auto_moniter.storage import write_json
from console.server import ConsoleServer
from console.tasks import TaskStore


class IdParser(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.ids.update(value for name, value in attrs if name == 'id')


class ConsolePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.store = TaskStore(self.root / 'console', self.project)
        self.task = self.store.create({'title': 'Scoped task', 'cwd': str(self.project),
                                      'requirement': 'PRIVATE_REQUIREMENT_CONTENT', 'auto_start': False})
        self.run = Path(self.task['run_dir'])
        self.run.mkdir()
        self.record = {'index': 1, 'role': 'worker', 'status': 'continue', 'summary': 'worker conclusion',
                       'evidence': ['app.py'], 'gaps': ['finish tests']}
        write_json(self.run / 'state.json', {'cwd': str(self.project), 'status': 'stopped',
                   'last_worker': self.record, 'rounds': [self.record], 'next_round': 2,
                   'git_history': {'enabled': True, 'ref': 'PRIVATE_GIT_HISTORY'}})
        write_json(self.run / 'report.json', {'status': 'stopped', 'rounds': 1, 'summary': 'Stopped',
                                            'stop_reason': 'stopped', 'records': [self.record]})
        write_json(self.run / 'directions.json', {'messages': [{'id': 'instruction', 'text': 'PRIVATE_DIRECTION_CONTENT'}]})
        (self.run / '001-worker.log').write_text('PRIVATE_RAW_LOG_CONTENT')
        self.server = ConsoleServer(('127.0.0.1', 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        connection.request('GET', path)
        response = connection.getresponse()
        result = response.status, response.read().decode()
        connection.close()
        return result

    def test_each_document_contains_only_requested_page_and_no_operation_forms(self):
        expected = {'/overview': 'stats', '/tasks': 'task-rows', '/resources': 'cpu-chart',
                    '/progress': 'progress-context', '/tasks/' + self.task['id']: 'role-details'}
        for path, marker in expected.items():
            with self.subTest(path=path):
                status, text = self.get(path)
                self.assertEqual(status, 200)
                ids = IdParser(text).ids
                self.assertIn(marker, ids)
                self.assertFalse((set(expected.values()) - {marker}) & ids)
                self.assertFalse({'create-form', 'direction-form', 'log-output'} & ids if marker != 'role-details'
                                 else {'create-form', 'direction-form'} & ids)
                self.assertNotIn('PRIVATE_REQUIREMENT_CONTENT', text)
                self.assertNotIn('__PAGE__', text)

    def test_sidebar_groups_match_workflow_and_task_context_routes(self):
        from console.pages import render_page
        home=render_page('overview','','',self.project).decode()
        for label in ['任务管理','执行监督','系统配置','待启动','需处理','已验收','实时协作','资源与进程','Agent、模型与认证']:
            self.assertIn(label,home)
        self.assertNotIn('>当前任务<',home)
        self.assertIn('href="/tasks?status=attention"',home)
        self.assertIn('data-create-task',home)
        for section in ['home','requirements','conclusions','git','logs']:
            body=render_page('detail',self.task['id'],section,self.project).decode()
            self.assertIn('>当前任务<',body)
            for suffix in ['', '/requirements','/conclusions','/git','/logs']:
                self.assertIn(f'href="/tasks/{self.task["id"]}{suffix}"',body)

    def test_summary_and_resource_apis_never_read_transcripts_or_send_task_bodies(self):
        with patch.object(self.store.activity, 'snapshot', side_effect=AssertionError('Unexpected transcript read')):
            for path in ('/api/tasks', '/api/overview', '/api/resources'):
                with self.subTest(path=path):
                    status, text = self.get(path)
                    self.assertEqual(status, 200)
                    for secret in ('PRIVATE_REQUIREMENT_CONTENT', 'PRIVATE_DIRECTION_CONTENT',
                                   'PRIVATE_GIT_HISTORY', 'PRIVATE_RAW_LOG_CONTENT'):
                        self.assertNotIn(secret, text)
                    for task in json.loads(text)['tasks']:
                        self.assertNotIn('activity', task)
                        self.assertNotIn('work', task)
                        self.assertNotIn('directions', task)

    def test_detail_menus_have_direct_routes_and_only_render_selected_content(self):
        markers = {'conclusions': 'detail-conclusions', 'git': 'detail-git',
                   'requirements': 'detail-body', 'logs': 'log-output'}
        titles = {'conclusions': '交互结论', 'git': 'Git 追溯',
                  'requirements': '需求与验收信息', 'logs': '执行日志 · 原始输出'}
        status, home = self.get(f'/tasks/{self.task["id"]}')
        self.assertEqual(status, 200)
        self.assertIn('detail-menu', IdParser(home).ids)
        self.assertFalse(set(markers.values()) & IdParser(home).ids)
        self.assertIn('data-section="home"', home)
        for section in markers:
            self.assertIn(f'href="/tasks/{self.task["id"]}/{section}"', home)
        for section, marker in markers.items():
            with self.subTest(section=section):
                status, body = self.get(f'/tasks/{self.task["id"]}/{section}')
                self.assertEqual(status, 200)
                ids = IdParser(body).ids
                self.assertIn(marker, ids)
                self.assertFalse((set(markers.values()) - {marker}) & ids)
                self.assertIn(f'data-section="{section}"', body)
                self.assertIn(f'<title>{titles[section]} · Auto Moniter 开发控制台</title>', body)
                self.assertIn(f'href="/tasks/{self.task["id"]}">← 返回任务详情</a>', body)
                self.assertFalse({'detail-menu', 'role-details', 'workflow-stages', 'detail-live'} & ids)
                self.assertNotIn('PRIVATE_RAW_LOG_CONTENT', body)
                self.assertNotIn('<!-- DETAIL_', body)
        for suffix in ('missing', 'logs/extra', '../logs'):
            self.assertEqual(self.get(f'/tasks/{self.task["id"]}/{suffix}')[0], 404)
        status, body = self.get('/tasks/ext-' + 'a' * 20 + '/git')
        self.assertEqual(status, 200)
        self.assertIn('id="detail-git"', body)

    def test_progress_requests_only_selected_task_and_keeps_detail_and_logs_separate(self):
        with patch.object(self.store.activity, 'snapshot', return_value={'events': [], 'round': 1}) as reader:
            status, text = self.get('/api/tasks/' + self.task['id'] + '/progress')
        self.assertEqual(status, 200)
        self.assertEqual(reader.call_args.args[0], self.run)
        data = json.loads(text)
        self.assertEqual(data['id'], self.task['id'])
        self.assertEqual(data['work']['conclusions'][0]['summary'], 'worker conclusion')
        self.assertNotIn('requirement', data)
        self.assertNotIn('git_history', data)
        self.assertNotIn('PRIVATE_RAW_LOG_CONTENT', text)
        status, detail = self.get('/api/tasks/' + self.task['id'])
        self.assertEqual(status, 200)
        self.assertIn('PRIVATE_REQUIREMENT_CONTENT', detail)
        status, logs = self.get('/api/tasks/' + self.task['id'] + '/logs')
        self.assertEqual(status, 200)
        self.assertIn('PRIVATE_RAW_LOG_CONTENT', logs)

    def test_log_api_defaults_to_ten_lines_and_validates_pagination(self):
        (self.run / '001-worker.log').write_text(''.join(f'line-{n}\n' for n in range(25)))
        path = '/api/tasks/' + self.task['id'] + '/logs'
        status, text = self.get(path)
        data = json.loads(text)
        self.assertEqual(status, 200)
        self.assertEqual(data['page_size'], 10)
        self.assertEqual(len(data['lines']), 10)
        self.assertEqual(data['total_lines'], 25)
        self.assertEqual(json.loads(self.get(path+'?page=2')[1])['lines'][0]['text'], 'line-10')
        self.assertEqual(len(json.loads(self.get(path+'?page_size=20')[1])['lines']), 20)
        for query in ('page=-1', 'page=invalid', 'page_size=0', 'page_size=101', 'page_size=invalid'):
            self.assertEqual(self.get(path+'?'+query)[0], 400)

    def test_page_assets_and_dialog_fragments_are_separate_and_allowlisted(self):
        for path in ('/pages/overview.js', '/pages/progress.js', '/pages/work.css', '/fragments/create.html', '/fragments/git.html'):
            self.assertEqual(self.get(path)[0], 200)
        for path in ('/pages/../server.py', '/fragments/../../README.md', '/pages/detail.html',
                     '/pages/detail-logs.html', '/index.html'):
            self.assertEqual(self.get(path)[0], 404)
        _, bootstrap = self.get('/app.js')
        self.assertIn('import(', bootstrap)
        self.assertNotIn('function renderTasks', bootstrap)
        self.assertNotIn('setInterval', bootstrap)

    def test_git_record_is_requested_separately_for_the_selected_task(self):
        commit = 'a' * 40
        with patch.object(self.store, 'git_record', return_value={'commit': commit}) as reader:
            status, body = self.get('/api/tasks/' + self.task['id'] + '/git/' + commit)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['commit'], commit)
        reader.assert_called_once_with(self.task['id'], commit)
        _, detail = self.get('/tasks/' + self.task['id'])
        self.assertNotIn('git-record-dialog', IdParser(detail).ids)

