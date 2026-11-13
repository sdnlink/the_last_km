"""Bind pre-review check receipts to one immutable submitted candidate."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from auto_moniter.storage import write_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def log_hashes(run, results):
    hashes = {}
    for item in results:
        if not isinstance(item,dict):
            return None
        name = item.get('log', '')
        if not isinstance(name, str) or not re.fullmatch(r'[0-9]+-check-[0-9]+(?:-attempt-[0-9]+)?\.log', name):
            return None
        path = Path(run)/name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 128*1024:
                return None
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
    return hashes


def create(run, context, snapshot, passed):
    results = snapshot.get('checks', [])
    receipt = {'version': 1, 'context': context, 'checks': results,
               'passed': bool(passed), 'source_changed': bool(snapshot.get('source_changed')),
               'complete': len(results) == len(context['commands']),
               'logs': log_hashes(run, results),
               'passed_commands': {command:item['log'] for command,item in zip(context['commands'],results)
                                   if item['status']=='passed'}}
    receipt['digest'] = digest(receipt)
    return receipt


def valid(receipt, run, context):
    if (not isinstance(receipt,dict) or receipt.get('version') != 1 or receipt.get('context') != context
            or not isinstance(context,dict) or not isinstance(receipt.get('checks'),list)
            or not isinstance(context.get('commands'),list)
            or not isinstance(receipt.get('passed_commands'),dict)
            or any(type(receipt.get(key)) is not bool for key in ['passed','complete','source_changed'])):
        return False
    if receipt['passed'] and (not receipt['complete'] or receipt['source_changed']
            or len(receipt['checks']) != len(context['commands'])
            or any(not isinstance(item,dict) or item.get('status') != 'passed' for item in receipt['checks'])
            or set(receipt['passed_commands']) != set(context['commands'])):
        return False
    payload = {key: value for key, value in receipt.items() if key != 'digest'}
    return (receipt.get('digest') == digest(payload) and isinstance(receipt.get('checks'), list)
            and receipt.get('logs') is not None
            and log_hashes(run, receipt['checks']) == receipt['logs'])


def archive(run, receipt):
    """Keep the preceding attempt and its logs before rechecking the same round."""
    if not isinstance(receipt,dict) or not valid(receipt, run, receipt.get('context')):
        return None
    run = Path(run)
    index = receipt['context']['round']
    attempt = 1
    while ((run/f'{index:03d}-checks-attempt-{attempt:03d}.json').exists()
           or any((run/(Path(item['log']).stem+f'-attempt-{attempt:03d}.log')).exists()
                  for item in receipt['checks'])):
        attempt += 1
    saved = copy.deepcopy(receipt)
    names = {}
    for item in saved['checks']:
        source = run/item['log']
        name = source.stem+f'-attempt-{attempt:03d}.log'
        with (run/name).open('xb') as stream:
            stream.write(source.read_bytes());stream.flush();os.fsync(stream.fileno())
        names[item['log']] = name
        item['log'] = name
    saved['passed_commands'] = {command:names[name] for command,name in saved['passed_commands'].items()}
    saved['failures'] = list(saved.get('failures',[]))
    for old,new in names.items():
        saved['failures'] = [text.replace(old,new) for text in saved['failures']]
    saved['logs'] = log_hashes(run, saved['checks'])
    saved['digest'] = digest({key:value for key,value in saved.items() if key!='digest'})
    name = f'{index:03d}-checks-attempt-{attempt:03d}.json'
    write_json(run/name, saved, durable=True)
    return name
