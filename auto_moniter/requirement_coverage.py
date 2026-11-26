"""Independent, read-only coverage review before a requirement plan is accepted."""
import hashlib
import json
import re


_COMPLEX_SCOPE = re.compile(
    r'跨(?:页面?|角色|仓库|服务|模块)|前后端|多角色|数据迁移|逐页|全(?:部|站|量).{0,8}(?:页面|筛选|路由|功能)'
    r'|\bcross[- ](?:page|role|repository|service|module)\b'
    r'|\b(?:multi[- ]role|data migration|all (?:pages|filters|routes))\b'
    r'|\bfront[- ]?end\b.{0,30}\bback[- ]?end\b',
    re.IGNORECASE,
)

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['pass', 'revise']},
        'summary': {'type': 'string'},
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'source_id': {'type': 'string'},
                    'quote': {'type': 'string'},
                    'gap': {'type': 'string'},
                },
                'required': ['source_id', 'quote', 'gap'],
            },
        },
    },
    'required': ['status', 'summary', 'findings'],
}

DISPATCH_SCHEMA = {
    **SCHEMA,
    'properties': {
        **SCHEMA['properties'],
        'independent_next_task': {
            'type': 'string',
            'description': 'Exact next_task ID approved to run independently, or empty.',
        },
        'affected_tasks': {
            'type': 'array',
            'items': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'task_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'evidence_needed': {'type': 'string'},
                    'owner': {'type': 'string'},
                },
                'required': ['task_ids', 'evidence_needed', 'owner'],
            },
        },
    },
    'required': [*SCHEMA['required'], 'independent_next_task', 'affected_tasks'],
}


def independent_dispatch(review, plan, accepted):
    """An explicit exception for the exact reviewed next function only."""
    task = plan.get('next_task') or {}
    if (review['status'] != 'revise' or not task
            or review.get('independent_next_task') != task['id']
            or task['delivery_scope'] != 'function'
            or not set(task['depends_on']) <= {item['task']['id'] for item in accepted}):
        return False
    planned = {item['id'] for item in plan.get('function_plan', [])}
    affected = {ident for item in review['affected_tasks'] for ident in item['task_ids']}
    return bool(affected and affected <= planned and task['id'] not in affected)


def requires_review(sources, modules, workspace_bindings):
    return (sum(module['status'] != 'accepted' for module in modules) >= 3 or bool(workspace_bindings)
            or any(_COMPLEX_SCOPE.search(text) for text in sources.values()))


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def prompt(sources, plan, *, modules, checks, accepted, scoped_dispatch=False):
    return (
        '独立复核需求覆盖范围。你处于独立只读会话，没有参与原方案制定。'
        '先从用户原文、验收要求和追加要求列出必须覆盖的条款，再逐项对照待复核方案。'
        '必要时只读核对相关页面、角色、接口、依赖和已有检查。重点查找漏项、冲突、'
        '缺少可观察验收结果、跨页或跨角色交接的缺口。仅报告当前授权范围内的实质缺项。'
        '后发的明确要求优先，保留未受影响的约束和已验收事项。'
        '如果没有实际环境，将未完成的验证作为交付约束登记，仍可先执行独立工作。'
        '复核按派单实际依赖判断放行条件。后续派单的构建产物、现场回执或执行配置可按其依赖登记为待办，'
        '只在这些条件属于当前首项的实际前置时阻止独立放行。'
        '每个缺项必须从下方「用户要求和受保护的验收来源」选择 source_id，填写逐字引用 quote 和具体 gap。'
        '裁判建议、待复核方案和源码只用于理解背景，不能作为 source_id。'
        '存在实质缺项时返回 revise，否则返回 pass 和空 findings。'
        '你只能给出复核结论，不得编辑文件、改写方案、执行部署或自行派单。\n\n'
        '用户要求和受保护的验收来源：\n' + json.dumps(sources, ensure_ascii=False)
        + '\n\n待复核方案：\n' + json.dumps(plan, ensure_ascii=False)
        + '\n\n固定模块、已批准的检查和已验收派单：\n'
        + json.dumps({'modules': modules, 'checks': checks, 'accepted': accepted}, ensure_ascii=False)
        + ('\n对 revise 的每条缺项，在 affected_tasks 按相同顺序填写受影响的正式派单编号、'
           '需要补齐的证据和责任方。涉及整份方案时，task_ids 填空数组。'
           '只有已逐项核对首个 next_task 的原文覆盖、真实检查输入和前置依赖，确认该任务与所有缺项独立、'
           '不会改变缺项所需的条件或证据，并且只交付单个功能时，才在 independent_next_task 写其编号。'
           '任何缺项无法定位或首项无法独立执行时填空字符串。pass 时两个字段分别填空字符串和空数组。'
           if scoped_dispatch else '')
    )


class InvalidSource(ValueError):
    def __init__(self, findings):
        super().__init__('需求覆盖缺项须附可核对的用户原文')
        self.findings = findings


def source_repair_prompt(error, previous):
    return (
        '\n\n请在同一份方案上只修正以下缺项的引用，并重新提交完整复核结论。'
        '逐项判断所述问题是否属于用户授权范围。属于时填写上面列出的来源编号及逐字引用，'
        '无法从用户要求或验收条件推导时撤回该项。保留其他已经有有效引用的意见。'
        '不要修改方案、增加新缺项或把裁判建议当成用户要求。'
        '\n引用错误的缺项序号（从 1 开始）：' + json.dumps(error.findings)
        + '\n上次结论：\n' + previous
    )


def parse(text, sources):
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError('需求覆盖复核未返回有效的 JSON 结论') from exc
    if (not isinstance(result, dict) or set(result) not in (
            set(SCHEMA['required']), set(DISPATCH_SCHEMA['required']))
            or result['status'] not in ('pass', 'revise')
            or not isinstance(result['summary'], str) or not result['summary'].strip()
            or len(result['summary']) > 2000):
        raise ValueError('需求覆盖复核结论格式有误')
    findings = result['findings']
    if (not isinstance(findings, list) or len(findings) > 24
            or (result['status'] == 'revise') != bool(findings)):
        raise ValueError('需求覆盖复核结论与缺项清单不一致')
    invalid_sources = []
    for index, item in enumerate(findings, 1):
        if (not isinstance(item, dict) or set(item) != {'source_id', 'quote', 'gap'}
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1000
                       for value in item.values())):
            raise ValueError('需求覆盖缺项格式有误')
        if item['quote'] not in sources.get(item['source_id'], ''):
            invalid_sources.append(index)
    if invalid_sources:
        raise InvalidSource(invalid_sources)
    if 'independent_next_task' in result:
        ident = result['independent_next_task']
        affected = result['affected_tasks']
        if (not isinstance(ident, str) or len(ident) > 120
                or not isinstance(affected, list) or len(affected) != len(findings)
                or any(not isinstance(row, dict) or set(row) != {'task_ids', 'evidence_needed', 'owner'}
                       or not isinstance(row['task_ids'], list) or len(row['task_ids']) > 200
                       or any(not isinstance(key, str) or not key.strip() or len(key) > 120
                              for key in row['task_ids'])
                       or any(not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > 1000
                              for key in ('evidence_needed', 'owner')) for row in affected)
                or ident and (not affected or any(not row['task_ids'] for row in affected))):
            raise ValueError('需求覆盖复核的派单、证据或责任方填写有误')
    return result
