"""Compare delivery claims only when they apply to the same candidate and scope."""


def diagnose(records, *, last_call=None, acceptance=None, current=None):
    worker = next((r for r in reversed(records) if r.get('role') == 'worker'), {})
    reviewer = next((r for r in reversed(records) if r.get('role') == 'reviewer' and r.get('index', 0) > 0), {})
    check = acceptance or {}
    same = bool(worker and reviewer and worker.get('tree') and worker.get('tree') == reviewer.get('tree')
                and worker.get('index') == reviewer.get('index')
                and worker.get('scope') == reviewer.get('scope')
                and set(worker.get('module_ids') or []) == set(reviewer.get('module_ids') or [])
                and worker.get('task_id') == reviewer.get('task_id'))
    watcher = reviewer.get('watcher_result') or {}
    check_current = bool(worker and worker.get('tree') and check.get('tree') == worker['tree']
                         and check.get('round') == worker.get('index') and not check.get('invalidated'))
    def role(r):
        return {k:r[k] for k in ('index','status','reported_status','summary','scope','tree','module_ids','task_id','task_status',
                                  'elapsed_s','failure','interruption','timed_out') if k in r}
    def ready(r, function_status):
        if r.get('failure') or r.get('interruption') or r.get('timed_out'):
            return None
        return r.get('task_status') == function_status if r.get('task_id') else (r.get('reported_status') or r.get('status')) == 'done'
    w_ready, r_ready = ready(worker, 'done'), ready(reviewer, 'accepted')
    delivery = reviewer.get('delivery') or {}
    reasons = delivery.get('reasons') or watcher.get('reasons') or []
    check_failed = check_current and (check.get('source_changed') or any(
        c.get('status') in {'check_failed','environment_unavailable','timed_out','failed'}
        for c in check.get('checks', [])))
    publication = (reviewer.get('git_checkpoint') or {}).get('business_commit') or {}
    published = bool(same and publication.get('commit') and publication.get('round') == reviewer.get('index'))
    if last_call and worker and last_call.get('round', 0) > worker.get('index', 0):
        same = False
        published = False
        check_current = False
    if not worker:
        code = 'implementation_not_started'
    elif same and (watcher.get('status') == 'rejected' or delivery.get('status') == 'rejected' or check_failed):
        code = 'watcher_gate_rejected'
    elif not same or w_ready is None or r_ready is None:
        code = 'no_comparable_review'
    elif published or delivery.get('status') == 'accepted':
        code = 'candidate_delivered_remaining_work_pending'
    elif w_ready != r_ready:
        code = 'worker_reviewer_disagree'
    elif not w_ready:
        code = 'both_incomplete'
    elif (watcher.get('function') or {}).get('status') == 'accepted':
        code = 'function_accepted_remaining_work_pending'
    else:
        code = 'joint_claim_delivery_unconfirmed'
    return {'code':code,'same_candidate_and_scope':same,'worker':role(worker),'reviewer':role(reviewer),
            'watcher':{'audit_status':watcher.get('status') if same else None,
                       'delivery_status':delivery.get('status') if same else None,
                       'reasons':reasons if same else [],
                       'published':published, 'checks':[{k:c.get(k) for k in ('status','exit_code','log')} for c in check.get('checks', [])] if check_current else []},
            'last_call':last_call or {},'current_function':current}
