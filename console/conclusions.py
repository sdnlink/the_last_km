"""Present reviewer claims alongside the monitor's recorded decision."""


def present_conclusion(item: dict) -> dict:
    keys = ('index', 'role', 'status', 'summary', 'done', 'gaps', 'next_actions', 'evidence',
            'blockers', 'exit_code', 'elapsed_s', 'git_checkpoint', 'issue_resolutions', 'watcher_result',
            'task_id', 'task_status')
    result = {key: item.get(key) for key in keys}
    outcome = item.get('watcher_result')
    if not isinstance(outcome, dict):
        outcome = {}
    if item.get('role') != 'reviewer':
        return result
    # Older checkpoints record the monitor's rejection in gaps, before an explicit
    # watcher_result field existed. Read it without rewriting the saved history.
    reasons = [text for text in item.get('gaps', []) or [] if isinstance(text, str) and text.startswith('Audit issue update rejected:')]
    if not outcome and reasons:
        outcome = {'status': 'rejected', 'closed': [], 'reasons': reasons, 'legacy': True}
    result['watcher_result'] = outcome
    if outcome.get('status') == 'rejected':
        ids = [proposal['id'] for proposal in item.get('issue_resolutions', []) or []
               if isinstance(proposal, dict) and proposal.get('id')]
        result['reported_summary'] = item.get('summary', '')
        result['summary'] = ('、'.join(ids) + ' 的关闭未获 watcher 接受，仍需复核。' if ids else
                             'watcher 未接受本轮审计更新，请核对系统处理结果。')
    return result
