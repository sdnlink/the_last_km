"""Locate browser challenges on the active or a still-valid deferred handoff."""

import time
from pathlib import Path

from auto_moniter import browser_execution, candidate_checks, field_handoff
from auto_moniter.control import ControlError, read_control
from auto_moniter.directions import Directions
from auto_moniter.workspace import workspace_fingerprint


def handoffs(run, state, status):
    if status not in {'action_required', 'running', 'starting', 'interrupted'} or not state.get('cwd'):
        return
    try:
        if read_control(run / 'control.json').get('action') in {'stop', 'pause'}:
            return
    except ControlError:
        return
    try:
        current = field_handoff.binding(state)
    except ValueError:
        current = None
    tree = None
    directions = None
    for filename, value in field_handoff.saved_handoffs(run):
        if not (value.get('browser_inputs') or
                any(row.get('retry_captcha') for row in value.get('failed_cases', {}).values())):
            continue
        if field_handoff.ready(value):
            continue
        if tree is None:
            tree = workspace_fingerprint(Path(state['cwd']), ignore_dir=run)
            directions = [row['id'] for row in Directions(run).messages()]
        binding = value['binding']
        if binding['tree'] != tree or binding['directions'] != directions:
            continue
        if filename == field_handoff.FILE and current == binding:
            if status != 'action_required':
                continue
            worker = next(row for row in reversed(state['rounds']) if
                          row['role'] == 'worker' and row['index'] == binding['round'])
            receipt = worker['watcher_result']['candidate_checks']
            if candidate_checks.valid(receipt, run, receipt.get('context')):
                yield filename, value, state
        elif checkpoint := field_handoff.deferred_state(run, state, value, tree=tree):
            yield filename, value, checkpoint


def pending_items(value):
    return {item['id']: item for item in value['items'] if item['kind'] == 'browser'
            and not (item.get('receipt') or item.get('executor_task') or item.get('claim'))}


def challenges(value):
    pending = pending_items(value)
    now = time.time()
    return {nonce: row for nonce, row in value.get('browser_inputs', {}).items()
            if row.get('id') in pending and row.get('kind') in {'image', 'otp'}
            and row.get('nonce') == nonce and row.get('expires', 0) > now}


def presentation(run, state, status):
    result = []
    for _, value, checkpoint in handoffs(run, state, status):
        inputs = challenges(value)
        retries = {key for key in pending_items(value)
                   if value.get('failed_cases', {}).get(key, {}).get('retry_captcha')}
        controls = retries | {row['id'] for row in inputs.values()}
        if controls:
            result.append({
                'binding': value['binding'], 'revision': value['revision'], 'deferred': checkpoint is not state,
                'browser_inputs': {nonce: {key: field for key, field in row.items() if key != 'image'}
                                   for nonce, row in inputs.items()},
                'items': [{'id': item['id'], 'title': item['title'], 'retry_captcha': item['id'] in retries}
                          for item in value['items'] if item['id'] in controls],
            })
    return result


def handle(run, status, data, *, config_path, workspace):
    directions = Directions(run)
    with directions.lock():
        state = directions.read('state.json', 32 * 1024 * 1024)
        for filename, handoff, checkpoint in handoffs(run, state, status):
            if data.get('revision') != handoff['revision'] or data.get('binding') != handoff['binding']:
                continue
            value, item = field_handoff._select(run, checkpoint, data, filename=filename)
            if item['id'] not in pending_items(value):
                break
            cases = browser_execution.cases(config_path, workspace)
            case_id = item.get('case_id')
            config = browser_execution.for_handoff(cases.get(case_id), value, case_id)
            if (not config or not config.get('interactive_captcha')
                    or not browser_execution.matches_item(config, value, item)):
                break
            field_handoff._require_dependencies(value, item)
            if data['action'] == 'retry_browser_captcha':
                if not value.get('failed_cases', {}).get(item['id'], {}).get('retry_captcha'):
                    break
                result = browser_execution.poll(run, value, item, config, retry_captcha=True)
                if result['status'] == 'failed':
                    raise ValueError(result['reason'])
                return {'status': result['status']}
            nonce = data.get('nonce')
            if not isinstance(nonce, str) or challenges(value).get(nonce, {}).get('id') != item['id']:
                break
            browser_execution.submit_captcha(run, value, item, config, nonce, data.get('code'))
            return {'submitted': True}
    raise ValueError('验证码对应的派单或浏览器会话已变化，请刷新任务。')
