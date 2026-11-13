"""Read-only paginated view of the watcher's approved function plan."""

from collections import Counter

from auto_moniter.function_plan import restore, waiting
from auto_moniter.function_tasks import FunctionTasks
from auto_moniter.function_gate import requirement_criteria
from auto_moniter.redaction import safe


def page(state, number=1, size=10, status='all'):
    if type(number) is not int or number < 1 or type(size) is not int or not 1 <= size <= 100:
        raise ValueError('派单页码必须大于 0，每页数量必须为 1–100')
    if status not in {'all', 'accepted', 'active', 'waiting'}:
        raise ValueError('派单状态筛选无效')
    ledger = FunctionTasks(state.get('function_tasks'))
    plan = restore(state.get('function_plan'))
    enabled = bool(state.get('function_dispatch'))
    ready = bool(plan and plan['cycle'] == len(state.get('requirement_cycles', [])) + 1)
    criteria = {}
    for module in state.get('modules', []):
        criteria.update({f"{module['id']}:{index}":text for index, text in enumerate(module.get('acceptance', []), 1)})
    for cycle in state.get('requirement_cycles', []):
        criteria.update(requirement_criteria(cycle['requirements_review']))
    criteria.update(requirement_criteria(state.get('requirements_review') or {}))
    rows = [{'task':item['task'], 'status':'accepted', 'accepted_round':item['round'],
             'tree':item['tree'], 'evidence':item['evidence'], 'verification':item.get('verification', {})}
            for item in ledger.accepted]
    if ledger.current:
        rows.append({'task':ledger.current, 'status':'active', 'run_status':state.get('status')})
    if ready:
        rows.extend({'task':task, 'status':'waiting'} for task in waiting(plan, ledger))
    for index, row in enumerate(rows, 1):
        row['position'] = index
        row['criteria'] = [{'id':key, 'text':criteria.get(key, '')} for key in row['task'].get('criterion_ids', [])]
    counts = {key:0 for key in ('accepted', 'active', 'waiting')}
    counts.update(Counter(row['status'] for row in rows))
    counts['all'] = len(rows)
    filtered = rows if status == 'all' else [row for row in rows if row['status'] == status]
    total = len(filtered)
    pages = max(1, (total + size - 1) // size)
    number = min(number, pages)
    start = (number - 1) * size
    review_status = (state.get('requirements_review') or {}).get('status')
    planning = ('disabled' if not enabled else 'not_generated' if not ready else
                'needs_review' if review_status in {'reviewing', 'failed', 'needs_input'}
                or state.get('planning_rejection') else 'ready')
    return safe({'items':filtered[start:start + size], 'page':number, 'pages':pages, 'page_size':size,
                 'total':total, 'start':start, 'end':min(start + size, total), 'counts':counts,
                 'filter':status, 'plan_state':planning, 'complete':ready,
                 'full_plan_enabled':bool(state.get('full_function_plan')),
                 'cycle':plan['cycle'] if ready else None})
