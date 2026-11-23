"""Reconstruct saved acceptance configurations from explicit console edits."""


def configurations(checks, changes):
    if not isinstance(checks, list) or any(not isinstance(command, str) for command in checks):
        raise ValueError('Invalid saved acceptance checks')
    if not isinstance(changes, list) or len(changes) > 20:
        raise ValueError('Invalid saved acceptance check history')
    current = list(checks)
    history = [current]
    for change in reversed(changes):
        if (not isinstance(change, dict) or not {'at', 'added'} <= change.keys()
                or change.keys() - {'at', 'added', 'removed', 'target'}
                or not isinstance(change['at'], str) or len(change['at']) > 100):
            raise ValueError('Invalid saved acceptance check history')
        target = change.get('target')
        if target is not None and (not isinstance(target, dict)
                or set(target) != {'task_id', 'criterion_ids', 'tree', 'cycle'}
                or not isinstance(target['task_id'], str) or not isinstance(target['tree'], str)
                or type(target['cycle']) is not int or target['cycle'] < 1
                or not isinstance(target['criterion_ids'], list)
                or any(not isinstance(item, str) for item in target['criterion_ids'])):
            raise ValueError('Invalid saved acceptance check target')
        added = change['added']
        removed = change.get('removed', [])
        if (not isinstance(added, list) or not isinstance(removed, list)
                or len(added) > 20 or len(removed) > 20 or not (added or removed)
                or any(not _command(command) for command in added)
                or (added and current[-len(added):] != added)):
            raise ValueError('Invalid saved acceptance check history')
        current = current[:-len(added)] if added else current.copy()
        previous_index = -1
        for item in removed:
            if (not isinstance(item, dict) or set(item) != {'index', 'command'}
                    or type(item['index']) is not int or not previous_index < item['index'] <= len(current)
                    or not _command(item['command']) or item['command'] in current):
                raise ValueError('Invalid saved acceptance check removal')
            previous_index = item['index']
            current.insert(previous_index, item['command'])
        history.append(current)
    original = history[-1]
    if any(commands[:len(original)] != original for commands in history):
        raise ValueError('Original acceptance checks must be preserved')
    return history[::-1]


def _command(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000 and '\0' not in value
