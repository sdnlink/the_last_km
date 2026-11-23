"""Validate a linked repository's completed Watcher delivery."""

import json
import re
from pathlib import Path

from auto_moniter import candidate_checks, function_tasks
from auto_moniter.directions import Directions
from auto_moniter.project_workspace import git
from auto_moniter.workspace import workspace_fingerprint


def links(values):
    if not isinstance(values, list) or len(values) > 200:
        raise ValueError('跨仓关联须为列表，最多 200 条。')
    result = {}
    for value in values:
        if not isinstance(value, str) or len(value) > 4096 or '\0' in value or '=' not in value:
            raise ValueError('跨仓关联应填写为「派单编号=运行目录绝对路径」。')
        ident, name = value.split('=', 1)
        if not function_tasks.identifier(ident) or ident in result or not Path(name).is_absolute():
            raise ValueError('跨仓关联需要互不重复的派单编号和运行目录绝对路径。')
        result[ident] = Path(name).resolve()
    return result


def _recorded_approval(workspace, row, history):
    checkpoint = row.get('git_checkpoint') or {}
    ref, oid = checkpoint.get('ref', ''), checkpoint.get('commit', '')
    name = checkpoint.get('record', '')
    if (ref != history.get('ref') or not ref.startswith('refs/auto-moniter/')
            or not re.fullmatch(r'[0-9a-f]{40,64}', oid)
            or not re.fullmatch(r'\.auto-moniter/history/[a-f0-9]+/[0-9]+-reviewer\.json', name)
            or git(workspace, '--no-replace-objects', 'merge-base', oid, ref, check=False) != oid):
        return None
    record = json.loads(git(workspace, '--no-replace-objects', 'show', oid + ':' + name))
    if record.get('round') != row.get('index') or record.get('role') != 'reviewer':
        return None
    return (record.get('result') or {}).get('watcher_result', {}).get('candidate_approval')


def verified(run, workspace, assignment, module, brief):
    """Require the target, behavior contract, receipt and current Git version."""
    state = Directions(run).read('state.json', 32 * 1024 * 1024)
    if state.get('cwd') != str(workspace):
        raise ValueError('关联任务的工作区与派单仓库不一致。')
    if state.get('status') != 'done':
        return None
    report = Directions(run).read('report.json', 32 * 1024 * 1024)
    history = state.get('git_history') or {}
    publication = history.get('last_business_commit') or {}
    commit = publication.get('commit', '')
    tree = report.get('accepted_tree', '')
    if (report.get('status') != 'done' or not re.fullmatch(r'[0-9a-f]{40,64}', commit)
            or not re.fullmatch(r'[0-9a-f]{64}', tree)
            or state.get('tree') != tree
            or git(workspace, 'rev-parse', 'HEAD', check=False) != commit
            or git(workspace, 'rev-parse', '--show-toplevel', check=False) != str(workspace)
            or workspace_fingerprint(workspace, ignore_dir=run) != tree):
        raise ValueError('关联任务的交付版本与当前仓库不一致。')
    ledger = function_tasks.FunctionTasks(state.get('function_tasks'))
    delivered = next((item for item in ledger.accepted if item['task']['id'] == assignment['id']), None)
    if not delivered or not delivered.get('verification'):
        raise ValueError('关联任务没有这个派单的验收记录。')
    contract = ('module_id', 'kind', 'delivery_scope', 'objective', 'inputs', 'outputs',
                'boundaries', 'verification', 'edit_files', 'test_files', 'review_test')
    if (any(delivered['task'].get(key) != assignment.get(key) for key in contract)
            or function_tasks.targets(delivered['task']) != function_tasks.targets(assignment)):
        raise ValueError('关联任务的派单目标或验收要求与原派单不一致。')
    child_module = next((item for item in state.get('modules', []) if item['id'] == assignment['module_id']), {})
    if not child_module:
        raise ValueError('关联任务没有登记对应模块。')
    if assignment['delivery_scope'] == 'module' and (
            child_module.get('status') != 'accepted' or child_module.get('acceptance') != module['acceptance']):
        raise ValueError('关联任务没有通过原派单所要求的模块验收。')
    from auto_moniter import function_gate
    expected = function_gate.known_criteria({module['id']: module}, brief)
    actual = function_gate.known_criteria({module['id']: child_module}, state.get('requirements_review') or {})
    requested = set(assignment['criterion_ids'])
    if (not requested <= set(delivered['task']['criterion_ids']) or not requested <= expected.keys()
            or any(expected[key] != actual.get(key) for key in requested)):
        raise ValueError('关联任务的验收条目与原派单不一致。')
    records = state.get('rounds', [])
    final = next((row for row in reversed(records) if row.get('role') == 'reviewer'
                  and row.get('index') == publication.get('round') and row.get('tree') == tree), {})
    approval = final.get('watcher_result', {}).get('candidate_approval') or {}
    worker = next((row for row in reversed(records) if row.get('role') == 'worker'
                   and row.get('index') == delivered['round'] and row.get('tree') == delivered['tree']), {})
    receipt = worker.get('watcher_result', {}).get('candidate_checks') or {}
    final_worker = next((row for row in reversed(records) if row.get('role') == 'worker'
                         and row.get('index') == publication.get('round') and row.get('tree') == tree), {})
    final_receipt = final_worker.get('watcher_result', {}).get('candidate_checks') or {}
    local_review = next((row for row in reversed(records) if row.get('role') == 'reviewer'
                         and row.get('index') == delivered['round'] and row.get('tree') == delivered['tree']), {})
    local_approval = (local_review.get('watcher_result', {}).get('candidate_approval') or {})
    if (not approval or approval.get('invalidated') or final.get('delivery', {}).get('status') != 'accepted'
            or approval != _recorded_approval(workspace, final, history)
            or not local_approval or local_approval.get('invalidated')
            or local_approval != _recorded_approval(workspace, local_review, history)
            or final.get('git_checkpoint', {}).get('business_commit') != publication
            or not final_receipt.get('passed')
            or not candidate_checks.valid(final_receipt, run, final_receipt.get('context'))
            or approval.get('checks_digest') != final_receipt['digest']
            or approval.get('context') != final_receipt['context']
            or local_review.get('task_status') != 'accepted'
            or local_review.get('delivery', {}).get('status') != 'accepted'
            or local_approval.get('checks_digest') != receipt.get('digest')
            or not receipt.get('passed') or not candidate_checks.valid(receipt, run, receipt.get('context'))
            or (receipt.get('context') or {}).get('task') != delivered['task']
            or not set(delivered['verification'].values()) <= receipt['logs'].keys()):
        raise ValueError('关联任务的 Watcher 检查记录缺失或已失效。')
    from auto_moniter.function_gate import target_state
    from auto_moniter.workspace import _file_token
    hashes = delivered.get('target_hashes') or {delivered['task']['file']: delivered.get('symbol_hash')}
    def file_hash(file):
        path = workspace / file
        return _file_token(path) if path.exists() or path.is_symlink() else None
    if (any(target_state(workspace / file, symbol)['body'] != hashes.get(file)
            for file, symbol in function_tasks.targets(assignment))
            or any(file_hash(file) != digest
                   for file, digest in delivered.get('file_hashes', {}).items())):
        raise ValueError('关联任务通过检查的代码与当前版本不一致。')
    return {'run_dir': str(run), 'workspace': str(workspace), 'commit': commit,
            'tree': tree, 'checks_digest': receipt['digest']}
