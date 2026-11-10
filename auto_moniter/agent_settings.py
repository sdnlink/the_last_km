"""Agent installation choices and task-scoped runtime configuration."""
import json
import os
from pathlib import Path
import shutil


AGENTS = {
    'codex': {'label': 'Codex', 'binary': 'codex', 'package': '@openai/codex', 'backend': 'codex', 'key_env': 'OPENAI_API_KEY', 'url_env': 'OPENAI_BASE_URL', 'docs': 'https://developers.openai.com/codex/cli/'},
    'grok': {'label': 'Grok', 'binary': 'grok', 'backend': 'grok', 'key_env': 'XAI_API_KEY', 'docs': 'https://docs.x.ai/build/cli/headless-scripting'},
    'claude': {'label': 'Claude Code', 'binary': 'claude', 'package': '@anthropic-ai/claude-code', 'backend': 'claude', 'key_env': 'ANTHROPIC_AUTH_TOKEN', 'url_env': 'ANTHROPIC_BASE_URL', 'docs': 'https://code.claude.com/docs/en/setup'},
    'antigravity': {'label': 'Antigravity CLI', 'binary': 'agy', 'backend': 'antigravity', 'docs': 'https://antigravity.google/docs/getting-started?tab=cli'},
    'gemini': {'label': 'Gemini CLI', 'binary': 'gemini', 'package': '@google/gemini-cli', 'backend': 'gemini', 'key_env': 'GEMINI_API_KEY', 'docs': 'https://geminicli.com/docs/get-started/installation/'},
    'copilot': {'label': 'Copilot CLI', 'binary': 'copilot', 'package': '@github/copilot', 'backend': 'copilot', 'key_env': 'COPILOT_GITHUB_TOKEN', 'worker_only': True, 'docs': 'https://docs.github.com/en/copilot/how-tos/copilot-cli/cli-getting-started'},
    'hermes': {'label': 'Hermes', 'binary': 'hermes', 'backend': 'hermes', 'key_env': 'OPENAI_API_KEY', 'url_env': 'OPENAI_BASE_URL', 'worker_only': True, 'docs': 'https://hermes-agent.nousresearch.com/docs/getting-started/installation'},
    'kimi': {'label': 'Kimi Code CLI', 'binary': 'kimi', 'package': '@moonshot-ai/kimi-code', 'backend': 'kimi', 'key_env': 'KIMI_MODEL_API_KEY', 'url_env': 'KIMI_MODEL_BASE_URL', 'worker_only': True, 'note': '使用当前 Kimi Code CLI，安装需要兼容版本的 Node.js。API Key 模式须填写模型 ID；使用自定义接口时选择接口类型并填写服务地址。', 'docs': 'https://moonshotai.github.io/kimi-code/en/guides/getting-started'},
    'codebuddy': {'label': 'CodeBuddy', 'binary': 'codebuddy', 'package': '@tencent-ai/codebuddy-code', 'backend': 'codebuddy', 'key_env': 'CODEBUDDY_API_KEY', 'docs': 'https://www.codebuddy.ai/docs/cli/installation'},
    'glm': {'label': 'GLM', 'binary': 'claude', 'package': '@anthropic-ai/claude-code', 'backend': 'claude', 'key_env': 'ANTHROPIC_AUTH_TOKEN', 'url_env': 'ANTHROPIC_BASE_URL', 'endpoint': 'https://api.z.ai/api/anthropic', 'note': '通过 Claude Code 接入 GLM；填写你的 GLM 模型 ID 和 API Key。', 'docs': 'https://docs.z.ai/devpack/tool/claude'},
    'deepseek': {'label': 'DeepSeek', 'binary': 'claude', 'package': '@anthropic-ai/claude-code', 'backend': 'claude', 'key_env': 'ANTHROPIC_API_KEY', 'url_env': 'ANTHROPIC_BASE_URL', 'endpoint': 'https://api.deepseek.com/anthropic', 'note': '通过 Claude Code 的 Anthropic 兼容接口接入 DeepSeek。', 'docs': 'https://api-docs.deepseek.com/guides/anthropic_api'},
    'cursor': {'label': 'Cursor Agent', 'binary': 'cursor-agent', 'backend': 'cursor', 'manual': True, 'docs': 'https://cursor.com/docs/cli/installation'},
    'paseo': {'label': 'Paseo', 'binary': 'paseo', 'backend': 'paseo', 'manual': True, 'docs': 'https://paseo.sh'},
}


def read_settings(path: Path | None = None) -> dict:
    path = path or (Path(os.environ['AUTO_MONITER_AGENT_SETTINGS']) if os.environ.get('AUTO_MONITER_AGENT_SETTINGS') else None)
    if path is None:
        return {}
    try:
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ValueError('Agent 配置文件无效')
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or not isinstance(value.get('agents', {}), dict)
                or any(not isinstance(profile,dict) for profile in value.get('agents',{}).values())):
            raise ValueError('Agent 配置文件格式无效')
        return value
    except FileNotFoundError:
        return {}


def executable(name: str, profile: dict, tools: Path | None = None) -> str:
    spec = AGENTS[name]
    custom = profile.get('executable', '')
    if custom:
        candidate = Path(custom).expanduser()
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else ''
    if tools is not None:
        candidate = tools / 'npm' / 'bin' / spec['binary']
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    candidates = [spec['binary']]
    if name == 'antigravity':
        candidates.append('antigravity')
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    for candidate in [Path.home()/'.local/bin'/spec['binary'], Path.home()/'.grok/bin'/spec['binary'],
                      Path.home()/'.hermes/hermes-agent/.hermes/bin'/spec['binary'],
                      Path.home()/'.hermes/hermes-agent/venv/bin'/spec['binary']]:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ''


def runtime_environment(name: str, profile: dict) -> dict:
    spec = AGENTS[name]
    result = {}
    if profile.get('auth_mode') == 'api_key' and profile.get('api_key') and spec.get('key_env'):
        result[spec['key_env']] = profile['api_key']
    endpoint = profile.get('endpoint') or spec.get('endpoint', '')
    if endpoint and spec.get('url_env'):
        result[spec['url_env']] = endpoint
    if spec['backend'] == 'claude' and profile.get('model'):
        for key in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL'):
            result[key] = profile['model']
    if name=='kimi' and profile.get('auth_mode')=='api_key' and profile.get('api_key'):
        result['KIMI_MODEL_NAME']=profile.get('model','')
        result['KIMI_MODEL_PROVIDER_TYPE']=profile.get('provider_type','kimi')
    return result
