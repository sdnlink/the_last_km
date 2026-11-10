import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from auto_moniter.agent_settings import AGENTS, read_settings
from auto_moniter.providers import ProviderError, resolve_provider
from auto_moniter.redaction import redact
from auto_moniter.supervisor import run_command
from console.agents import AgentManager
from console.server import ConsoleServer
from console.tasks import TaskStore


class AgentSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'project';self.project.mkdir()
        self.data=self.root/'data';self.data.mkdir()
        self.manager=AgentManager(self.data,self.project)

    def binary(self,name):
        path=self.root/name
        path.write_text(f'''#!{sys.executable}
import json,os,sys
if '--version' in sys.argv:
 print('fixture 1.0')
else:
 print(json.dumps({{'argv':sys.argv[1:],'token':os.environ.get('ANTHROPIC_AUTH_TOKEN'),
 'api_key':os.environ.get('ANTHROPIC_API_KEY'),'base_url':os.environ.get('ANTHROPIC_BASE_URL'),
 'openai_key':os.environ.get('OPENAI_API_KEY'),'kimi_model':os.environ.get('KIMI_MODEL_NAME'),
 'kimi_key':os.environ.get('KIMI_MODEL_API_KEY'),'kimi_type':os.environ.get('KIMI_MODEL_PROVIDER_TYPE'),
 'kimi_url':os.environ.get('KIMI_MODEL_BASE_URL')}}))
''')
        path.chmod(0o700);return path

    def test_catalog_initialization_persists_and_never_returns_api_keys(self):
        snapshot=self.manager.snapshot()
        self.assertFalse(snapshot['initialized'])
        expected={'codex','grok','claude','antigravity','gemini','copilot','hermes','kimi','codebuddy','glm','deepseek'}
        self.assertTrue(expected.issubset({agent['id'] for agent in snapshot['agents']}))
        secret='private-test-key-123456'
        result=self.manager.save({'complete':True,'agents':[{'id':'codex','enabled':True,'model':'configured-model','auth_mode':'api_key','api_key':secret}]})
        self.assertTrue(result['initialized']);self.assertNotIn(secret,json.dumps(result))
        self.assertEqual(stat.S_IMODE(self.manager.path.stat().st_mode),0o600)
        self.assertEqual(read_settings(self.manager.path)['agents']['codex']['api_key'],secret)
        self.assertTrue(AgentManager(self.data,self.project).initialized())
        self.manager.save({'agents':[{'id':'codex','api_key':''}]})
        self.assertEqual(read_settings(self.manager.path)['agents']['codex']['api_key'],secret)
        self.manager.save({'agents':[{'id':'codex','clear_api_key':True}]})
        self.assertNotIn('api_key',read_settings(self.manager.path)['agents']['codex'])

    def test_invalid_fields_and_endpoints_are_rejected_atomically(self):
        bad=[{'id':'unknown'}, {'id':'codex','enabled':'true'}, {'id':'codex','executable':'relative'},
             {'id':'codex','endpoint':'file:///etc/passwd'}, {'id':'codex','endpoint':'https://user:secret@example.com'},
             {'id':'codex','endpoint':'https://example.com?api_key=secret'}, {'id':'codex','model':'bad\nmodel'},
             {'id':'codex','command':'arbitrary shell'}, {'id':'copilot','endpoint':'https://example.com'},
             {'id':'kimi','auth_mode':'api_key','api_key':'key-123456789'},
             {'id':'codex','provider_type':'openai'},{'id':'kimi','provider_type':'unknown'}]
        for row in bad:
            with self.subTest(row=row),self.assertRaises(ValueError):self.manager.save({'agents':[row]})
        self.assertFalse(self.manager.path.exists())

    def test_scoped_keys_endpoints_and_models_reach_the_actual_cli(self):
        binary=self.binary('claude')
        self.manager.save({'agents':[
            {'id':'claude','enabled':True,'executable':str(binary),'model':'worker-model','endpoint':'https://worker.example/anthropic','auth_mode':'api_key','api_key':'worker-private-key'},
            {'id':'deepseek','enabled':True,'executable':str(binary),'model':'review-model','auth_mode':'api_key','api_key':'review-private-key'}]})
        prompt=self.root/'prompt.md';prompt.write_text('do work');schema=self.root/'schema.json';schema.write_text('{}')
        with patch.dict(os.environ,{'ANTHROPIC_AUTH_TOKEN':'parent-token-unchanged'}):
            worker=resolve_provider('claude',settings_path=self.manager.path)
            reviewer=resolve_provider('deepseek',role='reviewer',settings_path=self.manager.path)
            prepared=worker.start_command(prompt,self.project,schema)
            self.assertNotIn('worker-private-key',str(prepared.argv))
            result=json.loads(run_command(prepared,self.project,timeout_s=5).stdout)
            self.assertEqual(result['token'],'worker-private-key');self.assertEqual(result['base_url'],'https://worker.example/anthropic')
            self.assertIn('worker-model',result['argv'])
            reviewed=json.loads(run_command(reviewer.start_command(prompt,self.project,schema,read_only=True),self.project,timeout_s=5).stdout)
            self.assertIsNone(reviewed['token']);self.assertEqual(reviewed['api_key'],'review-private-key')
            self.assertEqual(reviewed['base_url'],'https://api.deepseek.com/anthropic')
            self.assertIn('plan',reviewed['argv']);self.assertIn('review-model',reviewed['argv'])
            self.assertEqual(os.environ['ANTHROPIC_AUTH_TOKEN'],'parent-token-unchanged')
            self.assertNotIn('review-private-key',redact(json.dumps(reviewed)))

    def test_codex_custom_endpoint_keeps_read_only_flags_and_key_out_of_argv(self):
        binary=self.binary('codex')
        self.manager.save({'agents':[{'id':'codex','enabled':True,'executable':str(binary),'endpoint':'https://codex.example/v1','auth_mode':'api_key','api_key':'codex-private-key'}]})
        prompt=self.root/'p.md';prompt.write_text('review');schema=self.root/'s.json';schema.write_text('{}')
        provider=resolve_provider('codex',role='reviewer',settings_path=self.manager.path)
        prepared=provider.start_command(prompt,self.project,schema,read_only=True)
        result=json.loads(run_command(prepared,self.project,timeout_s=5).stdout)
        self.assertEqual(result['openai_key'],'codex-private-key');self.assertIn('read-only',result['argv'])
        self.assertIn('model_providers.console.base_url="https://codex.example/v1"',result['argv'])
        self.assertNotIn('codex-private-key',str(result['argv']))

    def test_new_worker_adapters_cannot_be_used_as_unrestricted_reviewers(self):
        for name in ['copilot','hermes','kimi']:
            with self.subTest(name=name),self.assertRaises(ProviderError):resolve_provider(name,role='reviewer')
            self.assertEqual(resolve_provider(name).name,name)
        with self.assertRaises(ProviderError):resolve_provider('glm',settings_path=self.manager.path)
        self.assertEqual(resolve_provider('codebuddy',role='reviewer').name,'codebuddy')

    def test_kimi_ephemeral_api_configuration_uses_the_task_model_without_saved_aliases(self):
        binary=self.binary('kimi')
        self.manager.save({'agents':[{'id':'kimi','enabled':True,'executable':str(binary),
            'model':'saved-model','auth_mode':'api_key','api_key':'kimi-private-key',
            'provider_type':'openai','endpoint':'https://kimi.example/v1'}]})
        prompt=self.root/'p.md';prompt.write_text('work');schema=self.root/'s.json';schema.write_text('{}')
        provider=resolve_provider('kimi',settings_path=self.manager.path)
        prepared=provider.start_command(prompt,self.project,schema,model='task-model')
        result=json.loads(run_command(prepared,self.project,timeout_s=5).stdout)
        self.assertEqual(result['kimi_model'],'task-model')
        self.assertEqual(result['kimi_key'],'kimi-private-key')
        self.assertEqual(result['kimi_type'],'openai')
        self.assertEqual(result['kimi_url'],'https://kimi.example/v1')
        self.assertNotIn('--model',result['argv'])
        self.assertEqual(read_settings(self.manager.path)['agents']['kimi']['model'],'saved-model')
        self.assertNotIn('kimi-private-key',str(prepared.argv))

    def test_private_install_is_detected_without_a_custom_executable_path(self):
        target=self.manager.tools/'npm/bin/copilot';target.parent.mkdir(parents=True)
        target.symlink_to(self.binary('copilot'))
        self.manager.save({'agents':[{'id':'copilot','enabled':True,'executable':''}]})
        self.assertTrue(self.manager.providers({})['copilot'])
        provider=resolve_provider('copilot',settings_path=self.manager.path)
        prompt=self.root/'p.md';prompt.write_text('work');schema=self.root/'s.json';schema.write_text('{}')
        prepared=provider.start_command(prompt,self.project,schema)
        result=run_command(prepared,self.project,timeout_s=5)
        self.assertEqual(result.exit_code,0,result.stderr)
        self.assertTrue(next(row for row in self.manager.snapshot()['agents'] if row['id']=='copilot')['installed'])

    def test_user_install_outside_path_is_available_for_task_launch(self):
        target=self.root/'.local/bin/gemini';target.parent.mkdir(parents=True)
        target.symlink_to(self.binary('gemini'))
        from auto_moniter.providers import discover_providers
        with patch.dict(os.environ,{'PATH':''}),patch('auto_moniter.agent_settings.Path.home',return_value=self.root):
            available=self.manager.providers(discover_providers())
            self.assertTrue(available['gemini'])
            self.assertTrue(next(row for row in self.manager.snapshot()['agents'] if row['id']=='gemini')['installed'])

    def test_installer_runs_fixed_package_into_private_prefix_and_deduplicates(self):
        npm=self.root/'npm';record=self.root/'npm-args.json'
        npm.write_text(f'''#!{sys.executable}
import json,sys,time
from pathlib import Path
Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))
time.sleep(.2)
prefix=Path(sys.argv[sys.argv.index('--prefix')+1]);target=prefix/'bin/copilot';target.parent.mkdir(parents=True,exist_ok=True)
target.write_text('#!/bin/sh\\necho fixture-installed\\n');target.chmod(0o700)
print('fixture installation completed')
''');npm.chmod(0o700)
        from auto_moniter.agent_settings import executable as real_executable
        def local_executable(name,profile,tools=None):
            candidate=Path(profile['executable']) if profile.get('executable') else (tools/'npm/bin/copilot' if tools else self.root/'missing')
            return str(candidate) if candidate.is_file() else ''
        with patch('console.agents.shutil.which',return_value=str(npm)),patch('console.agents.executable',side_effect=local_executable):
            first=self.manager.install('copilot');second=self.manager.install('copilot')
            self.assertEqual(first['id'],second['id'])
            deadline=time.monotonic()+8
            while self.manager.jobs[first['id']]['status'] in ('queued','running') and time.monotonic()<deadline:time.sleep(.02)
            job=self.manager.jobs[first['id']]
            self.assertEqual(job['status'],'succeeded',job)
        argv=json.loads(record.read_text());self.assertEqual(argv[-1],'@github/copilot')
        self.assertEqual(argv[argv.index('--prefix')+1],str(self.manager.tools/'npm'))
        self.assertTrue(read_settings(self.manager.path)['agents']['copilot']['enabled'])
        self.assertTrue(real_executable('copilot',read_settings(self.manager.path)['agents']['copilot']))
        with self.assertRaises(ValueError):self.manager.install('unknown')
        with self.assertRaises(ValueError):self.manager.install('cursor')

    def test_probe_checks_version_without_running_a_model(self):
        binary=self.binary('codex')
        self.manager.save({'agents':[{'id':'codex','enabled':True,'executable':str(binary)}]})
        result=self.manager.probe('codex')
        self.assertTrue(result['installed']);self.assertEqual(result['message'].strip(),'fixture 1.0')

    def test_saved_choices_and_models_integrate_with_task_creation(self):
        store=TaskStore(self.data,self.project)
        store.agents.save({'agents':[{'id':'codebuddy','enabled':True,'executable':str(self.binary('codebuddy')),'model':'configured-codebuddy'}]})
        config=store.config();row=next(p for p in config['providers'] if p['name']=='codebuddy')
        self.assertTrue(row['available']);self.assertTrue(row['enabled'])
        self.assertEqual(config['agent_catalog']['models']['codebuddy']['models'][0]['id'],'configured-codebuddy')
        task=store.create({'title':'Configured task','requirement':'do work','provider':'codebuddy','reviewer':'codex'})
        self.assertEqual(task['model'],'configured-codebuddy')

    def test_http_setup_page_first_run_routing_csrf_and_secret_boundaries(self):
        store=TaskStore(self.data,self.project)
        with ConsoleServer(('127.0.0.1',0),store) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(method,path,data=None,token=''):
                conn=HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
                headers={'Content-Type':'application/json'}
                if token:headers['X-Console-Token']=token
                conn.request(method,path,json.dumps(data) if data is not None else None,headers)
                response=conn.getresponse();result=response.status,response.read().decode();conn.close();return result
            try:
                self.assertIn('data-needs-setup="true"',request('GET','/overview')[1])
                self.assertIn('id="setup-agents"',request('GET','/setup')[1])
                self.assertEqual(request('GET','/pages/setup.js')[0],200)
                self.assertEqual(request('POST','/api/agents/save',{'agents':[]})[0],403)
                token=json.loads(request('GET','/api/config')[1])['csrf_token']
                secret='http-secret-key-12345'
                payload={'complete':True,'agents':[{'id':'codex','enabled':True,'auth_mode':'api_key','api_key':secret}]}
                self.assertEqual(request('POST','/api/agents/save',payload,token)[0],200)
                for endpoint in ['/api/config','/api/agents/setup','/setup']:
                    self.assertNotIn(secret,request('GET',endpoint)[1])
                self.assertIn('data-needs-setup="false"',request('GET','/overview')[1])
                self.assertEqual(request('POST','/api/agents/install',{'id':'unknown'},token)[0],400)
            finally:server.shutdown();thread.join(3)
