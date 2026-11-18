"""A reviewer-authored backlog; only the active assignment authorizes execution."""

import copy
import json

from auto_moniter import function_tasks
from auto_moniter.function_gate import criteria, requirement_criteria, known_criteria

MAX_TASKS = 200


def validate(value, ledger, modules, brief, *, head=None):
    if not isinstance(value, list) or not value or len(value) + len(ledger.accepted) > MAX_TASKS:
        raise ValueError('Complete function plan must contain 1–200 remaining assignments within the run limit')
    if len(json.dumps(value, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise ValueError('Complete function plan exceeds 2 MiB')
    tasks = [function_tasks.validate(item, modules) for item in value]
    if head is not None and tasks[0] != head:
        raise ValueError('Complete function plan must start with the exact current/next assignment')
    seen = {item['task']['id'] for item in ledger.accepted}
    covered = {key for item in ledger.accepted for key in item['task'].get('criterion_ids', [])}
    problems = []
    known = known_criteria(modules,brief)
    for index, task in enumerate(tasks):
        if task['id'] in seen or not set(task['depends_on']) <= seen:
            problems.append('Complete function plan has duplicate IDs, cycles or forward/unaccepted dependencies: ' + task['id'])
        if task['delivery_scope'] == 'requirement' and index != len(tasks) - 1:
            problems.append('Requirement delivery must be the final assignment in the complete plan: ' + task['id'])
        ids = set(task.get('criterion_ids',[]))
        unknown = ids - known.keys()
        if not ids or unknown:
            problems.append('Complete function plan must reference known acceptance criteria: ' + task['id']
                            + ('; unknown IDs: ' + ', '.join(sorted(unknown)) if unknown else ''))
        if not ids & criteria(modules,task['module_id']).keys():
            problems.append('Complete function plan must reference its own module criteria: ' + task['id'])
        seen.add(task['id'])
        covered.update(task['criterion_ids'])
    if tasks[-1]['delivery_scope'] != 'requirement':
        problems.append('Complete function plan must end with requirement verification/delivery')
    required = set(requirement_criteria(brief)) | {key for module in modules for key in criteria(modules, module)}
    if required - covered:
        problems.append('Complete function plan leaves acceptance criteria unplanned: ' + ', '.join(sorted(required - covered)))
    if problems:
        raise ValueError('; '.join(problems)[:12000])
    return {'version': 1, 'cycle': brief.get('cycle', 1), 'tasks': tasks}


def restore(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'version', 'cycle', 'tasks'} or value['version'] != 1
            or type(value['cycle']) is not int or value['cycle'] < 1
            or not isinstance(value['tasks'], list) or not 1 <= len(value['tasks']) <= MAX_TASKS
            or len(json.dumps(value, ensure_ascii=False).encode()) > 2 * 1024 * 1024):
        raise ValueError('Invalid saved complete function plan')
    tasks = [function_tasks.validate(item) for item in value['tasks']]
    if len({task['id'] for task in tasks}) != len(tasks):
        raise ValueError('Saved complete function plan repeats assignment IDs')
    return {**value, 'tasks': tasks}


def waiting(plan, ledger):
    if plan is None:
        return []
    claimed = {item['task']['id'] for item in ledger.accepted}
    if ledger.current:
        claimed.add(ledger.current['id'])
    return [copy.deepcopy(task) for task in plan['tasks'] if task['id'] not in claimed]


def planning_contract():
    return ('\nBefore implementation, return function_plan: the COMPLETE ordered list of remaining function assignments '
            'through final requirement verification, including the exact current assignment first when one is unfinished. '
            'Set next_task to function_plan[0]. Each entry needs the same precise fields, focused checks, criteria and '
            'boundaries as next_task. Use unique IDs and only dependencies accepted already or appearing earlier in this plan. '
            'Cover every original module and requirement criterion; conclude with delivery_scope=requirement. '
            'A module delivery requires its prerequisite modules to have been accepted; accepted implementation '
            'functions alone do not confirm a module. Include prerequisite module verification milestones first. '
            'Later source edits can invalidate earlier module acceptance; schedule module verification after '
            'the implementation it must cover. '
            'Each assignment must cite its own module criterion and may also cite known criteria from other modules '
            'when its behavior integrates them; these references grant no additional edit scope or check commands. '
            'Preserve accepted work and the current assignment identity. Do not execute the backlog or bundle it into one worker turn. '
            'Future entries are a plan, not acceptance; the watcher revalidates each entry before dispatch. '
            'For needs_input return next_task=null and function_plan=[].\n')
