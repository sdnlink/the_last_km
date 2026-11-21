import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from html.parser import HTMLParser
from http.client import HTTPConnection
from io import StringIO
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

    def post(self, path, payload):
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        try:
            connection.request('POST', path, json.dumps(payload), {
                'Content-Type': 'application/json', 'X-Console-Token': self.server.csrf,
            })
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()

    def test_creating_task_configures_scoped_supervision_and_can_pause_it(self):
        schedule = {}
        preferences = {'cron': '*/30 * * * *', 'timezone': 'Asia/Shanghai', 'provider': 'codex'}

        def paseo(action, *args):
            if action == 'ls':
                return [dict(schedule)] if schedule else []
            if action == 'create':
                name = args[args.index('--name') + 1]
                self.assertTrue(name.startswith('Auto Monitor supervision'))
                self.assertIn('--task-id ' + name.rsplit(' ', 1)[-1], args[-1])
                self.assertIn('--console-port ' + str(self.server.server_port), args[-1])
                self.assertNotIn('Requirement for supervision test', args[-1])
                schedule.update(id='one-schedule', name=name, cadence='cron:*/30 * * * * (Asia/Shanghai)',
                                target='new-agent:codex', status='active', nextRunAt='2026-11-20T00:30:00Z')
            elif action == 'pause':
                self.assertEqual(args, ('one-schedule',))
                schedule.update(status='paused', nextRunAt=None)
            elif action == 'resume':
                self.assertEqual(args, ('one-schedule',))
                schedule.update(status='active', nextRunAt='2026-11-20T00:30:00Z')
            else:
                self.fail(f'Unexpected schedule action {action!r}')
            return dict(schedule)

        payload = {'title': 'Supervised', 'cwd': str(self.project), 'requirement': 'Requirement for supervision test',
                   'auto_start': False, 'supervision': preferences}
        with patch.object(self.server.supervision, '_call', side_effect=paseo):
            status, task = self.post('/api/tasks', payload)
            self.assertEqual(status, 201)
            self.assertEqual(task['status'], 'queued')
            self.assertEqual(task['supervision_preferences'], preferences)
            self.assertEqual(task['supervision']['status'], 'active')
            path = f"/api/tasks/{task['id']}/supervision"
            schedule['name'] = schedule['name'].replace('Auto Monitor', 'Auto Moniter', 1)
            status, paused = self.post(path, {'action': 'pause'})
            self.assertEqual((status, paused['status'], paused['next_run_at']), (200, 'paused', None))
            self.assertEqual(self.post(path, {'action': 'resume'})[1]['status'], 'active')
            global_supervision = json.loads(self.get('/api/supervision')[1])
            self.assertEqual(global_supervision['status'], 'unconfigured')
            self.assertIsNone(global_supervision['timezone'])
            _, snapshot = self.get(f"/api/tasks/{task['id']}/snapshot")
            self.assertEqual([row['id'] for row in json.loads(snapshot)['tasks']], [task['id']])
            self.assertNotIn(payload['requirement'], snapshot)

    def test_schedule_failure_does_not_undo_task_creation(self):
        from console.tasks import TaskError
        preferences = {'cron': '*/30 * * * *', 'timezone': 'UTC', 'provider': 'codex'}
        payload = {'title': 'Saved task', 'cwd': str(self.project), 'requirement': 'Task requirement',
                   'supervision': preferences}
        with patch.object(self.server.supervision, '_call', side_effect=TaskError('Paseo unavailable', 503)):
            status, task = self.post('/api/tasks', payload)
            self.assertEqual(status, 201)
            self.assertEqual(task['supervision_error'], 'Paseo unavailable')
        with patch.object(self.server.supervision, '_call', return_value=[]):
            _, body = self.get(f"/api/tasks/{task['id']}/supervision")
            self.assertEqual(json.loads(body), {'status': 'unconfigured', 'next_run_at': None, **preferences})

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

    def test_sidebar_contains_global_pages_and_task_tabs_own_local_navigation(self):
        from console.pages import render_page
        home=render_page('overview','','',self.project).decode()
        for label in ['工作概览','开发任务','实时协作','资源监控','Agent 管理']:
            self.assertIn(label,home)
        self.assertNotIn('>当前任务<',home)
        sidebar=home.split('<nav id="primary-navigation"',1)[1].split('</nav>',1)[0]
        self.assertNotIn('?status=',sidebar)
        self.assertNotIn('data-nav-key="detail:',sidebar)
        self.assertNotIn('id="new-task"',home)
        self.assertIn('data-create-task',home)
        for section in ['home','queue','requirements','conclusions','git','logs']:
            body=render_page('detail',self.task['id'],section,self.project).decode()
            sidebar=body.split('<nav id="primary-navigation"',1)[1].split('</nav>',1)[0]
            self.assertNotIn('/tasks/'+self.task['id'],sidebar)
            self.assertEqual(body.count('id="detail-menu"'),1)
            tabs=body.split('id="detail-menu"',1)[1].split('</nav>',1)[0]
            self.assertEqual(tabs.count('aria-current="page"'),1)
            for suffix in ['', '/queue', '/requirements','/conclusions','/git','/logs']:
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

    def test_watch_console_snapshot_reports_health_without_loading_task_details(self):
        from auto_moniter.cli import main

        (self.run / 'supervisor.lock').touch()
        output = StringIO()
        with redirect_stdout(output), patch.object(self.store.activity, 'snapshot',
                                                  side_effect=AssertionError('Unexpected transcript read')):
            self.assertEqual(main(['watch', '--console-port', str(self.server.server_port)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(len(result['tasks']), 1)
        task = result['tasks'][0]
        self.assertEqual((task['id'], task['status'], task['phase'], task['rounds']),
                         (self.task['id'], 'stopped', 'finished', 1))
        self.assertIn('agent_calls', task['usage'])
        self.assertNotIn('PRIVATE_REQUIREMENT_CONTENT', output.getvalue())
        self.assertNotIn('PRIVATE_RAW_LOG_CONTENT', output.getvalue())

    def test_detail_menus_have_direct_routes_and_only_render_selected_content(self):
        markers = {'queue':'detail-queue', 'conclusions': 'detail-conclusions', 'git': 'detail-git',
                   'requirements': 'detail-body', 'logs': 'log-output'}
        titles = {'queue':'需求队列','conclusions': '交互结论', 'git': 'Git 追溯',
                  'requirements': '需求与验收信息', 'logs': '执行日志 · 原始输出'}
        status, home = self.get(f'/tasks/{self.task["id"]}')
        self.assertEqual(status, 200)
        self.assertIn('detail-menu', IdParser(home).ids)
        self.assertIn('detail-usage', IdParser(home).ids)
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
                self.assertIn(f'<title>{titles[section]} · Auto Monitor 开发控制台</title>', body)
                self.assertIn('href="/tasks">← 返回开发任务</a>', body)
                self.assertIn('detail-menu',ids)
                self.assertFalse({'role-details', 'workflow-stages', 'detail-live'} & ids)
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

