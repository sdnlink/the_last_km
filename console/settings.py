"""Validated task execution settings shared by creation and launch."""
from pathlib import Path
from auto_moniter.modules import ModuleLedger
from auto_moniter.quality import policy

DEFAULTS = {'review_isolation': 'strict', 'worker_isolation': 'provider',
            'turn_timeout': 1800, 'idle_timeout': 900, 'check_timeout': 600,
            'keep_rounds': 50, 'max_run_bytes': 64 * 1024**2,
            'max_history_bytes': 512 * 1024**2, 'max_agent_state_bytes': 4 * 1024**3}


def settings(data, cwd):
    result = {}
    for name, default in DEFAULTS.items():
        value = data.get(name, default)
        if name.endswith('_isolation'):
            if value not in ('strict', 'provider'):
                raise ValueError(f'{name} 必须为 strict 或 provider')
        elif type(value) is not int or not 0 <= value <= 2**53 - 1 or name == 'check_timeout' and value == 0:
            raise ValueError(f'{name} 必须是有效非负整数（check_timeout 必须大于零）')
        result[name] = value
    paths = data.get('protect_paths', [])
    if not isinstance(paths, list) or len(paths) > 100 or any(not isinstance(p, str) or not p or '\0' in p for p in paths):
        raise ValueError('保护路径必须是字符串数组')
    for name in paths:
        p = (cwd / name).resolve()
        if Path(name).is_absolute() or not p.exists() or p == cwd or not p.is_relative_to(cwd):
            raise ValueError('保护路径必须存在于项目内')
    if paths and result['worker_isolation'] != 'strict':
        raise ValueError('保护路径需要 strict worker 隔离')
    plan = data.get('modules', [])
    result['modules'] = [{k: v for k, v in m.items() if k in {'id', 'title', 'acceptance', 'checks', 'depends_on', 'files'}}
                         for m in ModuleLedger(plan).export()]
    result['protect_paths'] = paths
    if 'quality_policy' in data:
        result['quality_policy'] = policy(data['quality_policy'])
        known = {m['id'] for m in result['modules']}
        if any(set(r['module_ids']) - known for r in result['quality_policy']['requirements']):
            raise ValueError('需求追溯必须关联固定模块清单中的模块')
        if result['quality_policy']['review_tools'] == 'files_only' and data.get('reviewer') != 'claude':
            raise ValueError('files_only 工具边界目前仅支持原生 Claude reviewer')
        if result['quality_policy']['review_tools'] == 'files_only' and result['review_isolation'] != 'strict':
            raise ValueError('files_only 工具边界需要 strict 审核隔离')
    if data.get('reviewer', 'claude') == 'paseo':
        if result['review_isolation'] == 'strict':
            raise ValueError('Paseo daemon 不支持 strict 审核；请选择原生 reviewer 或明确选择 provider 隔离')
        if data.get('paseo_backend', 'claude') != 'claude':
            raise ValueError('Paseo reviewer 仅支持 Claude plan')
    if data.get('provider', 'codex') == 'paseo' and result['worker_isolation'] == 'strict':
        raise ValueError('Paseo daemon 不支持 strict worker')
    return result
