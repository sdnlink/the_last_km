"""One reviewer-owned function assignment at a time, independent of module gates."""
import copy
import json
import re
from pathlib import Path

FIELDS={'id','module_id','file','symbol','objective','inputs','outputs','boundaries','verification','depends_on','delivery_scope'}
TASK_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 **{k:{'type':'string'} for k in ['id','module_id','file','symbol','objective','inputs','outputs']},
 **{k:{'type':'array','items':{'type':'string'}} for k in ['boundaries','verification','depends_on']},
 'delivery_scope':{'type':'string','enum':['function','module','requirement']}},'required':sorted(FIELDS)}
NEXT_SCHEMA={'anyOf':[{'type':'null'},TASK_SCHEMA]}


def validate(value, modules=None):
    if not isinstance(value,dict) or set(value)!=FIELDS:
        raise ValueError('Function assignment requires id/module_id/file/symbol/objective/inputs/outputs/boundaries/verification/depends_on/delivery_scope')
    for key in ['id','module_id']:
        if not isinstance(value[key],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',value[key]):
            raise ValueError('Invalid function assignment '+key)
    for key in ['file','symbol','objective','inputs','outputs']:
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>2000 or '\0' in value[key]:
            raise ValueError('Missing precise function assignment '+key)
    path=Path(value['file'])
    if path.is_absolute() or '..' in path.parts or not path.parts or re.search(r'[*?\[\]\r\n]', value['file']):
        raise ValueError('Function assignment file must be project-relative')
    if re.search(r'\s|[,;，；*?]',value['symbol']):
        raise ValueError('Function assignment must name one qualified function symbol')
    for key in ['boundaries','verification','depends_on']:
        v=value[key]
        if not isinstance(v,list) or len(v)>20 or any(not isinstance(s,str) or not s.strip() or len(s)>1500 for s in v):
            raise ValueError('Invalid function assignment '+key)
    if not value['boundaries'] or not value['verification']:
        raise ValueError('Function assignment requires boundaries and behavioral verification')
    if value['delivery_scope'] not in ['function','module','requirement']:
        raise ValueError('Invalid function delivery scope')
    if modules is not None and value['module_id'] not in modules:
        raise ValueError('Function assignment names an unknown module')
    if len(json.dumps(value,ensure_ascii=False).encode())>16000:
        raise ValueError('Function assignment exceeds 16 KiB')
    return copy.deepcopy(value)


class FunctionTasks:
    def __init__(self, saved=None):
        if saved is None:
            saved={'current':None,'accepted':[]}
        if not isinstance(saved,dict) or set(saved)!={'current','accepted'} or not isinstance(saved['accepted'],list) or len(saved['accepted'])>200:
            raise ValueError('Invalid function dispatch checkpoint')
        self.current=validate(saved['current']) if saved['current'] is not None else None
        self.accepted=copy.deepcopy(saved['accepted'])
        seen=set()
        for item in self.accepted:
            if not isinstance(item,dict) or set(item)!={'task','round','tree','evidence'}:
                raise ValueError('Invalid accepted function checkpoint')
            task=validate(item['task'])
            if (task['id'] in seen or not set(task['depends_on']) <= seen or type(item['round']) is not int or item['round']<1
                    or not isinstance(item['tree'], str) or not re.fullmatch('[0-9a-f]{64}',item['tree'])):
                raise ValueError('Invalid accepted function version')
            if not isinstance(item['evidence'],list) or any(not isinstance(e,str) for e in item['evidence']) or len(json.dumps(item).encode())>24000:
                raise ValueError('Invalid accepted function evidence')
            seen.add(task['id'])
        if self.current and (self.current['id'] in seen or not set(self.current['depends_on']) <= seen):
            raise ValueError('Saved assignment repeats accepted work or has unaccepted dependencies')

    def export(self):return copy.deepcopy({'current':self.current,'accepted':self.accepted})

    def assign(self,value,modules):
        task=validate(value,modules)
        accepted={i['task']['id'] for i in self.accepted}
        if task['id'] in accepted or not set(task['depends_on'])<=accepted:
            raise ValueError('Function assignment has unaccepted dependencies or repeats accepted work')
        if self.current and self.current!=task:
            raise ValueError('Reviewer cannot replace an unfinished function assignment; finish or repair it first')
        self.current=task

    def accept(self,worker,reviewer,index,tree):
        if not self.current or worker.task_id!=self.current['id'] or reviewer.task_id!=self.current['id']:
            raise ValueError('Function result must match the single dispatched task ID')
        if worker.task_status!='done' or reviewer.task_status!='accepted':
            return False
        if len(self.accepted)>=200:
            raise ValueError('Function dispatch limit reached; start a linked follow-up')
        evidence = [item.encode('utf-8')[:512].decode('utf-8', 'ignore') for item in reviewer.evidence[:4]]
        self.accepted.append({'task':self.current,'round':index,'tree':tree,'evidence':evidence})
        self.current=None
        return True


def schema(base, *, planning=False):
    result=copy.deepcopy(base)
    if planning:
        additions={'next_task':NEXT_SCHEMA,'modules':module_schema()}
    else:
        additions={'task_id':{'type':'string'},'task_status':{'type':'string',
                   'enum':['done','incomplete','accepted','changes_required']},'next_task':NEXT_SCHEMA}
    result['properties'].update(additions)
    result['required'].extend(additions)
    return result


def module_schema():
    from auto_moniter.verdict import VERDICT_SCHEMA
    return VERDICT_SCHEMA['properties']['modules']


def verdict_contract(role, task):
    if role=='worker':
        return (
            'Only implement the dispatched function and its focused verification. Do not start other work. '
            'Return task_id='+task['id']+' and task_status=done only when the function is implemented and checked; '
            'otherwise task_status=incomplete. Return next_task=null and modules=[]. '
            'For delivery_scope=function return status=continue even when task_status=done. '
            'For module or requirement delivery, return status=done with scope matching delivery_scope and '
            'module_ids=['+task['module_id']+'] only when that delivery scope is fully verified. '
            'Give changed-file evidence and behavioral check results; gaps and next_actions are only for this function.')
    return (
        'Review only the dispatched function and its caller/verification evidence. Return matching task_id and '
        'task_status=accepted or changes_required. Accept only after worker task_status=done and current evidence agrees. '
        'For changes_required: status=continue, next_task=null; give only this function\'s actionable gaps and minimal repair. '
        'For an accepted intermediate function: status=continue, gaps/next_actions/blockers=[], '
        'and propose exactly one independently implementable next_task. '
        'For module or requirement delivery: status=done with matching scope and module_ids containing only the '
        'current module; propose one next_task after module delivery, null after requirement delivery. '
        'Final requirement approval still checks coverage of every original requirement and integration.')
