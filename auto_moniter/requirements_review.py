"""Read-only reviewer elaboration; preserve the person's original scope."""
import json

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
    normalized = ''.join(text.split()).lower()
    markers = (
        '继续下一轮功能需求补充',
        '继续下一轮需求补充',
        '下一轮功能需求补充',
        '下一轮需求补充',
        'continuewithanotherrequirement',
        'continuewiththenextrequirement',
        'nextrequirementelaboration',
    )
    return any(marker in normalized for marker in markers)


def prompt(config, modules, *, follow_up=None):
    return ('You are the independent requirement reviewer. Before any worker implementation, read the project and elaborate the original request. '
            'You are strictly read-only: no edits, commits, configuration changes, deployments or external writes. '
            'Return only the supplied requirement-elaboration JSON schema, not an implementation completion verdict. '
            'Preserve every original requirement, explicit exclusion, fixed module, executable check and human authority. '
            'Add concrete acceptance criteria, failure/edge cases, constraints and reasonable assumptions within the original scope. '
            'Never invent approvals or credentials, remove requirements or expand authorization. Mark genuine external decisions as needs_input. '
            'No implementation has been delivered; ready means the requirement is ready for worker execution only.\n\n'
            + _follow_up_block(follow_up) +
            'Original request:\n'+config.requirement+'\n\nHuman acceptance criteria:\n'+config.acceptance+
            '\n\nImmutable modules:\n'+json.dumps(modules,ensure_ascii=False)+'\n\nImmutable commands:\n'+json.dumps(config.checks,ensure_ascii=False))


def _follow_up_block(follow_up):
    if not follow_up:
        return ''
    return ('This is the next requirement elaboration after a locally committed, jointly accepted delivery. '
            'Read the current project and name one concrete unfinished capability inside the original request. '
            'Cite files or symbols, expected behavior, failure or boundary conditions, and how to verify it. '
            'Do not resubmit a defect that already has an open audit issue, and do not treat a repair round as a new requirement. '
            'needs_input is required when no concrete remaining capability can be named from the project. '
            'Preserve the original request, human acceptance criteria, fixed modules, checks, quality limits and accepted work.\n\n'
            'Accepted deliveries already committed in this run:\n'+json.dumps(follow_up,ensure_ascii=False)+'\n\n')
