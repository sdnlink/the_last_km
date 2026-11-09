"""Discovery is bounded/read-only; role model choices reach the real CLI."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from auto_moniter.cli import build_parser
from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import Supervisor
from console.agent_catalog import discover, model_rows, text_models
from console.tasks import TaskStore


class AgentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cache(self, provider, value):
        path=self.root/('.'+provider)/'models_cache.json';path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))

    def test_cache_whitelist_never_exposes_credentials_or_endpoints(self):
        self.cache('grok', {'identity':'private', 'models':{
            'grok-custom':{'info':{'model':'private-model','name':'Custom', 'base_url':'PRIVATE_URL'},'api_key':'PRIVATE_KEY'},
            'hidden':{'info':{'id':'hidden','hidden':True}}, 'malformed':{'info':None}}})
        self.cache('codex', {'models':[{'slug':'sol','display_name':'Sol','access_token':'PRIVATE_TOKEN'}]})
        result=discover(self.root,{'grok':True,'codex':True},home=self.root)
        serialized=json.dumps(result)
        for secret in ('PRIVATE_URL','PRIVATE_KEY','PRIVATE_TOKEN','private-model','private'):self.assertNotIn(secret,serialized)
        self.assertEqual(result['models']['grok']['models'][0]['id'],'grok-custom')
        self.assertEqual(result['models']['codex']['models'][0]['id'],'sol')

    def test_malformed_and_missing_cache_does_not_break_config(self):
        self.cache('grok',[])
        self.cache('codex',{'models':'wrong'})
        result=discover(self.root,{'grok':True,'codex':True,'claude':False},home=self.root)
        self.assertEqual(result['models']['grok']['models'],[])
        self.assertEqual(result['models']['codex']['models'],[])

    def test_live_discovery_preserves_backend_availability_and_direct_model_ids(self):
        self.cache('codex',{'models':[{'slug':'cached-sol'}]})
        def query(argv,cwd):
            if argv[1:]==['provider','ls','--json']:
                return json.dumps([{'provider':'claude','label':'Claude','status':'available'},
                    {'provider':'copilot','status':'unavailable'}])
            if argv[1:]==['provider','models','claude','--json']:
                return json.dumps([{'id':'opus','model':'Opus','api_key':'PRIVATE'}])
            if argv[0]=='/bin/grok':return 'Available models:\n * grok-4.7 (default)\n - custom-worker\n'
            raise AssertionError(argv)
        with patch('console.agent_catalog._which',side_effect=lambda name:'/bin/'+name),patch('console.agent_catalog.query',query):
            result=discover(self.root,{'paseo':True,'claude':True,'grok':True,'codex':True},home=self.root,live=True)
        self.assertEqual([m['id'] for m in result['models']['grok']['models']],['grok-4.7','custom-worker'])
        self.assertEqual(result['paseo_backends'][0]['models'][0]['id'],'opus')
        self.assertFalse(result['paseo_backends'][1]['available'])
        self.assertEqual(result['models']['claude']['models'][0]['id'],'opus')
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_timeout_keeps_cached_options_and_never_calls_an_agent(self):
        self.cache('grok',{'models':{'cached-grok':{'info':{'id':'cached-grok'}}}})
        calls=[]
        def timeout(argv,cwd):
            calls.append(argv);raise subprocess.TimeoutExpired(argv,4)
        with patch('console.agent_catalog._which',return_value='/bin/tool'),patch('console.agent_catalog.query',timeout):
            result=discover(self.root,{'paseo':True,'grok':True},home=self.root,live=True)
        self.assertEqual(result['models']['grok']['models'][0]['id'],'cached-grok')
        self.assertTrue(all('run' not in call and 'agent' not in call for call in calls))

    def test_separate_worker_and_reviewer_backends_reach_real_parser(self):
        store=TaskStore(self.root/'data',self.root)
        task=store.create({'title':'roles','requirement':'Implement', 'provider':'paseo','reviewer':'paseo',
            'paseo_backend':'grok','reviewer_paseo_backend':'claude','model':'grok-4.7',
            'reviewer_model':'opus','review_isolation':'provider'})
        args=build_parser().parse_args(store._new_args(store._get(task['id'])))
        self.assertEqual((args.paseo_backend,args.reviewer_paseo_backend),('grok','claude'))
        self.assertEqual((args.model,args.reviewer_model),('grok-4.7','opus'))
        self.assertEqual(task['reviewer_paseo_backend'],'claude')

    def test_review_only_boundary_is_not_weakened_by_model_discovery(self):
        store=TaskStore(self.root/'data',self.root)
        with self.assertRaisesRegex(ValueError,'Claude plan'):
            store.create({'title':'bad','requirement':'Implement','provider':'codex','reviewer':'paseo',
                'review_isolation':'provider','reviewer_paseo_backend':'grok'})

    def test_text_parsing_ignores_status_lines_and_empty_account(self):
        self.assertEqual(text_models('No models available for this account.','native_cli'),[])
        self.assertEqual(text_models('You are using XAI_API_KEY.\nAvailable models:\n - grok-4.7','native_cli',grok=True)[0]['id'],'grok-4.7')

    def test_different_paseo_backends_do_not_share_model_namespace(self):
        supervisor=object.__new__(Supervisor)
        supervisor.config=SimpleNamespace(provider=resolve_provider('paseo',paseo_backend='grok'),
            reviewer=resolve_provider('paseo',paseo_backend='claude',role='reviewer'),
            model='grok-4.7',reviewer_model='')
        self.assertEqual(supervisor._reviewer_model(),'')
        supervisor.config.provider=resolve_provider('paseo',paseo_backend='claude')
        supervisor.config.model='opus'
        self.assertEqual(supervisor._reviewer_model(),'opus')
        supervisor.config.reviewer_model='sonnet'
        self.assertEqual(supervisor._reviewer_model(),'sonnet')
