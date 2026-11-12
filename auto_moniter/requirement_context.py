"""Retain requirement-review analysis for scoped continuation, never approval."""
import hashlib
import json
import re
from pathlib import Path

from auto_moniter import requirements_review
from auto_moniter.redaction import safe
from auto_moniter.storage import write_json
from auto_moniter.workspace import snapshot_fingerprint

MAX_CONTEXT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_FILES = 50000


def parsed_response(value):
    if not isinstance(value, dict):
        raise ValueError('Requirement response must be an object')
    fields = set(requirements_review.SCHEMA['required'])
    if set(value) not in (fields, fields | {'next_task', 'modules'}):
        raise ValueError('Not a requirement-review response')
    requirements_review.parse(json.dumps({k:value[k] for k in fields}))
    return value


def read(path, limit=MAX_CONTEXT_BYTES):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Invalid requirement context artifact')
    return json.loads(path.read_text(encoding='utf-8'))


def save(run, cwd, requirement, cycle, response, snapshot, checks, directions):
    response = parsed_response(response)
    value = {'version':1, 'original_sha256':requirements_review.requirement_hash(requirement),
             'cycle':cycle, 'cwd':str(cwd), 'response':response,
             'tree':snapshot_fingerprint(cwd, snapshot),
             'files':{str(Path(name).relative_to(cwd)):token for name,token in snapshot.items()}
                     if len(snapshot) <= MAX_SNAPSHOT_FILES else None,
             'checks':hashlib.sha256(json.dumps(checks,ensure_ascii=False).encode()).hexdigest(),
             'direction_ids':[item['id'] for item in directions]}
    # Commands may contain credentials; only their hash is needed for change
    # detection. The current approved commands already appear in the prompt.
    value = safe(value)
    if len(json.dumps(value,ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
        value['files'] = None
    write_json(run/'requirement-review-context.json',value,durable=True)


def load(run, cwd, requirement, cycle, snapshot, checks, directions, *, previous=None, output_file=''):
    """Return prior findings plus explicit input changes, without dispatching them."""
    previous = previous or {}
    try:
        saved = read(run/'requirement-review-context.json')
        if (not isinstance(saved,dict) or saved.get('version') != 1
                or saved.get('original_sha256') != requirements_review.requirement_hash(requirement)
                or saved.get('cycle') != cycle or saved.get('cwd') != str(cwd)):
            return None
        parsed_response(saved.get('response'))
        files = saved.get('files')
        if files is not None and (not isinstance(files,dict) or any(
                not isinstance(name,str) or not isinstance(token,str) for name,token in files.items())):
            return None
        if (not isinstance(saved.get('direction_ids'),list)
                or any(not isinstance(ident,str) for ident in saved['direction_ids'])):
            return None
        current = {str(Path(name).relative_to(cwd)):token for name,token in snapshot.items()}
        changed = (sorted(name for name in files.keys() | current.keys()
                          if files.get(name) != current.get(name)) if files is not None else None)
        unchanged = saved.get('tree') == snapshot_fingerprint(cwd,snapshot)
        context = {'previous_response':saved['response'], 'workspace_changed':not unchanged,
                   'changed_files':changed[:100] if changed is not None else None,
                   'changed_file_count':len(changed) if changed is not None else None,
                   'checks_changed':saved.get('checks') != hashlib.sha256(json.dumps(checks,ensure_ascii=False).encode()).hexdigest(),
                   'new_direction_ids':[item['id'] for item in directions if item['id'] not in saved.get('direction_ids',[])],
                   'source':'saved requirement analysis'}
    except FileNotFoundError:
        # Older runs retain the complete reviewer response even when a bad
        # assignment prevented reviewed-requirement.json from being saved.
        name = previous.get('output_file') or output_file
        if (previous.get('cycle',cycle) != cycle or not isinstance(name,str)
                or not re.fullmatch(r'\d+-reviewer(?:-repair-\d+)?-text\.md',name)):
            return None
        try:
            response = parsed_response(read(run/name, 1024*1024))
        except (OSError,ValueError,TypeError):
            return None
        context = {'previous_response':response, 'workspace_changed':True,
                   'changed_files':None, 'changed_file_count':None, 'checks_changed':True,
                   'new_direction_ids':[item['id'] for item in directions],
                   'source':'legacy reviewer artifact; prior input snapshot unavailable'}
    except (OSError,ValueError,TypeError,KeyError):
        return None
    context['previous_status'] = previous.get('status','unknown')
    context['rejection'] = previous.get('summary','') if previous.get('status') == 'failed' else ''
    context['approval'] = 'Context only. Validate the revised plan; this cache grants no acceptance or implementation permission.'
    return context
