"""Bind pre-review check receipts to one immutable submitted candidate."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from auto_moniter.redaction import redact
from auto_moniter.storage import write_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def observed_assertion_failure(receipt, run):
    """Use actual failed-test output, never infer a product bug from exit 1 alone."""
    return any(output for _, output in _failed_assertion_outputs(receipt, run))


def assertion_signature(receipt, run, command):
    for executed, output in _failed_assertion_outputs(receipt, run):
        if executed == command and output:
            stable = re.sub(r'(?m)^\s*duration_ms: .*\n|^Ran (\d+) tests? in [\d.]+s$', r'\1', output)
            return hashlib.sha256(stable.encode()).hexdigest()
    return None


def failed_assertion_locations(receipt, run, root, paths):
    """Find corroborating test locations in every failed check's saved output."""
    locations = []
    for command, output in _failed_assertion_outputs(receipt, run):
        found = []
        for path in paths:
            names = '|'.join(re.escape(str(name)) for name in (root / path, path))
            for match in re.finditer(r'(?<![\w./-])(?:' + names + r')(?::|", line )(\d{1,9})\b', output):
                found.append((path, int(match[1])))
        if not found:
            return []
        locations.append((command, sorted(set(found))))
    return locations


def _failed_assertion_outputs(receipt, run):
    for item in receipt.get('checks', []):
        if item.get('status') != 'check_failed':
            continue
        path = Path(run)/item['log']
        output = ''
        try:
            # Callers validate receipt/log hashes first. Skip command text:
            # mentioning an assertion in the requested command is not evidence.
            output = '\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[2:])
        except OSError:
            pass
        asserted = re.search(r'AssertionError|FAILED \((?:failures|errors)=|'
                             r'(?m:^not ok [0-9]+)|test result: FAILED|panicked at|XCTAssert.*failed', output)
        yield item['command'], output if asserted else ''


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


def intact(receipt):
    return (isinstance(receipt, dict) and receipt.get('version') == 1
            and receipt.get('digest') == digest({key: value for key, value in receipt.items() if key != 'digest'}))


def retained_results(receipt, run, context):
    """Keep individual passing results whose original bytes can still be verified."""
    if (not intact(receipt) or receipt.get('context') != context or not receipt.get('passed')
            or not isinstance(receipt.get('checks'), list) or not isinstance(receipt.get('logs'), dict)):
        return {}
    return {
        command: result for command, result in zip(context['commands'], receipt['checks'])
        if isinstance(result, dict) and result.get('status') == 'passed' and result.get('command') == redact(command)
        and (hashes := log_hashes(run, [result]))
        and all(receipt['logs'].get(name) == value for name, value in hashes.items())
    }


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


def review_summary(receipt, manifest=None):
    """Present check results once, with log references for the full evidence."""
    context = receipt['context']
    summary = {key: receipt[key] for key in ('passed', 'complete', 'source_changed', 'digest')}
    summary.update(round=context['round'], tree=context['tree'], expected_checks=len(context['commands']))
    if context.get('task'):
        summary['task_id'] = context['task']['id']
    if context.get('publication_tree'):
        summary['publication_tree'] = context['publication_tree']
    if receipt.get('reused_from'):
        summary['reused_from'] = receipt['reused_from']
    if receipt.get('scope_gaps'):
        summary['scope_gaps'] = receipt['scope_gaps']
    summary['checks'] = [
        {key: check[key] for key in ('status', 'exit_code', 'log', 'reused_from') if key in check}
        for check in receipt['checks']
    ]
    bindings = {item['command']: {key: item[key] for key in ('check_id', 'git_tree', 'base')}
                for item in context.get('supplemental_checks', []) if 'git_tree' in item}
    executed = set()
    for position, (check, result) in enumerate(zip(receipt['checks'], summary['checks'])):
        command = check['command']
        if position < len(context['commands']) and redact(context['commands'][position]) == command:
            command = context['commands'][position]
        executed.add(command)
        if manifest is not None:
            result.update(manifest.get(command, {}))
        if command in bindings:
            result.update(bindings[command])
    if manifest is not None:
        summary['not_run'] = [manifest[command] for command in context['commands'] if command not in executed]
    return summary


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
    return (intact(receipt) and receipt.get('logs') is not None
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
