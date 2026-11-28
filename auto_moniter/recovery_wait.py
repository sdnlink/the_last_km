"""Persistent retry scheduling for deterministic recovery conditions."""

import time

from auto_moniter import candidate_checks
from auto_moniter.directions import Directions
from auto_moniter.redaction import redact
from auto_moniter.storage import write_json

FILE = 'recovery-wait.json'


def inputs(run):
    directions = Directions(run)
    return candidate_checks.digest([directions.messages(), directions.read('requirement-queue.json')])


def checkpoint(run, *, legacy=False, include_checks=True, state=None):
    directions = Directions(run)
    if state is None:
        state = directions.read('state.json', 32 * 1024 * 1024)
    if not state:
        return ''
    saved = {key: state.get(key) for key in (
        'status', 'summary', 'updated_at', 'tree', 'next_round', 'dispatch_directions',
        'requirement_queue_version', 'active_queued_requirement', 'referee',
    )}
    if include_checks:
        saved.update({key: state.get(key) for key in ('checks', 'check_source_aliases', 'quality_policy')})
    return candidate_checks.digest([saved, directions.messages() if legacy else inputs(run)])


def progress(run, state):
    """Identify the work boundary across a launcher or supervisor restart."""
    return candidate_checks.digest([{key: state.get(key) for key in (
        'cwd', 'next_round', 'requirement_revision', 'dispatch_directions', 'checks', 'check_source_aliases',
        'quality_policy', 'active_queued_requirement', 'business_update_count', 'last_invocation',
    )}, inputs(run)])


def schedule(run, condition, action, *, detail='', terminal_status=''):
    old = Directions(run).read(FILE)
    attempts = old.get('attempts', 0) + 1 if old.get('condition') == condition else 1
    wait = {'condition': condition, 'action': action, 'detail': detail, 'attempts': attempts,
            'retry_at': time.time() + min(15 * 2 ** min(attempts - 1, 5), 300), 'inputs': inputs(run),
            'checkpoint_version': 2}
    if action == 'resume':
        wait['checkpoint'] = checkpoint(run)
    if terminal_status:
        wait.update(phase='prepared', terminal_status=terminal_status, detail=redact(detail),
                    progress=progress(run, Directions(run).read('state.json', 32 * 1024 * 1024)))
    write_json(run / FILE, wait, durable=True)
    return wait


def available(run):
    value = Directions(run).read(FILE)
    return value if not value.get('blocked') and value.get('retry_at', 0) <= time.time() else {}


def cleanup_marker(run, state):
    """Find final bookkeeping that can resume on the accepted checkpoint."""
    if state.get('status') not in {'running', 'recovery_failed', 'action_required', 'stopped'}:
        return {}
    marker = Directions(run).read('delivery-cleanup.json', 32 * 1024 * 1024)
    binding = marker.get('binding')
    if not isinstance(binding, dict):
        return {}
    current_inputs = inputs(run)
    if (not binding.get('tree') or binding['tree'] != state.get('tree')
            or binding.get('requirement') != (state.get('requirement_revision') or {}).get('sha256')
            or binding.get('checks') != state.get('checks')
            or marker.get('recovery_inputs', current_inputs) != current_inputs):
        return {}
    return marker


def delivery_artifacts(run):
    import hashlib

    artifacts = {}
    for name in ('acceptance-results.json', 'requirement-coverage.json'):
        path = run / name
        if path.is_file() and not path.is_symlink():
            try:
                with path.open('rb') as stream:
                    artifacts[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
            except FileNotFoundError:
                continue
    return artifacts


def controller_cleanup(run, state):
    marker = cleanup_marker(run, state)
    receipt = marker.get('receipt')
    context = receipt.get('context') if isinstance(receipt, dict) else None
    return bool(isinstance(context, dict) and context.get('final') and context.get('tree') == state.get('tree')
                and receipt.get('passed') and candidate_checks.valid(receipt, run, context)
                and candidate_checks.covers_configured_checks(context, state.get('checks') or [])
                and marker.get('artifacts') == delivery_artifacts(run))


def seal(run, status, summary):
    wait = Directions(run).read(FILE)
    if (wait.get('phase') == 'prepared' and wait.get('terminal_status') == status
            and wait.get('detail') == redact(summary)):
        write_json(run / FILE, {**wait, 'phase': 'pending', 'checkpoint': checkpoint(run)}, durable=True)
    else:
        (run / FILE).unlink(missing_ok=True)


def starting(run, wait, state):
    run.mkdir(parents=True, mode=0o700, exist_ok=True)
    record = {**wait, 'phase': 'starting', 'progress': progress(run, state), 'inputs': inputs(run),
              'checkpoint_version': 2}
    if wait.get('phase') in {'starting', 'adopted'}:
        attempts = wait.get('attempts', 0) + 1
        record.update(attempts=attempts, retry_at=time.time() + min(15 * 2 ** min(attempts - 1, 5), 300))
    if record['action'] == 'resume':
        record['checkpoint'] = checkpoint(run, state=state)
    write_json(run / FILE, record, durable=True)
    return record


def retry(run, wait, detail):
    attempts = wait.get('attempts', 0) + 1
    write_json(run / FILE, {**wait, 'attempts': attempts, 'error': detail,
                           'retry_at': time.time() + min(15 * 2 ** min(attempts - 1, 5), 300)}, durable=True)


def adopted(run):
    directions = Directions(run)
    wait = directions.read(FILE)
    if wait.get('phase') != 'starting' or wait.get('inputs') != inputs(run):
        return
    state = directions.read('state.json', 32 * 1024 * 1024)
    if wait.get('action') != 'start' and wait.get('progress') != progress(run, state):
        return
    write_json(run / FILE, {**wait, 'phase': 'adopted', 'progress': progress(run, state)}, durable=True)
