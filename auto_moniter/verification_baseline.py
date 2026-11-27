"""Keep the pre-dispatch source identity for each read-only assignment."""

import gzip
import json

from auto_moniter import candidate_checks, function_gate, function_tasks
from auto_moniter.directions import Directions
from auto_moniter.storage import write_json
from auto_moniter.workspace import snapshot_fingerprint


def prepare(run, root, task, snapshot, *, cycle, after_round, started, records):
    """Rebuild the in-memory snapshot only when it matches the saved boundary."""
    name = f"verification-baseline-{cycle}-{after_round}-{task['id']}.json"
    directions = Directions(run)
    saved = directions.read(name, 4096)
    tree = snapshot_fingerprint(root, snapshot)
    expected = {'version': 1, 'cwd': str(root), 'tree': tree}
    recovered = False
    if (saved or (run / name).exists()) and saved != expected:
        raise ValueError('只读派单 ' + task['id'] + ' 的工作区与其原始候选不一致。'
                         '请恢复原候选，或由 Reviewer 为当前候选重新安排验收。')
    if not saved and started:
        if not (_matches_legacy_baseline(run, root, task, tree)
                or _matches_dispatch_history(run, task, tree, cycle, after_round, records)):
            raise ValueError('只读派单 ' + task['id'] + ' 缺少范围基线，现有记录无法确认原候选。'
                             '请找回原基线或由 Reviewer 为当前候选重新安排验收。')
        recovered = True
    if not saved:
        function_gate.verify_read_only_target(root, task)
        write_json(run / name, expected, durable=True)
    return {'id': task['id'], 'files': snapshot}, recovered


def _matches_legacy_baseline(run, root, task, tree):
    path = run / 'function-baseline.json.gz'
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            saved = json.load(handle)
        return saved['id'] == task['id'] and snapshot_fingerprint(root, saved['files']) == tree
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        return False


def _matches_dispatch_history(run, task, tree, cycle, after_round, records):
    """Recover legacy read-only baselines from independent pre/post turn evidence."""
    rounds = set()
    for record in records:
        if (record.role != 'worker' or record.verdict.task_id != task['id']
                or record.index <= after_round or record.tree != tree
                or record.failure or record.interruption or record.exit_code or record.timed_out):
            continue
        receipt = record.watcher_result.get('candidate_checks') or {}
        context = receipt.get('context') or {}
        original = context.get('task') or {}
        if (original.get('kind') == 'verification' and original.get('id') == task['id']
                and function_tasks.targets(original) == function_tasks.targets(task)
                and context.get('round') == record.index and context.get('tree') == tree
                and context.get('requirement_cycle') == cycle and not receipt.get('source_changed')
                and candidate_checks.valid(receipt, run, context)):
            rounds.add(str(record.index))
    if not rounds:
        return False
    for name in ('events.jsonl', 'events.previous.jsonl'):
        path = run / name
        if not path.is_file() or path.is_symlink():
            continue
        with path.open(encoding='utf-8', errors='replace') as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(event, dict) and event.get('event') == 'function_dispatch_validated'
                        and event.get('task_id') == task['id'] and str(event.get('round')) in rounds
                        and event.get('tree') == tree):
                    return True
    return False
