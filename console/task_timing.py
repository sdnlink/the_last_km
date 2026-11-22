"""Estimate the current development plan from the task's recorded pace."""

import math

from auto_moniter.function_plan import restore, waiting
from auto_moniter.function_tasks import FunctionTasks


def estimate(state, status, elapsed_s):
    if status == 'done':
        return {'status': 'complete'}
    if status in {'cancelled', 'failed', 'invalid', 'max_rounds'}:
        return {'status': 'unavailable'}

    try:
        plan = restore(state.get('function_plan'))
        ledger = FunctionTasks(state.get('function_tasks'))
    except (TypeError, ValueError):
        return {'status': 'plan_pending'}

    if (not plan or not ledger.current or ledger.current not in plan['tasks']
            or plan['cycle'] != len(state.get('requirement_cycles') or []) + 1
            or (state.get('requirements_review') or {}).get('status') != 'ready'
            or state.get('planning_rejection')):
        return {'status': 'plan_pending'}

    completed = len(ledger.accepted)
    remaining = 1 + len(waiting(plan, ledger))
    counts = {'accepted_assignments': completed, 'remaining_assignments': remaining}
    if completed < 3 or type(elapsed_s) not in (int, float) or not 0 < elapsed_s <= 1e15:
        return {'status': 'insufficient_history', **counts}

    remaining_s = math.ceil(elapsed_s / completed * remaining)
    return {'status': 'estimated', **counts, 'remaining_s': remaining_s,
            'total_s': math.ceil(elapsed_s + remaining_s)}
