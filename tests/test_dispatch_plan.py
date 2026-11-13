"""The entire approved dispatch plan is paged without inventing pending tasks."""
import copy
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from auto_moniter.storage import write_json
from console.dispatch import page
from console.server import ConsoleServer
from console.tasks import TaskStore
from tests.test_function_dispatch import task
from tests.test_review_first import BRIEF


def fixture(count=25):
    tasks = [task('F'+str(i), f'file_{i}.py', depends=['F'+str(i-1)] if i > 1 else (),
                  scope='requirement' if i == count else 'function') for i in range(1, count+1)]
    accepted = [{'task':value,'round':index,'tree':'a'*64,'evidence':['Evidence '+value['id']]}
                for index,value in enumerate(tasks[:5],1)]
    return {'function_dispatch':True,'full_function_plan':True,'status':'running',
            'function_plan':{'version':1,'cycle':1,'tasks':tasks},
            'function_tasks':{'current':tasks[5],'accepted':accepted},
            'requirements_review':{**BRIEF,'cycle':1},'modules':[{'id':'main','acceptance':['All original behavior and checks pass']}],
            'requirement_cycles':[]}


class DispatchPlanTests(unittest.TestCase):
    def test_full_plan_pagination_preserves_order_and_exact_contents(self):
        state = fixture()
        all_rows = []
        for index in range(1,4):
            result = page(state,index,10)
            self.assertEqual(result['total'],25)
            self.assertEqual(result['counts'],{'accepted':5,'active':1,'waiting':19,'all':25})
            self.assertTrue(result['complete'])
            self.assertEqual(result['page'],index)
            all_rows.extend(result['items'])
        self.assertEqual([row['task']['id'] for row in all_rows],['F'+str(i) for i in range(1,26)])
        self.assertEqual(all_rows[5]['status'],'active')
        self.assertEqual(all_rows[6]['task'],state['function_plan']['tasks'][6])
        self.assertEqual(all_rows[0]['criteria'][0]['text'],'All original behavior and checks pass')

    def test_status_filters_and_out_of_range_page_are_bounded(self):
        state = fixture()
        waiting = page(state,2,10,'waiting')
        self.assertEqual(waiting['total'],19)
        self.assertEqual(waiting['items'][0]['task']['id'],'F17')
        self.assertEqual(page(state,999,10,'active')['page'],1)
        self.assertEqual(page(state,1,10,'accepted')['total'],5)
        for arguments in [(0,10,'all'),(1,0,'all'),(1,101,'all'),(1,10,'unknown')]:
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):
                page(state,*arguments)

    def test_legacy_tasks_show_known_history_without_claiming_empty_backlog(self):
        state = fixture();state.pop('function_plan');state.pop('full_function_plan')
        result = page(state)
        self.assertFalse(result['complete'])
        self.assertEqual(result['plan_state'],'not_generated')
        self.assertEqual(result['total'],6)
        self.assertEqual(result['counts']['active'],1)

    def test_reviewing_plan_is_visible_without_claiming_dispatch_readiness(self):
        state = fixture();state['requirements_review']['status']='reviewing'
        result = page(state)
        self.assertEqual(result['plan_state'],'needs_review')
        self.assertEqual(len(result['items']),10)
        stale = copy.deepcopy(state);stale['function_plan']['cycle']=2
        self.assertFalse(page(stale)['complete'])
        self.assertEqual(page(stale)['counts']['waiting'],0)

    def test_http_pagination_is_read_only_and_scoped_to_selected_task(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUTO_MONITER_STATE_DIR':directory+'/state'}):
            root = Path(directory);store = TaskStore(root/'data',root)
            selected = store.create({'title':'Selected','requirement':'Task A'})
            other = store.create({'title':'Other','requirement':'Task B'})
            run = Path(selected['run_dir']);run.mkdir()
            write_json(run/'state.json',fixture())
            before = (run/'state.json').read_bytes()
            with ConsoleServer(('127.0.0.1',0),store) as server:
                thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    def get(ident,query=''):
                        conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                        try:
                            conn.request('GET',f'/api/tasks/{ident}/dispatch-plan'+query)
                            response=conn.getresponse();return response.status,json.loads(response.read())
                        finally:conn.close()
                    status,data=get(selected['id'],'?page=2&page_size=10')
                    self.assertEqual(status,200);self.assertEqual(data['items'][0]['task']['id'],'F11')
                    self.assertEqual(get(other['id'])[1]['total'],0)
                    for query in ['?page=-1','?page_size=101','?status=invalid','?page=abc']:
                        self.assertEqual(get(selected['id'],query)[0],400)
                    self.assertEqual((run/'state.json').read_bytes(),before)
                finally:server.shutdown();thread.join(5)


if __name__ == '__main__':
    unittest.main()
