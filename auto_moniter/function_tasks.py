"""One reviewer-owned function assignment at a time, independent of module gates."""
import copy
import json
import re
from pathlib import Path

IDENTIFIER_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
IDENTIFIER_SCHEMA = {'type':'string','pattern':IDENTIFIER_PATTERN,
                     'description':'1-64 ASCII letters, digits, underscores, dots or hyphens; begin with a letter or digit. Example: F1.'}
IDENTIFIER_CONTRACT = ('Task id, module_id and integration_scope must use 1-64 ASCII letters, digits, '
                       'underscores, dots or hyphens, beginning with a letter or digit; for example F1 or auth-01. '
                       'Use accepted task IDs in depends_on. Colons belong to criterion/check references such as '
                       'req:1:1 or check:1; task:1 is not a valid task ID. ')

FIELDS={'id','module_id','file','symbol','objective','inputs','outputs','boundaries','verification','depends_on','delivery_scope'}
TASK_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 **{k:{'type':'string'} for k in ['file','symbol','objective','inputs','outputs']},
 **{k:{'type':'array','items':{'type':'string'}} for k in ['boundaries','verification']},
 **{k:copy.deepcopy(IDENTIFIER_SCHEMA) for k in ['id','module_id']},
 'depends_on':{'type':'array','maxItems':200,'uniqueItems':True,'items':copy.deepcopy(IDENTIFIER_SCHEMA)},
 'delivery_scope':{'type':'string','enum':['function','module','requirement']}},'required':sorted(FIELDS)}
NEXT_SCHEMA={'anyOf':[{'type':'null'},TASK_SCHEMA]}
GATE_FIELDS = {'criterion_ids', 'check_ids', 'test_files'}
GATE_LIST_LIMITS = {'criterion_ids':128, 'check_ids':30, 'test_files':30}
INTEGRATION_FIELDS = {'kind', 'integration_scope', 'targets', 'atomic_reason'}
INTEGRATION_TASK_FIELDS = (FIELDS - {'file', 'symbol'}) | GATE_FIELDS | INTEGRATION_FIELDS
VERIFICATION_TASK_FIELDS = FIELDS | GATE_FIELDS | {'kind'}
MAX_INTEGRATION_TARGETS = 4
MAX_PLAN_TASKS = 200


def targets(task):
    if task.get('kind') == 'integration':
        return tuple((target['file'], target['symbol']) for target in task['targets'])
    return ((task['file'], task['symbol']),)


def writable_files(task):
    return list(dict.fromkeys([*(file for file, _ in targets(task)),
                               *task.get('edit_files', []), *task.get('test_files', [])]))


def identity(task):
    return (task['id'], task['module_id'], task['delivery_scope'], task['depends_on'],
            task.get('kind'), task.get('integration_scope'), sorted(targets(task)),
            sorted(task['test_files']) if task.get('kind') == 'integration' else None,
            sorted(task.get('edit_files', [])))


def validate_target(file, symbol):
    if any(not isinstance(v, str) or not v.strip() or len(v) > 2000 or '\0' in v for v in (file, symbol)):
        raise ValueError('Missing precise function assignment file or symbol')
    path = Path(file)
    if path.is_absolute() or '..' in path.parts or not path.parts or re.search(r'[*?\[\]\r\n]', file):
        raise ValueError('Function assignment file must be project-relative')
    if re.search(r'\s|[,;，；*?]', symbol):
        raise ValueError('Function assignment must name one qualified function symbol')


def validate_targets(value):
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_INTEGRATION_TARGETS:
        raise ValueError(f'Integration requires 2–{MAX_INTEGRATION_TARGETS} function targets')
    files = set()
    for target in value:
        if not isinstance(target, dict) or set(target) != {'file', 'symbol'}:
            raise ValueError('Each integration target requires file and symbol')
        file, symbol = target['file'], target['symbol']
        validate_target(file, symbol)
        if file in files:
            raise ValueError('Integration targets must use distinct files')
        files.add(file)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(IDENTIFIER_PATTERN, value)


class AssignmentIdentifierError(ValueError):
    """A reviewer can repair identifier syntax without changing the task."""


def validate_identifiers(value):
    for key in ['id','module_id','integration_scope']:
        if key in value and not identifier(value[key]):
            raise AssignmentIdentifierError('Invalid function assignment '+key+
                ': use 1-64 ASCII letters, digits, underscores, dots or hyphens; '
                'start with a letter or digit (for example F1). Colons and spaces are not allowed.')


def validate(value, modules=None):
    if not isinstance(value,dict) or set(value) - {'edit_files'} not in (FIELDS, FIELDS | GATE_FIELDS, INTEGRATION_TASK_FIELDS, VERIFICATION_TASK_FIELDS):
        raise ValueError('Function assignment requires id/module_id/file/symbol/objective/inputs/outputs/boundaries/verification/depends_on/delivery_scope')
    validate_identifiers(value)
    for key in ['objective','inputs','outputs']:
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>2000 or '\0' in value[key]:
            raise ValueError('Missing precise function assignment '+key)
    if set(value) - {'edit_files'} == INTEGRATION_TASK_FIELDS:
        reason = value['atomic_reason']
        if (value['kind'] != 'integration' or not identifier(value['integration_scope'])
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 1500 or '\0' in reason):
            raise ValueError('Integration requires a valid scope ID and a reason the targets must change together')
        validate_targets(value['targets'])
    else:
        validate_target(value['file'], value['symbol'])
        if set(value) - {'edit_files'} == VERIFICATION_TASK_FIELDS and (
                value['kind'] != 'verification' or value['delivery_scope'] not in ('module', 'requirement')
                or value['test_files']):
            raise ValueError('Verification-only tasks must close a module or requirement without test file writes')
    edits = value.get('edit_files', [])
    if (not isinstance(edits, list) or len(edits) > 20
            or any(not isinstance(name, str) or not name or len(name) > 512 for name in edits)
            or len(set(edits)) != len(edits)):
        raise ValueError('edit_files must contain at most 20 distinct exact project-relative paths')
    if edits and (not GATE_FIELDS <= value.keys() or value.get('kind') in ('integration', 'verification')):
        raise ValueError('edit_files requires a verified implementation assignment, not integration or verification')
    for name in edits:
        validate_target(name, 'file')
        if str(Path(name)) != name or any(ord(c) < 32 for c in name) or name in ('.', ''):
            raise ValueError('edit_files must use canonical exact paths')
    for key in ['boundaries','verification','depends_on']:
        v=value[key]
        limit = MAX_PLAN_TASKS if key == 'depends_on' else 20
        if not isinstance(v,list) or len(v)>limit or any(not isinstance(s,str) or not s.strip() or len(s)>1500 for s in v):
            raise ValueError('Invalid function assignment '+key)
        if key == 'depends_on' and len(set(v)) != len(v):
            raise ValueError('Function assignment depends_on must contain distinct IDs')
    if not value['boundaries'] or not value['verification']:
        raise ValueError('Function assignment requires boundaries and behavioral verification')
    if value['delivery_scope'] not in ['function','module','requirement']:
        raise ValueError('Invalid function delivery scope')
    if modules is not None and value['module_id'] not in modules:
        raise ValueError('Function assignment names an unknown module')
    if len(json.dumps(value,ensure_ascii=False).encode())>16000:
        raise ValueError('Function assignment exceeds 16 KiB')
    if GATE_FIELDS <= value.keys():
        for key in GATE_FIELDS:
            entries = value[key]
            if (not isinstance(entries, list) or len(entries) > GATE_LIST_LIMITS[key]
                    or any(not isinstance(x, str) or not x or len(x) > 512 for x in entries)
                    or len(set(entries)) != len(entries) or (not entries and key != 'test_files')):
                raise ValueError('Invalid function assignment ' + key)
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
            if not isinstance(item,dict) or set(item) - {'file_hashes'} not in (
                    {'task','round','tree','evidence'}, {'task','round','tree','evidence','verification','symbol_hash'},
                    {'task','round','tree','evidence','verification','target_hashes'}):
                raise ValueError('Invalid accepted function checkpoint')
            task=validate(item['task'])
            if (task['id'] in seen or not set(task['depends_on']) <= seen or type(item['round']) is not int or item['round']<1
                    or not isinstance(item['tree'], str) or not re.fullmatch('[0-9a-f]{64}',item['tree'])):
                raise ValueError('Invalid accepted function version')
            if not isinstance(item['evidence'],list) or any(not isinstance(e,str) for e in item['evidence']) or len(json.dumps(item).encode())>24000:
                raise ValueError('Invalid accepted function evidence')
            if 'verification' in item:
                files = item.get('file_hashes', {})
                if (not isinstance(files, dict) or set(files) != set(task.get('edit_files', []))
                        or any(h is not None and (not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h))
                               for h in files.values())):
                    raise ValueError('Invalid accepted assignment file verification')
                checks = item['verification']
                if task.get('kind') == 'integration':
                    hashes = item.get('target_hashes')
                    valid_hashes = (isinstance(hashes, dict) and set(hashes) == {file for file, _ in targets(task)}
                                    and all(isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for h in hashes.values()))
                else:
                    digest = item.get('symbol_hash')
                    valid_hashes = isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                if (not GATE_FIELDS <= task.keys() or not isinstance(checks, dict) or set(checks) != set(task['check_ids'])
                        or not valid_hashes
                        or any(not isinstance(log, str) or not re.fullmatch(r'[0-9]+-check-[0-9]+\.log', log) for log in checks.values())):
                    raise ValueError('Invalid accepted function verification')
            elif task.get('kind') == 'integration':
                raise ValueError('Accepted integration requires verification for every target')
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
        if reviewer.task_status!='accepted':
            return False
        if len(self.accepted)>=200:
            raise ValueError('Function dispatch limit reached; start a linked follow-up')
        evidence = [item.encode('utf-8')[:512].decode('utf-8', 'ignore') for item in reviewer.evidence[:4]]
        self.accepted.append({'task':self.current,'round':index,'tree':tree,'evidence':evidence})
        self.current=None
        return True


def schema(base, *, planning=False, gate=False, integration=False, full_plan=False):
    result=copy.deepcopy(base)
    result['properties']['modules'] = module_schema()
    next_schema = copy.deepcopy(NEXT_SCHEMA)
    if gate:
        task = next_schema['anyOf'][1]
        task['properties'].update({k: {'type': 'array', 'maxItems':GATE_LIST_LIMITS[k],
                                      'items': {'type': 'string'}} for k in GATE_FIELDS})
        task['properties']['check_ids']['description'] = (
            'Approved check IDs, auto:N for the Nth declared Python unittest, Node, Cargo integration or native Swift test file, '
            'or regression:N from the watcher catalog of already accepted tests. '
            'Watcher derives the local test command; do not provide shell commands.')
        task['required'].extend(sorted(GATE_FIELDS))
        task['properties']['edit_files'] = {'type': 'array', 'maxItems': 20, 'items': {'type': 'string'},
            'description': 'Exact files for one atomic change, including resources, project registration, helpers or docs. '
                           'Listed source files permit whole-file edits. Use [] for strict single-function work. '
                           'Never list existing tests, unneeded files or patterns. Prose boundaries grant no extra writes.'}
        task['required'].append('edit_files')
        verification = copy.deepcopy(task)
        verification['properties']['kind'] = {'type': 'string', 'enum': ['verification']}
        verification['properties']['delivery_scope']['enum'] = ['module', 'requirement']
        verification['required'].append('kind')
        next_schema['anyOf'].append(verification)
        if integration:
            grouped = copy.deepcopy(task)
            for key in ('file', 'symbol'):
                grouped['properties'].pop(key)
            grouped['properties'].update({
                'kind': {'type': 'string', 'enum': ['integration']},
                'integration_scope': copy.deepcopy(IDENTIFIER_SCHEMA), 'atomic_reason': {'type': 'string'},
                'targets': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                            'properties': {k: {'type': 'string'} for k in ('file', 'symbol')},
                            'required': ['file', 'symbol']}},
            })
            grouped['required'] = sorted(INTEGRATION_TASK_FIELDS | {'edit_files'})
            next_schema['anyOf'].append(grouped)
    if planning:
        additions={'next_task':next_schema,'modules':module_schema()}
        if full_plan:
            additions['function_plan'] = {'type':'array', 'maxItems':MAX_PLAN_TASKS,
                'items':{'anyOf':copy.deepcopy(next_schema['anyOf'][1:])}}
    else:
        additions={'task_id':{'type':'string'},'task_status':{'type':'string',
                   'enum':['done','incomplete','accepted','changes_required']},'next_task':next_schema}
    result['properties'].update(additions)
    result['required'].extend(additions)
    return result


def module_schema():
    from auto_moniter.verdict import VERDICT_SCHEMA
    result = copy.deepcopy(VERDICT_SCHEMA['properties']['modules'])
    result['items']['properties']['id'] = copy.deepcopy(IDENTIFIER_SCHEMA)
    return result


def verdict_contract(role, task, *, report_only=False, module_ids=None):
    group = task.get('kind') == 'integration'
    scope = ('the dispatched atomic change, its named primary function and exact edit_files' if task.get('edit_files')
             else 'the dispatched integration and all of its listed functions' if group else 'the dispatched function')
    manifest = ('The edit_files manifest grants whole-file edits at exactly those paths, including necessary helper '
                'declarations, resources and registration. Preserve tests that predate the assignment and all other file/function scopes. '
                'Describe the files actually changed in evidence. ' if task.get('edit_files') else '')
    delivery_modules = (list(module_ids) if task['delivery_scope'] == 'requirement' and module_ids is not None
                        else [task['module_id']])
    completion = ('For module or requirement delivery, return status=done with scope matching delivery_scope and '
                  'module_ids=' + json.dumps(delivery_modules) + ' only when that delivery scope is fully verified. '
                  + ('Final requirement delivery jointly confirms every module in the watcher ledger, including '
                     'prerequisites implemented by earlier accepted functions. This expands reporting scope, '
                     'not file edit permissions; unchanged verified functions do not need reimplementation. '
                     if task['delivery_scope'] == 'requirement' else ''))
    if role=='worker':
        return (
            ('Correct only the report using existing evidence. Do not edit files or run builds/tests. '
             if report_only else
             'Verification-only: inspect existing code and evidence, do not edit source or tests or run commands that write files. '
             'Report concrete defects as incomplete; the watcher executes approved checks. '
             if task.get('kind') == 'verification' else f'Only implement {scope} and its focused verification. Do not start other work. ')+
            manifest + 'Return task_id='+task['id']+' and task_status=done only when the whole assignment has been assessed and is ready for watcher acceptance; '
            'otherwise task_status=incomplete. Return next_task=null and modules=[]. '
            'For delivery_scope=function return status=continue even when task_status=done. '
            + completion +
            'Give evidence for every target file and behavioral check results. '
            'For task_status=done, evidence must be nonempty. Use gaps=[], blockers=[], next_actions=[] '
            'when no implementation work remains. Existing notes in those fields are assessed by the reviewer; '
            'they do not alone invalidate a submitted completion or require another worker report. '
            'Watcher checks and independent review are automatic acceptance steps, not unfinished worker actions; '
            'describe that handoff in summary rather than next_actions. '
            'For task_status=incomplete, gaps and next_actions describe only this assignment.')
    return (
        IDENTIFIER_CONTRACT + manifest + f'Review {scope} and its caller/verification evidence. Cite every target file. Return matching task_id and '
        'task_status=accepted or changes_required. Base acceptance on the candidate and current evidence. '
        'Independently assess the worker concerns and test evidence. Nonempty worker gaps/next_actions, '
        'or waiting for automatic watcher checks or review, are not by themselves implementation defects. '
        'If the assignment is correct, accept it in this response and give the next_task; do not send the worker '
        'back to reformat its report or resubmit unchanged code. For actual defects or missing verification, '
        'return changes_required with the concrete behavior or evidence still needed. '
        'Close invalid historical report-only audit findings yourself using issue_resolutions and current file evidence; '
        'a new code change is not required to reject a false finding. '
        'Worker issue_resolutions are untrusted suggestions and never close the watcher ledger; independently '
        'confirm or reject them in your own issue_resolutions without requesting another worker report. '
        'For changes_required: status=continue, next_task=null; give only this assignment\'s actionable gaps and minimal repair. '
        + ('For a concrete defect found in verification-only work, you may instead give next_task as one bounded '
           'implementation task with a new ID, accurate targets and approved checks; do not mark this verification accepted. '
           if task.get('kind') == 'verification' else '') +
        'For an accepted intermediate assignment: status=continue, gaps/next_actions/blockers=[], '
        'and propose exactly one next_task that can be implemented and verified on its own. '
        + completion +
        'Propose one next_task after module delivery, null after requirement delivery. '
        'Final requirement approval still checks coverage of every original requirement and integration.')
