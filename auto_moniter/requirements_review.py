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


def prompt(config, modules):
    return ('You are the independent requirement reviewer. Before any worker implementation, read the project and elaborate the original request. '
            'You are strictly read-only: no edits, commits, configuration changes, deployments or external writes. '
            'Return only the supplied requirement-elaboration JSON schema, not an implementation completion verdict. '
            'Preserve every original requirement, explicit exclusion, fixed module, executable check and human authority. '
            'Add concrete acceptance criteria, failure/edge cases, constraints and reasonable assumptions within the original scope. '
            'Never invent approvals or credentials, remove requirements or expand authorization. Mark genuine external decisions as needs_input. '
            'No implementation has been delivered; ready means the requirement is ready for worker execution only.\n\n'
            'Original request:\n'+config.requirement+'\n\nHuman acceptance criteria:\n'+config.acceptance+
            '\n\nImmutable modules:\n'+json.dumps(modules,ensure_ascii=False)+'\n\nImmutable commands:\n'+json.dumps(config.checks,ensure_ascii=False))
