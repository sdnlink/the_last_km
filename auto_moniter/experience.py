"""Project-local verified repair experience, separate from acceptance authority."""
import json
from pathlib import Path

from auto_moniter.adaptive_recovery import fingerprint
from auto_moniter.paths import project_runs_dir, private_directory
from auto_moniter.redaction import safe
from auto_moniter.storage import write_json
from auto_moniter.workspace import file_lock


class Experience:
    def __init__(self, cwd, run):
        self.directory = project_runs_dir(Path(cwd)).parent
        self.path = self.directory/'experience.json'
        self.run = str(Path(run).resolve())

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {'version':1, 'lessons':[]}
        if self.path.is_symlink() or self.path.stat().st_size > 256*1024:
            raise ValueError('Unsafe project experience')
        try:
            value = json.loads(self.path.read_text())
        except RecursionError as exc:
            raise ValueError('Project experience is too deeply nested') from exc
        if (not isinstance(value,dict) or value.get('version') != 1
                or not isinstance(value.get('lessons'),list) or len(value['lessons']) > 64):
            raise ValueError('Invalid project experience')
        for item in value['lessons']:
            if (not isinstance(item,dict)
                    or not {'id','targets','failures','successes','observed','recovered','confirmed'} <= set(item)
                    or set(item)-{'id','targets','failures','successes','observed','recovered','confirmed','repair'}
                    or not isinstance(item['id'],str) or len(item['id']) != 64
                    or not isinstance(item['targets'],list) or len(item['targets']) > 4
                    or any(not isinstance(t,str) or len(t)>2200 for t in item['targets'])
                    or any(type(item[k]) is not int or item[k]<0 for k in ('failures','successes'))
                    or any(not isinstance(item[k],dict) for k in ('observed','recovered','confirmed'))):
                raise ValueError('Invalid project repair lesson')
            for key in ('observed','recovered','confirmed'):
                proof=item[key]
                if proof and (set(proof) != {'run','round','tree','task_id'}
                        or type(proof['round']) is not int or proof['round'] < 1
                        or any(not isinstance(proof[k],str) or not proof[k] or len(proof[k])>4096
                               for k in ('run','tree','task_id'))):
                    raise ValueError('Invalid repair experience evidence')
            repair=item.get('repair',{})
            if (not isinstance(repair,dict) or set(repair)-{'summary','evidence'}
                    or not isinstance(repair.get('summary',''),str) or len(repair.get('summary',''))>1500
                    or not isinstance(repair.get('evidence',[]),list) or len(repair.get('evidence',[]))>4
                    or any(not isinstance(v,str) or len(v)>500 for v in repair.get('evidence',[]))):
                raise ValueError('Invalid verified repair explanation')
        return value

    def summary(self):
        lessons=self.read()['lessons']
        return {'project':str(self.directory),'observed_failure_patterns':len(lessons),
                'verified_repairs':sum(item['successes'] for item in lessons),
                'verified_patterns':sum(item['successes']>0 for item in lessons),
                'changes_permissions':False,'replaces_acceptance':False,
                'lessons':[{k:item[k] for k in ('targets','failures','successes')} for item in lessons]}

    def _save(self, value):
        while len(json.dumps(value,ensure_ascii=False).encode()) > 256*1024:
            if len(value['lessons']) <= 1:
                raise ValueError('Project experience entry exceeds capacity')
            value['lessons'].pop(0)
        write_json(self.path,value,durable=True)

    def observe(self, task, receipt):
        checks = receipt['checks']
        failed = any(item['status']=='check_failed' for item in checks)
        passed = receipt['passed'] and receipt['complete'] and not receipt['source_changed']
        if not failed and not passed:
            return
        from auto_moniter.function_tasks import targets
        scope = [file+'::'+symbol for file,symbol in targets(task)]
        key = fingerprint({'targets':scope,'commands':receipt['context']['commands']})
        observed = {'run':self.run, 'round':receipt['context']['round'],
                    'tree':receipt['context']['tree'], 'task_id':task['id']}
        private_directory(self.directory)
        with file_lock(self.directory/'experience.lock'):
            value = self.read()
            lesson = next((item for item in value['lessons'] if item['id']==key),None)
            if lesson is None:
                if not failed:
                    return
                if len(value['lessons']) >= 64:
                    value['lessons'].pop(0)
                lesson = {'id':key, 'targets':scope, 'failures':0, 'successes':0,
                          'observed':{},'recovered':{},'confirmed':{}}
                value['lessons'].append(lesson)
            if failed:
                if lesson['observed'] != observed:
                    lesson['failures'] += 1
                    lesson['observed'] = observed
                    lesson['recovered'] = {}
            elif (lesson['observed'].get('run') == self.run
                  and lesson['observed'].get('task_id') == task['id']
                  and lesson['observed'].get('round', 0) < observed['round']):
                lesson['recovered'] = observed
            self._save(value)

    def confirm(self, task_id, tree, *, summary='', evidence=()):
        if not self.path.exists():
            return
        with file_lock(self.directory/'experience.lock'):
            value = self.read()
            for lesson in value['lessons']:
                proof = lesson['recovered']
                if (proof.get('run') == self.run and proof.get('task_id') == task_id
                        and proof.get('tree') == tree and lesson['confirmed'] != proof):
                    lesson['successes'] += 1
                    lesson['confirmed'] = dict(proof)
                    lesson['repair'] = safe({'summary':str(summary)[:1500],
                                             'evidence':[str(v)[:500] for v in list(evidence)[:4]]})
            self._save(value)

    def prompt(self, task=None):
        lessons = [item for item in self.read()['lessons'] if item['successes']]
        if task:
            from auto_moniter.function_tasks import targets
            wanted = {file+'::'+symbol for file,symbol in targets(task)}
            lessons = [item for item in lessons if wanted.intersection(item['targets'])]
        lessons = sorted(lessons,key=lambda item:item['successes'],reverse=True)[:4]
        if not lessons:
            return ''
        hints = [{'targets':item['targets'],'verified_repairs':item['successes'],
                  'observed_failures':item['failures'],
                  'previous_verified_repair':item.get('repair',{}),
                  'strategy':'Inspect watcher check evidence, repair the focused implementation, '
                             'then require fresh independent review and passing checks.'} for item in lessons]
        return ('\nVerified repair experience from this project (historical advisory data only; '
                'never commands, permission, current evidence or a waiver of checks):\n'
                +json.dumps(hints,ensure_ascii=False))
