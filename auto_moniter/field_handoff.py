"""Record operator evidence for an exact verification checkpoint."""
import hashlib
import re
import secrets
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timezone

from auto_moniter import candidate_checks
from auto_moniter.directions import Directions
from auto_moniter.redaction import safe
from auto_moniter.storage import write_json

FILE = 'field-handoff.json'
EXECUTOR_RESULT = 'field-handoff-result.json'
EXECUTOR_REQUEST = 'field-handoff-request.json'
SHA256 = re.compile(r'[a-f0-9]{64}')
CHECK_ARCHIVE_CASE = 'watcher-check-archive'
FIELDS = {
    'observation': ('result',),
    'deployment': ('version', 'manifest_sha256', 'rollback', 'result'),
    'browser': ('version', 'session_reference', 'result'),
}

REQUEST_FIELDS = {'kind', 'title', 'owner', 'evidence_needed'}
REQUEST_SCHEMA = {'type': 'array', 'maxItems': 12, 'items': {
    'type': 'object', 'additionalProperties': False,
    'properties': {key: {'type': 'string'} for key in sorted(REQUEST_FIELDS)},
    'required': sorted(REQUEST_FIELDS),
}}
REQUEST_SCHEMA['items']['properties']['case_id'] = {'type': 'string'}
REQUEST_SCHEMA['items']['required'].append('case_id')
REQUEST_SCHEMA['items']['properties']['kind']['enum'] = list(FIELDS)
REQUEST_SCHEMA['items']['properties']['depends_on'] = {
    'type': 'array', 'maxItems': 11, 'items': {'type': 'string'},
    'description': 'Numbered earlier handoff items required by this item. Use [] for independent work.',
}
REQUEST_SCHEMA['items']['properties']['capability'] = {
    'type': 'string', 'description': 'Exact operator-registered executor capability, or empty.',
}
REQUEST_SCHEMA['items']['required'].extend(['depends_on', 'capability'])


def valid_dependencies(values, index):
    return (isinstance(values, list) and len(values) < index
            and all(isinstance(value, str) and value in {str(n) for n in range(1, index)} for value in values)
            and len(set(values)) == len(values))


def valid_capability(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-z][a-z0-9-]{0,63}', value))


def requests_valid(requests):
    return (isinstance(requests, list) and len(requests) <= 12
            and all(isinstance(row, dict) and REQUEST_FIELDS <= row.keys() <= REQUEST_FIELDS | {'case_id', 'depends_on', 'capability'}
                    and all(isinstance(v, str) and 0 < len(v.strip()) <= 300 and '\0' not in v
                            for k, v in row.items() if k in REQUEST_FIELDS)
                    and isinstance(row.get('case_id', ''), str)
                    and _valid_case(row['kind'], row.get('case_id', ''))
                    and row['kind'] in FIELDS
                    and ('depends_on' not in row or valid_dependencies(row['depends_on'], index))
                    and (row.get('capability', '') == '' or valid_capability(row['capability']))
                    for index, row in enumerate(requests, 1)))


def _valid_case(kind, case_id):
    return (not case_id or kind == 'observation' and case_id == CHECK_ARCHIVE_CASE
            or kind == 'browser' and bool(re.fullmatch(r'[a-z][a-z0-9-]{0,63}', case_id)))


def binding(state):
    task = (state.get('function_tasks') or {}).get('current') or {}
    rows = state.get('rounds')
    if not isinstance(rows, list):
        rows = state.get('records') or []
    worker = next((row for row in reversed(rows) if row.get('role') == 'worker'
                   and (row.get('watcher_result', {}).get('candidate_checks') or {}).get('context', {}).get('task') == task), {})
    review = next((row for row in reversed(rows)
                   if row.get('role') == 'reviewer' and row.get('index') == worker.get('index')), {})
    check = worker.get('watcher_result', {}).get('candidate_checks') or {}
    if (task.get('kind') != 'verification' or review.get('index') != worker.get('index')
            or not review.get('blockers') or review.get('task_status') != 'changes_required'
            or review.get('watcher_result', {}).get('status') != 'blocked'
            or check.get('context', {}).get('task') != task
            or not re.fullmatch(r'[a-f0-9]{64}', worker.get('tree', ''))):
        raise ValueError('当前没有等待现场回执的只读派单。')
    return {'task_id': task['id'], 'task_sha256': candidate_checks.digest(task),
            'round': worker['index'], 'tree': worker['tree'], 'checks_digest': check['digest'],
            'configured_checks': candidate_checks.digest(state.get('checks') or []),
            'directions': state.get('dispatch_directions') or []}


def read(run):
    path = run / FILE
    return Directions(run).read(FILE, 256 * 1024) if path.exists() or path.is_symlink() else {}


def pending_fallback(value):
    fallback = value.get('fallback') or {}
    return fallback if (fallback.get('key') and fallback.get('revision') == value.get('revision')
                        and fallback.get('status') != 'handled') else {}


def finish_fallback(run, fallback):
    value = read(run)
    active = pending_fallback(value)
    if not active or (active['key'], active['revision']) != (fallback['key'], fallback['revision']):
        return
    value['fallback'] = {**active, 'status': 'handled'}
    value['fallback_history'] = list(dict.fromkeys([*value.get('fallback_history', []), active['key']]))
    write_json(run / FILE, value, durable=True)


def activate(run, checkpoint):
    current = read(run)
    if current.get('binding') == checkpoint:
        return current
    archives = sorted(run.glob('field-handoff-*.json'), key=lambda path: path.stat().st_mtime_ns,
                      reverse=True)
    for path in archives:
        if not re.fullmatch(r'field-handoff-[a-f0-9]{32}\.json', path.name):
            continue
        value = Directions(run).read(path.name, 256 * 1024)
        if value.get('binding') == checkpoint and path.name == f"field-handoff-{value.get('revision')}.json":
            _archive(run, current)
            write_json(run / FILE, value, durable=True)
            return value
    return {}


def _archive(run, value):
    if value:
        archived = {key: entry for key, entry in value.items() if key != 'browser_inputs'}
        write_json(run / f"field-handoff-{value['revision']}.json", archived, durable=True)


def ready(value):
    return bool(value.get('items')) and all(item.get('receipt') for item in value['items'])


def archive_item(value):
    pending = [item for item in value.get('items', []) if not item.get('receipt')]
    if value.get('automatic') and len(pending) == 1:
        item = pending[0]
        if (item.get('kind') == 'observation' and item.get('case_id') == CHECK_ARCHIVE_CASE
                and not (item.get('claim') or item.get('executor_task'))):
            return item
    return None


def archive_checks(run, state, handoff, item, receipt):
    checkpoint = binding(state)
    context = receipt.get('context', {})
    if (checkpoint != handoff['binding'] or archive_item(handoff) != item
            or checkpoint['checks_digest'] != receipt.get('digest')
            or not receipt.get('passed') or not receipt.get('checks')
            or not candidate_checks.valid(receipt, run, context)):
        raise ValueError('归档需要与当前交接绑定且完整通过的 Watcher 检查记录。')
    archive = {'binding': checkpoint, 'revision': handoff['revision'], 'item_id': item['id'],
               'source': 'watcher_candidate_checks', 'cwd': state['cwd'],
               'business_head': state.get('git_history', {}).get('business_head'),
               'publication_tree': context.get('publication_tree'),
               'source_aliases': context.get('check_source_aliases', []),
               'check_inputs': (context.get('quality_policy') or {}).get('check_inputs', {}),
               'checks': receipt['checks'], 'logs': receipt['logs']}
    name = f"field-check-archive-{candidate_checks.digest(archive)}.json"
    write_json(run / name, safe(archive), durable=True)
    return submit(run, state, {'binding': checkpoint, 'revision': handoff['revision'], 'id': item['id'],
        'receipt': {'result': f'Watcher 已归档候选的正式检查结果。归档文件：{run / name}。'
                              f'回执摘要：{receipt["digest"]}。'
                              + '原始日志：' + '、'.join(f'check:{log}' for log in list(receipt['logs'])[:8])}})


def public(value):
    """The browser receives claim metadata, while the claim key stays with its holder."""
    if not value:
        return {}
    return {**value, 'items': [
        {**{key: ({k: v for k, v in field.items() if k != 'key_sha256'} if key == 'claim' else field)
            for key, field in row.items() if key != 'expected_resources'},
         **({'resources_expected': len(row['expected_resources'])} if 'expected_resources' in row else {}),
         'allow_manual': row.get('allow_manual', False)}
        for row in value['items']
    ]}


def matches(value, *, task, round_index, tree, checks_digest, directions):
    return ready(value) and matches_checkpoint(value, task=task, round_index=round_index, tree=tree,
                                               checks_digest=checks_digest, directions=directions)


def matches_checkpoint(value, *, task, round_index, tree, checks_digest, directions):
    checkpoint = value.get('binding') or {}
    return (checkpoint.get('round') == round_index
            and checkpoint.get('tree') == tree
            and checkpoint.get('task_sha256') == candidate_checks.digest(task)
            and checkpoint.get('checks_digest') == checks_digest
            and checkpoint.get('directions') == directions)


def rebind_after_check_retry(run, previous, current, context):
    """Keep submitted field evidence when only a failed check's result changes."""
    if (not previous or previous.get('passed') or previous.get('context') != context
            or previous.get('source_changed') or current.get('source_changed') or not current.get('complete')
            or not candidate_checks.valid(current, run, context)):
        return
    value = read(run)
    if not matches_checkpoint(
            value, task=context['task'], round_index=context['round'], tree=context['tree'],
            checks_digest=previous['digest'], directions=context['direction_ids']):
        return
    binding = value['binding']
    write_json(run / FILE, {**value,
               'submitted_checks_digest': value.get('submitted_checks_digest', binding['checks_digest']),
               'binding': {**binding, 'checks_digest': current['digest']}}, durable=True)


def citations(value):
    return {row['citation'] for row in review_evidence(value)}


def review_evidence(value):
    return [*value.get('retained_evidence', []),
            *[{**item, 'citation': f"handoff:{value['revision']}:{item['id']}"}
              for item in public(value)['items'] if item.get('receipt')]]


def prepare_executor(run, parent_id, value, item):
    """Persist the immutable request before launching its assigned executor."""
    request = {
        'parent_task': parent_id, 'binding': value['binding'], 'revision': value['revision'],
        'item': {key: item[key] for key in (
            'id', 'kind', 'title', 'evidence_needed', 'url', 'expected_target',
            'expected_version', 'expected_manifest_sha256', 'artifact_manifest', 'browser_proof', 'capability', 'executor_resource'
        ) if key in item},
    }
    existing = Directions(run).read(EXECUTOR_REQUEST)
    if existing and existing != request:
        raise ValueError('执行任务已绑定另一份现场交接，请为新交接关联独立执行任务。')
    if not existing:
        run.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_json(run / EXECUTOR_REQUEST, request, durable=True)


def executor_prompt(cwd, run):
    import json

    request = Directions(run).read(EXECUTOR_REQUEST)
    if not request:
        return ''
    item = request['item']
    path = cwd / EXECUTOR_RESULT
    extra = ''
    if item['kind'] == 'deployment':
        from auto_moniter.deployment import evidence_path

        path = evidence_path(cwd, run).with_name(EXECUTOR_RESULT)
        extra = '由部署报告的 verify 阶段在 evidence 中引用此文件。proof 填实际 target 和资源路径到 SHA-256 的映射。'
    elif item['kind'] == 'browser':
        extra = ('保留原标签页，proof 填同源 url、带时区的 observed_at 和 session_reference。'
                 '只有既有运行环境确实需要人工完成的操作才报告具体阻塞。')
    envelope = {key: request[key] for key in ('binding', 'revision')}
    envelope['id'] = item['id']
    return ('\n\nWatcher 已登记的现场交接输入如下。在原任务的授权范围内执行，并独立核对现场证据。'
            '恢复时优先核对已有结果，避免重复部署或覆盖原浏览器会话。'
            f'执行任务须将真实回执写入 {path}。{extra}'
            '保留以下 JSON 外层字段，在 receipt 填入所列字段。缺少证据时报告实际阻塞。\n'
            + json.dumps({'request': item, 'result_binding': envelope, 'receipt_fields': FIELDS[item['kind']]},
                         ensure_ascii=False))


def reopen(run, value, blockers, requests=()):
    _archive(run, value)
    items = requests or [
        {'kind': 'observation', 'title': reason[:300], 'owner': '现场执行器'}
        for reason in blockers[:12]
    ]
    rows = []
    for index, request in enumerate(items, 1):
        row = dict(safe(request), id=str(index))
        if row['kind'] == 'browser' and not row.get('case_id'):
            # Carry the operator's registration only for an unchanged observation.
            registered = [item for item in value['items']
                          if item.get('allow_manual') and not item.get('executor_task')
                          and all(item.get(key) == row.get(key)
                                  for key in REQUEST_FIELDS | {'depends_on', 'capability'})]
            if len(registered) == 1:
                item = registered[0]
                row.update({key: item[key] for key in ('allow_manual', 'url', 'expected_version', 'browser_proof')
                            if key in item})
        rows.append(row)
    updated = {**{key: field for key, field in value.items()
                  if key not in ('resume', 'dispatch', 'executor_status', 'fallback', 'failed_cases', 'browser_inputs')},
               'revision': uuid.uuid4().hex, 'review_feedback': safe(blockers),
               'retained_evidence': review_evidence(value)[-12:],
               'automatic': True,
               'items': rows}
    write_json(run / FILE, updated, durable=True)


def _text(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise ValueError('请填写完整的交接信息或现场结果。')
    return value.strip()


def _resources(value):
    if (not isinstance(value, dict) or not 1 <= len(value) <= 1000
            or any(not isinstance(path, str) or len(path) > 500 or '\\' in path
                   or any(part in ('', '.', '..') for part in path.split('/'))
                   or not isinstance(digest, str) or not SHA256.fullmatch(digest)
                   for path, digest in value.items())):
        raise ValueError('资源对账须提供相对文件路径及对应的 SHA-256。')
    return value


def configure(run, state, items, *, automatic=False, replace_revision=None, independent=()):
    checkpoint = binding(state)
    existing = read(run)
    if existing.get('binding') == checkpoint:
        if (automatic or any(row.get('receipt') or row.get('claim') for row in existing['items'])
                or not (existing.get('automatic') or replace_revision == existing.get('revision'))):
            raise ValueError('此派单已有交接待办，请继续补交原回执。')
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError('交接待办需要 1 至 12 项。')
    rows = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or item.get('kind') not in FIELDS:
            raise ValueError('交接类型无效。')
        kind = item['kind']
        row = {'id': str(index), 'kind': kind, 'title': _text(item.get('title'), 300),
               'owner': _text(item.get('owner'), 200)}
        if 'evidence_needed' in item:
            row['evidence_needed'] = _text(item['evidence_needed'], 300)
        if 'depends_on' in item:
            if not valid_dependencies(item['depends_on'], index):
                raise ValueError('交接依赖须引用编号更小的待办，且不得重复。')
            row['depends_on'] = list(item['depends_on'])
        if item.get('capability', '') != '':
            if not valid_capability(item['capability']):
                raise ValueError('执行能力编号无效。')
            row['capability'] = item['capability']
        for key in ('url', 'expected_version', 'expected_manifest_sha256', 'expected_target', 'executor_task', 'case_id',
                    'artifact_manifest'):
            if key in item and not (key == 'case_id' and item[key] == ''):
                row[key] = _text(item[key], 500)
        if not _valid_case(kind, row.get('case_id', '')):
            raise ValueError('交接用例编号无效。')
        if 'executor_task' in row and not re.fullmatch(r'[a-f0-9]{32}', row['executor_task']):
            raise ValueError('请指定有效的已登记执行任务编号。')
        if 'artifact_manifest' in row and (kind != 'deployment' or not row['artifact_manifest'].startswith('/')):
            raise ValueError('归档清单须使用部署交接的绝对路径。')
        if item.get('allow_manual') is True:
            row['allow_manual'] = True
        if 'url' in row:
            url = urlsplit(row['url'])
            if (kind != 'browser' or url.scheme not in ('http', 'https')
                    or not url.hostname or url.username or url.password
                    or url.query or url.fragment or '\\' in row['url']
                    or any(ch.isspace() or ord(ch) < 32 for ch in row['url'])):
                raise ValueError('请提供无凭据的正常浏览器入口。')
        expected = row.get('expected_manifest_sha256')
        if expected and (kind != 'deployment' or not SHA256.fullmatch(expected)):
            raise ValueError('归档候选的 SHA-256 无效。')
        if 'expected_resources' in item:
            if kind != 'deployment' or not row.get('expected_target'):
                raise ValueError('部署资源对账须绑定目标地址。')
            row['expected_resources'] = _resources(item['expected_resources'])
        if item.get('browser_proof') is True:
            if kind != 'browser' or not row.get('url'):
                raise ValueError('浏览器观察记录须绑定现场入口。')
            row['browser_proof'] = True
        rows.append(row)
    value = safe({'binding': checkpoint, 'items': rows, 'automatic': automatic, 'revision': uuid.uuid4().hex,
                  'independent_tasks': list(independent)})
    if existing.get('binding') == checkpoint and existing.get('retained_evidence'):
        value['retained_evidence'] = existing['retained_evidence']
    if existing.get('binding') == checkpoint and existing.get('fallback_history'):
        value['fallback_history'] = existing['fallback_history']
    _archive(run, existing)
    write_json(run / FILE, value, durable=True)
    return value


def _select(run, state, data):
    value = read(run)
    if (data.get('revision') != value.get('revision') or data.get('binding') != value.get('binding')
            or binding(state) != value.get('binding')):
        raise ValueError('交接版本已变化，请刷新后核对当前候选。')
    item = next((row for row in value['items'] if row['id'] == data.get('id')), None)
    if item is None:
        raise ValueError('找不到这项现场交接。')
    return value, item


def claim(run, state, data):
    value, item = _select(run, state, data)
    if item.get('receipt') or item.get('claim'):
        raise ValueError('这项交接已认领或已提交。')
    _require_dependencies(value, item)
    key = secrets.token_urlsafe(32)
    item['claim'] = {'name': safe(_text(data.get('name'), 100)),
                     'key_sha256': hashlib.sha256(key.encode()).hexdigest()}
    write_json(run / FILE, value, durable=True)
    return {**public(value), 'claim_key': key}


def bind_browser_case(run, state, data, config):
    value, item = _select(run, state, data)
    case_id = data['case_id']
    if (item['kind'] != 'browser' or item.get('receipt') or item.get('claim')
            or item.get('executor_task') or item.get('case_id') not in (None, '', case_id)):
        raise ValueError('只可为尚未认领或提交的浏览器待办绑定用例。')
    if (item.get('url', config['url']) != config['url']
            or item.get('expected_version', config['expected_version']) != config['expected_version']):
        raise ValueError('浏览器用例的现场入口或版本与原待办不一致。')
    item.update(case_id=case_id, url=config['url'], expected_version=config['expected_version'],
                browser_proof=True)
    write_json(run / FILE, value, durable=True)
    return value


def release(run, state, data):
    value, item = _select(run, state, data)
    if item.get('receipt') or not item.get('claim'):
        raise ValueError('只有尚未交回执的认领才可撤销。')
    _check_claim(item, data)
    del item['claim']
    write_json(run / FILE, value, durable=True)
    return public(value)


def dependency_error(value, item):
    if 'depends_on' in item:
        received = {row['id'] for row in value['items'] if row.get('receipt')}
        missing = [ident for ident in item['depends_on'] if ident not in received]
        return '等待同一候选的前置回执：' + '、'.join(missing) if missing else ''
    if item['kind'] == 'browser' and any(
            row['kind'] == 'deployment' and not row.get('receipt') for row in value['items']):
        return '等待同一候选的部署回执。'
    return ''


def _require_dependencies(value, item):
    error = dependency_error(value, item)
    if error:
        raise ValueError(error if 'depends_on' in item else '请先提交同一候选的部署回执，再进行浏览器取证。')


def _check_claim(item, data):
    if item.get('claim'):
        key = data.get('claim_key')
        if (not isinstance(key, str) or len(key) > 100
                or not secrets.compare_digest(hashlib.sha256(key.encode()).hexdigest(), item['claim']['key_sha256'])):
            raise ValueError('请由认领人使用原认领凭据处理交接。')


def _proof(item, data):
    value = data.get('proof')
    if 'expected_resources' in item:
        if (not isinstance(value, dict) or set(value) != {'target', 'resources'}
                or value['target'] != item['expected_target']):
            raise ValueError('请提交绑定目标的现场资源对账。')
        actual = _resources(value['resources'])
        if actual != item['expected_resources']:
            raise ValueError('现场资源存在缺失、额外文件或哈希不一致。')
        return {'target': value['target'], 'resources_verified': len(actual),
                'resources_sha256': candidate_checks.digest(actual)}
    if item.get('browser_proof'):
        if (not isinstance(value, dict) or set(value) != {'url', 'observed_at', 'session_reference'}
                or value['session_reference'] != data['receipt']['session_reference']):
            raise ValueError('请提交同一标签页的 URL、观察时间和会话引用。')
        url = urlsplit(_text(value['url'], 500))
        expected = urlsplit(item['url'])
        if ((url.scheme, url.hostname, url.port) != (expected.scheme, expected.hostname, expected.port)
                or url.username or url.password or url.query or url.fragment or '\\' in value['url']):
            raise ValueError('浏览器观察地址与已登记入口不同源。')
        try:
            observed = datetime.fromisoformat(_text(value['observed_at'], 60))
            if observed.tzinfo is None or abs((datetime.now(timezone.utc) - observed).total_seconds()) > 86400:
                raise ValueError()
        except ValueError:
            raise ValueError('浏览器观察时间须含时区，且在最近 24 小时内。') from None
        return {'url': value['url'], 'observed_at': observed.isoformat(),
                'session_reference': value['session_reference']}
    return None


def submit(run, state, data, *, executor=None):
    value, item = _select(run, state, data)
    fields = data.get('receipt')
    if not isinstance(fields, dict) or set(fields) != set(FIELDS[item['kind']]):
        raise ValueError('回执字段与待办要求不一致。')
    _require_dependencies(value, item)
    if executor is not None:
        if executor != item.get('executor_task') and not (
                not item.get('executor_task') and item.get('case_id') and executor == 'browser:' + item['case_id']):
            raise ValueError('回执来自未关联的执行任务。')
    else:
        _check_claim(item, data)
    receipt = safe({key: _text(fields[key]) for key in FIELDS[item['kind']]})
    if item.get('expected_version', receipt.get('version')) != receipt.get('version'):
        raise ValueError('实际版本与归档候选不一致。')
    digest = receipt.get('manifest_sha256')
    if digest is not None and (not re.fullmatch(r'[a-f0-9]{64}', digest)
                               or item.get('expected_manifest_sha256', digest) != digest):
        raise ValueError('部署清单的 SHA-256 与归档候选不一致。')
    proof = _proof(item, data)
    if proof is not None:
        receipt['proof'] = safe(proof)
    if executor is not None:
        receipt['executor_task'] = executor
    if item.get('receipt') and item['receipt'] != receipt:
        raise ValueError('这一项已提交，请核对已保存的回执。')
    item['receipt'] = receipt
    if ready(value):
        value.setdefault('resume', {'status': 'pending', 'agent_calls': (state.get('usage') or {}).get('agent_calls', 0)})
    write_json(run / FILE, value, durable=True)
    return value


def update_resume(run, value, status, **details):
    updated = {**value, 'resume': {'status': status, **details}}
    write_json(run / FILE, updated, durable=True)
    return updated
