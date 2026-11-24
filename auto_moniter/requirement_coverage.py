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


def requires_review(sources, modules, workspace_bindings):
    return (sum(module['status'] != 'accepted' for module in modules) >= 3 or bool(workspace_bindings)
            or any(_COMPLEX_SCOPE.search(text) for text in sources.values()))


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def prompt(sources, plan, *, modules, checks, accepted):
    return (
        '独立复核需求覆盖范围。你处于独立只读会话，没有参与原方案制定。'
        '先从用户原文、验收要求和追加要求列出必须覆盖的条款，再逐项对照待复核方案。'
        '必要时只读核对相关页面、角色、接口、依赖和已有检查。重点查找漏项、冲突、'
        '缺少可观察验收结果、跨页或跨角色交接的缺口。仅报告当前授权范围内的实质缺项。'
        '后发的明确要求优先，保留未受影响的约束和已验收事项。'
        '如果没有实际环境，将未完成的验证作为交付约束登记，仍可先执行独立工作。'
        '每个缺项必须引用下方来源中的原文，填写 source_id、逐字引用 quote 和具体 gap。'
        '存在实质缺项时返回 revise，否则返回 pass 和空 findings。'
        '你只能给出复核结论，不得编辑文件、改写方案、执行部署或自行派单。\n\n'
        '用户要求和受保护的验收来源：\n' + json.dumps(sources, ensure_ascii=False)
        + '\n\n待复核方案：\n' + json.dumps(plan, ensure_ascii=False)
        + '\n\n固定模块、已批准的检查和已验收派单：\n'
        + json.dumps({'modules': modules, 'checks': checks, 'accepted': accepted}, ensure_ascii=False)
    )


def parse(text, sources):
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError('需求覆盖复核未返回有效的 JSON 结论') from exc
    if (not isinstance(result, dict) or set(result) != set(SCHEMA['required'])
            or result['status'] not in ('pass', 'revise')
            or not isinstance(result['summary'], str) or not result['summary'].strip()
            or len(result['summary']) > 2000):
        raise ValueError('需求覆盖复核结论格式有误')
    findings = result['findings']
    if (not isinstance(findings, list) or len(findings) > 24
            or (result['status'] == 'revise') != bool(findings)):
        raise ValueError('需求覆盖复核结论与缺项清单不一致')
    for item in findings:
        if (not isinstance(item, dict) or set(item) != {'source_id', 'quote', 'gap'}
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1000
                       for value in item.values())
                or item['quote'] not in sources.get(item['source_id'], '')):
            raise ValueError('需求覆盖缺项须附可核对的用户原文')
    return result
