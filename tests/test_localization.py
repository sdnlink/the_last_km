import copy
import json
import re
import threading
import unittest
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

from console.localization import localize_response, messages, select_language, translate, translate_html
from console.pages import DETAIL_SECTIONS, PAGES, STATIC, render_page
from console.server import ConsoleServer


class TextParser(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.text = []
        self.attributes = []
        self.feed(source)

    def handle_data(self, data):
        self.text.append(data.strip())

    def handle_starttag(self, tag, attrs):
        self.attributes.extend(value for key, value in attrs if key in {'title', 'placeholder', 'aria-label', 'alt', 'label'})


class LocalizationTests(unittest.TestCase):
    def test_language_preference_and_weighted_browser_languages(self):
        cases = [('', '', 'zh-CN'), ('', 'en-US,en;q=0.9', 'en'),
                 ('', 'en;q=0.2,zh-CN; q=0.9', 'zh-CN'),
                 ('', 'zh;q=0,en;q=1', 'en'), ('', 'fr-FR', 'en'),
                 ('auto_moniter_lang=en', 'zh-CN', 'en'),
                 ('auto_moniter_lang=zh-CN', 'en-US', 'zh-CN'),
                 ('auto_moniter_lang=invalid', 'en-US', 'en'),
                 ('', 'en;q=invalid,zh;q=0.8', 'zh-CN')]
        for cookie, browser, expected in cases:
            with self.subTest(cookie=cookie, browser=browser):
                self.assertEqual(select_language(cookie, browser), expected)

    def test_catalog_placeholders_preserve_all_values(self):
        for source, target in messages().items():
            self.assertIsInstance(target, str)
            self.assertEqual(set(re.findall(r'\{\d+\}', source)), set(re.findall(r'\{\d+\}', target)), source)

    def test_static_pages_and_lazy_dialogs_have_no_untranslated_copy(self):
        workspace = Path('/tmp/保留项目原文')
        documents = [render_page(page, 'a'*32, 'home', workspace, language='en').decode() for page in PAGES]
        documents += [render_page('detail', 'a'*32, section, workspace, language='en').decode() for section in DETAIL_SECTIONS]
        documents += [translate_html(path.read_text(), 'en') for path in (STATIC/'fragments').glob('*.html')]
        for document in documents:
            parsed = TextParser(document)
            for text in parsed.text + parsed.attributes:
                if text == '简体中文':
                    continue  # Languages remain identifiable in their own language.
                self.assertIsNone(re.search(r'[\u3400-\u9fff]', text), text)
        self.assertIn('data-workspace="/tmp/保留项目原文"', documents[0])
        self.assertIn('<html lang="en">', documents[0])
        self.assertIn('<title>Overview · Auto Moniter Console</title>', documents[0])

    def test_console_messages_translate_without_changing_user_or_raw_content(self):
        original = {'title':'执行失败', 'requirement':'执行失败', 'summary':'执行失败',
                    'control_message':'正在停止执行并保存检查点…',
                    'failure':{'title':'任务未完成', 'raw_summary':'执行失败', 'reason':'任务已经停止，尚未通过完整交付验收。'},
                    'work':{'roles':{'worker':{'current_work':'执行失败', 'current_work_generated':False},
                                    'reviewer':{'wait_reason_parts':['正在独立审核第 3 轮工作及项目改动。']}},
                            'conclusions':[{'summary':'执行失败'}],
                            'workflow':[{'kind':'message','text':'执行失败'},
                                        {'kind':'phase','text':'函数通过验收：F1','console_event':'function_accepted'}]}}
        snapshot = copy.deepcopy(original)
        result = localize_response(original, 'en', '/api/tasks/abc')
        self.assertEqual(original, snapshot)
        for key in ('title','requirement','summary'):
            self.assertEqual(result[key], '执行失败')
        self.assertEqual(result['failure']['raw_summary'], '执行失败')
        self.assertEqual(result['work']['roles']['worker']['current_work'], '执行失败')
        self.assertEqual(result['work']['workflow'][0]['text'], '执行失败')
        self.assertEqual(result['work']['workflow'][1]['display_text'], 'Function accepted: F1')
        self.assertIn('round 3', result['work']['roles']['reviewer']['wait_reason'])
        self.assertEqual(result['failure']['title'], 'Task incomplete')
        logs = {'content':'执行失败', 'lines':[{'text':'任务未完成'}]}
        self.assertEqual(localize_response(logs,'en','/api/tasks/abc/logs'), logs)
        raw = {'failure':{'title':'任务未完成','reason':'执行失败','raw_summary':'执行失败'}}
        self.assertEqual(localize_response(raw,'en','/api/tasks/abc')['failure']['reason'],'执行失败')

    def test_backend_templates_and_owned_check_descriptions(self):
        self.assertEqual(translate('未找到 codex 可执行文件，请先安装并完成登录','en'),
                         'codex executable not found. Install it and log in first.')
        self.assertEqual(translate('auto_start 必须为布尔值','en'),'auto_start must be a boolean.')
        result = localize_response({'checks':[{'title':'改动格式检查','purpose':'检查冲突标记及空白错误；不验证业务功能',
                                             'command':'echo 执行失败'}]},'en','/api/acceptance/scan')
        self.assertEqual(result['checks'][0]['title'],'Diff formatting check')
        self.assertEqual(result['checks'][0]['command'],'echo 执行失败')

    def test_http_locale_applies_to_pages_fragments_and_errors_but_not_logs(self):
        store = SimpleNamespace(cwd=Path('/tmp/项目'), agents=SimpleNamespace(initialized=lambda:True),
                                log_text=lambda *_:'执行失败\n原始日志',
                                detail=lambda *_:{'requirement':'任务未完成','error':'任务不存在或运行目录已不可用'})
        with ConsoleServer(('127.0.0.1',0),store) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get(path, cookie='auto_moniter_lang=en'):
                conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                conn.request('GET',path,headers={'Cookie':cookie,'Accept-Language':'zh-CN'})
                response=conn.getresponse();body=response.read().decode();conn.close()
                return response.status,body
            try:
                self.assertIn('<html lang="en">',get('/tasks')[1])
                self.assertIn('<html lang="zh-CN">',get('/tasks','auto_moniter_lang=zh-CN')[1])
                self.assertIn('New development task',get('/fragments/create.html')[1])
                self.assertEqual(json.loads(get('/missing')[1])['error'],'Page not found')
                self.assertEqual(get('/api/tasks/abc/logs?file=a&full=1')[1],'执行失败\n原始日志')
                self.assertEqual(json.loads(get('/api/tasks/abc')[1])['requirement'],'任务未完成')
                self.assertEqual(get('/i18n.js')[0],200)
                self.assertEqual(get('/translations.js')[0],200)
            finally:
                server.shutdown();thread.join(3)
