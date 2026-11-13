"""Read-only reviewer elaboration; preserve the person's original scope."""
import json
import re
import hashlib

MAX_CYCLES = 32


def requirement_hash(text):
    return hashlib.sha256(text.replace('\r\n', '\n').replace('\r', '\n').strip().encode()).hexdigest()

SCHEMA = {'type':'object','additionalProperties':False,'properties':{
    'status':{'type':'string','enum':['ready','needs_input']},
    'summary':{'type':'string'},'refined_requirement':{'type':'string'},
    'acceptance_criteria':{'type':'array','items':{'type':'string'}},
    'constraints':{'type':'array','items':{'type':'string'}},
    'assumptions':{'type':'array','items':{'type':'string'}},
    'blockers':{'type':'array','items':{'type':'string'}}},
    'required':['status','summary','refined_requirement','acceptance_criteria','constraints','assumptions','blockers']}


def parse(text):
    value=json.loads(text)
    if not isinstance(value,dict) or set(value)!=set(SCHEMA['required']):
        raise ValueError('Requirement elaboration has missing or unknown fields')
    if value['status'] not in ('ready','needs_input'):
        raise ValueError('Invalid elaboration status')
    for key in ('summary','refined_requirement'):
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>16000:
            raise ValueError('Missing substantive '+key)
    for key in ('acceptance_criteria','constraints','assumptions','blockers'):
        if not isinstance(value[key],list) or len(value[key])>60 or any(not isinstance(v,str) or not v.strip() or len(v)>2000 for v in value[key]):
            raise ValueError('Invalid '+key)
    if value['status']=='ready' and (not value['acceptance_criteria'] or value['blockers']):
        raise ValueError('Ready elaboration needs acceptance criteria and no unresolved blockers')
    return value


def requests_follow_up(text):
    """True only when the saved request asks for another requirement after delivery.

    Ordinary tasks still finish when the current requirement is accepted. A
    follow-up is another reviewed requirement inside the same run, not a queue
    and not another repair round for the same defect.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    markers = (
        '继续下一轮功能需求补充',
        '继续下一轮需求补充',
        '下一轮功能需求补充',
        '下一轮需求补充',
        'continuewithanotherrequirement',
        'continuewiththenextrequirement',
        'nextrequirementelaboration',
    )
    authorized = False
    for clause in re.split(r'[。！？!?;；\n]', text):
        normalized = ''.join(clause.split()).lower()
        for marker in markers:
            position = normalized.find(marker)
            if position < 0:
                continue
            prefix = normalized[:position]
            # A mentioned feature is not permission to start another task when
            # the same instruction explicitly forbids that continuation.
            if (re.search(r'不要|无需|禁止|不得|不必|不再|不需要|不允许|停止|取消', prefix)
                    or re.search(r"donot|don't|never|without|noneedto|notto|mustnot|stop|cancel", prefix)):
                return False
            authorized = True
    return authorized


def prompt(config, modules, *, follow_up=None, continuation=None, queued_requirement=None):
    opening = ('You are the independent requirement reviewer continuing a saved review. Do not restart a full repository review. '
               'Reuse the previous analysis and inspect only the reported rejection, unresolved blockers, new human '
               'directions, changed files and their affected dependencies. Repair the pending assignment or select '
               'the next unaccepted function. Revalidate stale findings where inputs changed; preserve unchanged findings. '
               'When the prior input snapshot is unavailable, inspect the relevant inputs before relying on a finding. '
               if continuation else
               'You are the independent requirement reviewer. Before any worker implementation, read the project and elaborate the original request. ')
    return (opening+
            'You are strictly read-only: no edits, commits, configuration changes, deployments or external writes. '
            'Return only the supplied requirement-elaboration JSON schema, not an implementation completion verdict. '
            'Preserve every original requirement, explicit exclusion, fixed module, executable check and human authority. '
            'Add concrete acceptance criteria, failure/edge cases, constraints and reasonable assumptions within the original scope. '
            'Plan small deliverable increments: inspect only enough capability inventory to implement the next requested behavior. '
            'Do not make an exhaustive repository audit a dependency of all implementation unless the person requires it. '
            'Reuse verified findings and define dependencies only for actual data or integration needs. '
            'Fixed checks are a minimum: identify focused behavioral checks for the requested changes, '
            'rather than treating unrelated existing tests or diff formatting as proof of new functionality. '
            'Never invent approvals or credentials, remove requirements or expand authorization. Mark genuine external decisions as needs_input. '
            'No implementation has been delivered; ready means the requirement is ready for worker execution only.\n\n'
            + _follow_up_block(follow_up, queued_requirement) +
            ('Saved requirement analysis and input changes (context, not approval):\n'
             +json.dumps(continuation,ensure_ascii=False)+'\n\n' if continuation else '') +
            'Original request:\n'+config.requirement+'\n\nHuman acceptance criteria:\n'+config.acceptance+
            '\n\nImmutable modules:\n'+json.dumps(modules,ensure_ascii=False)+'\n\nImmutable commands:\n'+json.dumps(config.checks,ensure_ascii=False))


def _follow_up_block(follow_up, queued_requirement=None):
    if not follow_up:
        return ''
    if queued_requirement:
        return ('The preceding requirement passed joint review and watcher acceptance. '
                'The person explicitly queued the following next requirement. Refine this request, '
                'without inventing work for a later queue item or repeating completed work. '
                'It may extend the original feature scope; preserve accepted behavior, fixed checks and exclusions.\n'
                + json.dumps(queued_requirement, ensure_ascii=False)
                + '\n\nAccepted deliveries already committed in this run:\n'
                + json.dumps(follow_up, ensure_ascii=False) + '\n\n')
    return ('This is the next requirement elaboration after a locally committed, jointly accepted delivery. '
            'Read the current project and name one concrete unfinished capability inside the original request. '
            'Cite files or symbols, expected behavior, failure or boundary conditions, and how to verify it. '
            'Do not resubmit a defect that already has an open audit issue, and do not treat a repair round as a new requirement. '
            'needs_input is required when no concrete remaining capability can be named from the project. '
            'Preserve the original request, human acceptance criteria, fixed modules, checks, quality limits and accepted work.\n\n'
            'Accepted deliveries already committed in this run:\n'+json.dumps(follow_up,ensure_ascii=False)+'\n\n')
