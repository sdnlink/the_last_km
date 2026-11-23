"""Validate a linked repository's completed Watcher delivery."""

import json
import re
from pathlib import Path

from auto_moniter import candidate_checks, function_tasks
from auto_moniter.directions import Directions
from auto_moniter.project_workspace import git
from auto_moniter.workspace import workspace_fingerprint


CONTRACT_FIELDS = ('module_id', 'kind', 'delivery_scope', 'objective', 'inputs', 'outputs',
                   'boundaries', 'verification', 'edit_files', 'test_files', 'review_test')


def same_contract(approved, proposed):
    return (all(approved.get(key) == proposed.get(key) for key in CONTRACT_FIELDS)
            and function_tasks.targets(approved) == function_tasks.targets(proposed))


def contracts(value):
    if not isinstance(value, list) or len(value) > function_tasks.MAX_PLAN_TASKS:
        raise ValueError('跨仓派单合同必须是有效的派单列表。')
    tasks = [function_tasks.validate(item) for item in value]
    result = {item['id']: item for item in tasks}
    if len(result) != len(tasks) or any(item['delivery_scope'] == 'requirement' for item in tasks):
        raise ValueError('跨仓派单合同必须使用唯一编号，需求总验收由原任务负责。')
    return result


def preserve(approved, proposed):
    if not isinstance(proposed, dict):
        return proposed
    ident = proposed.get('id')
    original = approved.get(ident) if isinstance(ident, str) else None
    if original is None:
        return proposed
    # The receiving reviewer selects local checks and criterion IDs. The sender
    # owns the exact scope, expected behavior and approved test bytes.
    return {**original, **{key: proposed[key] for key in ('criterion_ids', 'check_ids', 'depends_on')
                           if key in proposed}}


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


def _reverified(workspace, delivered, receipt, final_receipt, module, tree, ledger, local_review):
    """Require the original behavioral check to pass again on the final candidate."""
    from auto_moniter.local_tests import command
    from auto_moniter.redaction import redact

    task = delivered['task']
    context = final_receipt['context']
    if (module.get('status') != 'accepted' or module.get('tree') != tree
            or not context.get('final') or context.get('tree') != tree):
        return False
    original = {check['command'] for check in receipt['checks'] if check['status'] == 'passed'}
    passing = {check['command'] for check in final_receipt['checks'] if check['status'] == 'passed'}
    if not original <= passing:
        return False
    paths = function_tasks.writable_files(task)
    if (git(workspace, '--literal-pathspecs', 'diff', '--name-only', 'HEAD', '--', *paths)
            or git(workspace, '--literal-pathspecs', 'ls-files', '--others', '--', *paths)):
        return False
    baseline = local_review.get('git_checkpoint', {}).get('business_commit', {}).get('commit', '')
    if not re.fullmatch(r'[0-9a-f]{40,64}', baseline):
        return False
    selected = {check['command'] for check in receipt['checks']
                if check['log'] in delivered['verification'].values()}
    related = [delivered] if task.get('kind') != 'verification' else [
        item for item in ledger.accepted if item['round'] <= delivered['round']
        and item['task']['module_id'] == task['module_id']]
    tests = [item['task']['review_test']['path'] for item in related if item['task'].get('review_test')]
    focused = [name for name in tests if (runner := command(workspace, name)) and redact(runner) in selected]
    if not focused:
        return False
    return all(git(workspace, '--literal-pathspecs', 'ls-tree', baseline, '--', name)
               and not git(workspace, '--literal-pathspecs', 'diff', '--name-only', baseline, '--', name)
               for name in focused)


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
    if not same_contract(assignment, delivered['task']):
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
    verified_text = {actual[key] for key in delivered['task']['criterion_ids'] if key in actual}
    if not requested <= expected.keys() or any(expected[key] not in verified_text for key in requested):
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
        if not _reverified(workspace, delivered, receipt, final_receipt, child_module, tree, ledger, local_review):
            raise ValueError('关联任务通过检查的代码与当前版本不一致，最终验收缺少对原专项测试的复验。')
    return {'run_dir': str(run), 'workspace': str(workspace), 'commit': commit,
            'tree': tree, 'checks_digest': receipt['digest']}
