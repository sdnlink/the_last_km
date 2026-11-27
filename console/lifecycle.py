"""Resume pending task lifecycle actions after a control-plane restart."""

from pathlib import Path

from auto_moniter import dispatch_inputs, recovery_wait
from auto_moniter.control import read_control
from auto_moniter.directions import Directions
from auto_moniter.redaction import redact
from auto_moniter.storage import write_json


def presentation(run, status, control):
    if status not in {'queued', 'stopped', 'interrupted', 'action_required', 'recovery_failed'} or control in {'pause', 'stop'}:
        return {}
    from console.tasks import read_json

    wait = read_json(run / recovery_wait.FILE)
    try:
        if not wait or not matches(run, wait, _state(run)):
            return {}
    except (OSError, ValueError):
        return {}
    return {key: wait[key] for key in ('action', 'condition', 'retry_at', 'attempts', 'blocked', 'error', 'phase') if key in wait}


def _state(run):
    return Directions(run).read('state.json', 32 * 1024 * 1024)


def matches(run, wait, state):
    if wait.get('inputs') and wait['inputs'] != recovery_wait.inputs(run):
        return False
    if wait.get('checkpoint') and wait['checkpoint'] == recovery_wait.checkpoint(
            run, legacy='inputs' not in wait, include_checks=wait.get('checkpoint_version') == 2, state=state):
        return True
    if wait.get('phase') == 'prepared':
        if wait.get('progress') != recovery_wait.progress(run, state):
            return False
        if state.get('status') == wait['terminal_status']:
            return state.get('summary') == wait['detail']
        report = Directions(run).read('report.json', 32 * 1024 * 1024)
        return (state.get('status') == 'running' and report.get('status') == wait['terminal_status']
                and report.get('summary') == wait['detail'])
    if wait.get('phase') in {'starting', 'adopted'}:
        if wait.get('action') == 'start' and not state:
            return True
        return (state.get('status') == 'running'
                and (wait.get('progress') == recovery_wait.progress(run, state)
                     or wait.get('action') == 'start' and not (state.get('usage') or {}).get('agent_calls')))
    return not wait.get('checkpoint')


def launch(store, meta, run, wait, state, action):
    from console.tasks import TaskError

    wait = recovery_wait.starting(run, {**wait, 'action': action}, state)
    try:
        store.action(meta['id'], action)
    except (OSError, TaskError) as exc:
        error = redact(str(exc))
        if isinstance(exc, OSError) or exc.retryable or exc.status == 503:
            recovery_wait.retry(run, wait, error)
        else:
            write_json(run / recovery_wait.FILE, {**wait, 'blocked': True, 'error': error}, durable=True)
        if action == 'start':
            store._save({**meta, 'error': error})


def _input_ready(run, state, condition):
    if condition == 'delivery_readiness':
        marker = recovery_wait.cleanup_marker(run, state)
        return marker and dispatch_inputs.delivery_ready(run, state, marker)
    directions = Directions(run)
    wait = directions.read(dispatch_inputs.WAIT_FILE)
    if state.get('status') == 'running':
        # A prepared or adopted checkpoint still carries the original wait binding.
        state = {**state, 'status': 'action_required', 'summary': wait.get('summary')}
    return dispatch_inputs.ready(wait, state, Path(state['cwd']).resolve(),
                                 [row['id'] for row in directions.messages()], run=run)


def recover(store, meta, run):
    from console.tasks import read_json

    wait = recovery_wait.available(run)
    if not wait and meta.get('auto_start_pending') and not (run / recovery_wait.FILE).exists():
        wait = {'action': 'start', 'condition': 'startup', 'detail': ''}
    if not wait and not (run / recovery_wait.FILE).exists() and (run / 'delivery-cleanup.json').is_file():
        state = _state(run)
        if state.get('status') == 'running' and recovery_wait.cleanup_marker(run, state):
            wait = {'action': 'resume', 'condition': 'delivery_cleanup', 'detail': '', 'phase': 'orphaned'}
    if not wait:
        return False
    if read_control(run / 'control.json').get('action') in ('stop', 'pause'):
        return True
    state = _state(run)
    if not matches(run, wait, state):
        return False
    view = store._view(meta, {}, include_details=False)
    action = wait.get('action')
    condition = wait.get('condition')
    if view['status'] in {'running', 'starting', 'paused'}:
        return True
    if condition in {'delivery_readiness', 'dispatch_inputs'} and not _input_ready(run, state, condition):
        return bool(wait.get('checkpoint'))
    if (view['status'] == 'interrupted' and state.get('status') == 'running'
            and wait.get('phase') in {'prepared', 'starting', 'adopted', 'orphaned'}):
        # A launched supervisor can die after writing its running checkpoint.
        action, expected = 'resume', True
    elif action == 'start' and condition == 'startup':
        expected = view['status'] == 'queued'
    elif action == 'resume' and condition == 'delivery_cleanup':
        expected = view['status'] == 'recovery_failed' and (run / 'delivery-cleanup.json').is_file()
    elif action == 'resume' and condition == 'referee_cleanup':
        expected = view['status'] == 'recovery_failed' and bool(wait.get('checkpoint'))
    elif action == 'resume' and condition == 'model_transport':
        expected = (view['status'] == 'action_required'
                    and (wait.get('checkpoint') or view.get('summary') == wait.get('detail')))
    elif action == 'resume' and condition == 'check_environment':
        expected = view['status'] == 'action_required'
    elif action == 'resume' and condition in {'delivery_readiness', 'dispatch_inputs'}:
        expected = view['status'] == 'action_required'
    elif action == 'resume' and condition == 'guardian_crash':
        expected = (view['status'] in {'stopped', 'interrupted'}
                    and read_json(run / 'guardian.json').get('status') == 'restart_limit')
    else:
        return False
    if not expected or action not in view['actions']:
        return False
    launch(store, meta, run, wait, state, action)
    return True
