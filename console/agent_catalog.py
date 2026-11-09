"""Bounded, read-only discovery of installed agents and their model catalogs."""
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from auto_moniter.providers import PreparedCommand, _which

LIMIT = 2 * 1024 * 1024


def read_json(path):
    try:
        if path.stat().st_size > LIMIT:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def model_rows(value, source):
    """Only public IDs/labels cross the API; caches may contain credentials."""
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if isinstance(item, dict):
                info = item.get('info', item)
                if isinstance(info, dict):
                    rows.append({**info, '_key': key})
        value = rows
    if not isinstance(value, list):
        return []
    result = {}
    for item in value[:1000]:
        if not isinstance(item, dict) or item.get('hidden') or item.get('visibility') == 'hide':
            continue
        ident = item.get('id') or item.get('slug') or item.get('_key')
        if not isinstance(ident, str) or not ident or len(ident) > 200 or any(ord(c) < 32 for c in ident):
            continue
        label = item.get('display_name') or item.get('name') or item.get('model') or ident
        result[ident] = {'id': ident, 'label': str(label)[:200], 'source': source}
    return list(result.values())


def query(argv, cwd):
    from auto_moniter.supervisor import run_command
    result = run_command(PreparedCommand(['env', 'NO_COLOR=1', *argv]), cwd, timeout_s=4,
                         max_output_bytes=LIMIT, detect_input_requests=False)
    if result.exit_code or result.timed_out or result.interruption:
        raise ValueError('catalog command failed')
    return result.stdout


def text_models(text, source, *, grok=False):
    rows = []
    in_list = not grok
    for line in text.splitlines():
        line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).strip()
        if line == 'Available models:':
            in_list = True
            continue
        if not in_list:
            continue
        match = re.fullmatch(r'(?:[-*]\s+)?([A-Za-z0-9][A-Za-z0-9._:/\[\]-]{1,199})(?:\s+[-–]\s+(.+?)|\s+\(default\))?', line)
        if match:
            rows.append({'id': match[1], 'name': match[2] or match[1]})
    return model_rows(rows, source)


def discover(cwd, available, *, live=False, home=None):
    home = Path(home) if home is not None else Path.home()
    catalogs = {}
    for name in available:
        root = Path(os.environ.get('CODEX_HOME', home/'.codex')) if name == 'codex' else home/('.'+name)
        rows = model_rows(read_json(root/'models_cache.json').get('models', []), 'local_cache')
        catalogs[name] = {'models': rows, 'note': '本机缓存，尚未验证账户调用权限' if rows else '未枚举到模型，可使用默认模型或自定义 ID'}
    # Configuration can identify custom models without disclosing config values.
    claude = read_json(home/'.claude/settings.json')
    env = claude.get('env', {}) if isinstance(claude, dict) else {}
    if isinstance(env, dict):
        values = [env.get(key) or os.environ.get(key) for key in
                  ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL')]
        rows = model_rows([{'id': value} for value in values if isinstance(value, str)], 'local_config')
        if rows:
            catalogs.setdefault('claude', {})['models'] = rows
    backends = []
    if live:
        paseo = _which('paseo') if available.get('paseo') else None
        if paseo:
            try:
                value = json.loads(query([paseo, 'provider', 'ls', '--json'], cwd))
                for row in value[:100] if isinstance(value, list) else []:
                    ident = row.get('provider') if isinstance(row, dict) else None
                    if isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', ident):
                        backends.append({'id': ident, 'label': str(row.get('label', ident))[:100],
                                         'available': row.get('status') == 'available', 'models': [], 'source': 'paseo_cli'})
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        jobs = {}
        with ThreadPoolExecutor(max_workers=12) as pool:
            for backend in backends:
                if backend['available']:
                    jobs[pool.submit(query, [paseo, 'provider', 'models', backend['id'], '--json'], cwd)] = ('paseo', backend)
            for name, command in [('grok', ['models']), ('cursor', ['models'])]:
                binary = _which('grok' if name == 'grok' else 'cursor-agent')
                if available.get(name) and binary:
                    jobs[pool.submit(query, [binary, *command], cwd)] = (name, None)
            for future in as_completed(jobs):
                name, backend = jobs[future]
                try:
                    output = future.result()
                    if name == 'paseo':
                        rows = model_rows(json.loads(output), 'paseo_cli')
                        backend['models'] = rows
                        # Codex/Grok native catalogs have a more direct source.
                        if backend['id'] in catalogs:
                            current = catalogs[backend['id']]['models']
                            merged = {row['id']:row for row in [*rows, *current]}
                            catalogs[backend['id']] = {'models': list(merged.values()), 'note': '本机配置/缓存及 Paseo provider 目录，未发起模型调用'}
                    else:
                        rows = text_models(output, 'native_cli', grok=name == 'grok')
                        catalogs[name] = {'models': rows, 'note': '通过本机 CLI 枚举，未发起模型调用'}
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    if backend is not None:
                        backend['note'] = '模型目录查询失败，可使用默认模型或自定义 ID'
    return {'at': datetime.now(timezone.utc).isoformat(), 'live': live,
            'models': catalogs, 'paseo_backends': backends,
            'note': '执行器可用表示发现本机 CLI；模型目录不等同于账户鉴权或功能验收通过'}
