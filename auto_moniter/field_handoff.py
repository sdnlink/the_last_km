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
REQUEST_SCHEMA['items']['properties']['kind']['enum'] = list(FIELDS)


def requests_valid(requests):
    return (isinstance(requests, list) and len(requests) <= 12
            and all(isinstance(row, dict) and set(row) == REQUEST_FIELDS
                    and all(isinstance(v, str) and 0 < len(v.strip()) <= 300 and '\0' not in v
                            for v in row.values())
                    and row['kind'] in FIELDS for row in requests))


def binding(state):
    task = (state.get('function_tasks') or {}).get('current') or {}
    rows = state.get('rounds')
    if not isinstance(rows, list):
        rows = state.get('records') or []
    worker = next((row for row in reversed(rows) if row.get('role') == 'worker'), {})
    review = next((row for row in reversed(rows) if row.get('role') == 'reviewer'), {})
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


def ready(value):
    return bool(value.get('items')) and all(item.get('receipt') for item in value['items'])


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
    checkpoint = value.get('binding') or {}
    return (ready(value) and checkpoint.get('round') == round_index
            and checkpoint.get('tree') == tree
            and checkpoint.get('task_sha256') == candidate_checks.digest(task)
            and checkpoint.get('checks_digest') == checks_digest
            and checkpoint.get('directions') == directions)


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
            'expected_version', 'expected_manifest_sha256', 'artifact_manifest', 'browser_proof'
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
    write_json(run / f"field-handoff-{value['revision']}.json", value, durable=True)
    items = requests or [
        {'kind': 'observation', 'title': reason[:300], 'owner': '现场执行器'}
        for reason in blockers[:12]
    ]
    updated = {**{key: field for key, field in value.items()
                  if key not in ('resume', 'dispatch', 'executor_status')},
               'revision': uuid.uuid4().hex, 'review_feedback': safe(blockers),
               'retained_evidence': review_evidence(value)[-12:],
               'automatic': True,
               'items': [dict(safe(row), id=str(index)) for index, row in enumerate(items, 1)]}
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
        for key in ('url', 'expected_version', 'expected_manifest_sha256', 'expected_target', 'executor_task',
                    'artifact_manifest'):
            if key in item:
                row[key] = _text(item[key], 500)
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


def release(run, state, data):
    value, item = _select(run, state, data)
    if item.get('receipt') or not item.get('claim'):
        raise ValueError('只有尚未交回执的认领才可撤销。')
    _check_claim(item, data)
    del item['claim']
    write_json(run / FILE, value, durable=True)
    return public(value)


def _require_dependencies(value, item):
    if item['kind'] == 'browser' and any(
            row['kind'] == 'deployment' and not row.get('receipt') for row in value['items']):
        raise ValueError('请先提交同一候选的部署回执，再进行浏览器取证。')


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
        if executor != item.get('executor_task'):
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
