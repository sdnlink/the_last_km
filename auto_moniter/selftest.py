"""Installed three-role smoke with real subprocesses and no model access."""
import json
import shlex
import sys
import tempfile
from pathlib import Path

from auto_moniter.providers import Provider, PreparedCommand
from auto_moniter.supervisor import RunConfig, Supervisor


class FixtureProvider(Provider):
    def __init__(self, reviewer=False):
        super().__init__('codex', sys.executable, True)
        self.reviewer = reviewer
        self.calls = 0
        self.requirement_calls = 0

    def start_command(self, prompt_file, cwd, schema_file, **kwargs):
        return self.resume_command('', prompt_file, cwd, schema_file, **kwargs)

    def resume_command(self, session_id, prompt_file, cwd, schema_file, **kwargs):
        if schema_file.name=='requirements.schema.json':
            if not self.reviewer or not kwargs.get('read_only'):
                raise ValueError('Requirement elaboration must be read-only reviewer work')
            self.requirement_calls += 1
            brief={'status':'ready','summary':'Clarified fixture requirement','refined_requirement':'Write fixture.txt with ok and independently verify its exact content.',
                   'acceptance_criteria':['fixture.txt contains exactly ok'],'constraints':['Use only local fixtures'],
                   'assumptions':[],'blockers':[]}
            payload={'type':'result','session_id':'33333333-3333-3333-3333-333333333333','result':json.dumps(brief)}
            return PreparedCommand([sys.executable,'-c',f'import json;print(json.dumps({payload!r}))'])
        self.calls += 1
        if kwargs.get('read_only', False) != self.reviewer:
            raise ValueError('Fixture role boundary lost')
        verdict = {'status': 'done', 'scope': 'requirement', 'summary': 'Fixture ready', 'module_ids': ['fixture'],
                   'done': [], 'gaps': [], 'next_actions': [], 'blockers': [], 'modules': [], 'evidence': ['fixture.txt']}
        code = 'from pathlib import Path; import json; '
        if self.reviewer and self.calls == 1:
            verdict.update(status='continue', summary='Fixture requires correction', gaps=['fixture must contain ok'])
            code += "assert Path('fixture.txt').read_text() == 'incomplete'; "
        elif self.reviewer:
            verdict['issue_resolutions'] = [{'id': 'AUD-1', 'summary': 'Verified corrected content', 'evidence': ['fixture.txt']}]
            code += "assert Path('fixture.txt').read_text() == 'ok'; "
        else:
            if self.calls > 1:
                code += f"assert 'fixture must contain ok' in Path({str(prompt_file)!r}).read_text(); "
            content = 'incomplete' if self.calls == 1 else 'ok'
            code += f"Path('fixture.txt').write_text({content!r}); "
        payload = {'type': 'result', 'session_id': '22222222-2222-2222-2222-222222222222' if self.reviewer else '11111111-1111-1111-1111-111111111111',
                   'result': json.dumps(verdict)}
        code += f'print(json.dumps({payload!r}))'
        return PreparedCommand([sys.executable, '-c', code])


def selftest() -> dict:
    with tempfile.TemporaryDirectory(prefix='auto-moniter-selftest-') as directory:
        root = Path(directory)
        worker, reviewer = FixtureProvider(), FixtureProvider(True)
        check = shlex.join([sys.executable, '-c', "from pathlib import Path; assert Path('fixture.txt').read_text() == 'ok'"])
        result = Supervisor(RunConfig('Create a verified fixture', root, worker, reviewer=reviewer,
                              workflow='review-first',
                              modules=[{'id': 'fixture', 'title': 'Fixture', 'acceptance': ['content is ok']}],
                              quality_policy={'requirements': [{'id': 'R1', 'text': 'Fixture content is correct',
                                              'module_ids': ['fixture'], 'evidence_paths': ['fixture.txt']}]},
                              checks=[check], max_rounds=2, max_runtime_s=20, retry_delay_s=0,
                              git_history='off', run_dir=root / 'run'), log=lambda _: None).run()
        path = root / 'run/report.json'
        report = json.loads(path.read_text()) if path.exists() else {}
        resolved = any(i['id'] == 'AUD-1' and i['status'] == 'resolved' for i in report.get('audit_issues', {}).get('items', []))
        passed = (result.status == 'done' and worker.calls == reviewer.calls == 2 and reviewer.requirement_calls==1 and resolved
                  and report.get('modules', [{}])[0].get('status') == 'accepted'
                  and not (root / 'run/process.json').exists())
        return {'passed': passed, 'status': result.status, 'summary': result.summary,
                'checks': ['read-only requirement elaboration','worker subprocess', 'independent reviewer subprocess', 'review rejection', 'feedback to worker',
                           'explicit audit closure', 'module ledger', 'requirement evidence coverage', 'acceptance command', 'report persistence', 'child cleanup'],
                'paid_model_calls': 0, 'worker_calls': worker.calls, 'reviewer_calls': reviewer.calls,
                'requirement_review_calls':reviewer.requirement_calls,
                'scope': 'Offline role flow; authentication and OS isolation require separate verification.'}
