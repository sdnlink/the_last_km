"""Apply one agent's private credentials immediately before exec, outside argv/logs."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_moniter.agent_settings import AGENTS, read_settings, runtime_environment


def main():
    path, name, model, *argv = sys.argv[1:]
    profile = read_settings(Path(path)).get('agents', {}).get(name, {})
    if model:
        profile = {**profile, 'model': model}
    env = os.environ.copy()
    if profile.get('auth_mode') == 'api_key' and AGENTS[name]['backend'] == 'claude':
        # A different Claude-compatible service must not inherit another profile's token.
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
            env.pop(key, None)
    if profile.get('auth_mode')=='api_key' and name=='codebuddy':
        env.pop('CODEBUDDY_AUTH_TOKEN',None)
    env.update(runtime_environment(name, profile))
    endpoint = profile.get('endpoint')
    if AGENTS[name]['backend'] == 'codex' and endpoint:
        import json
        options = {'model_provider': 'console', 'model_providers.console.name': 'Console',
                   'model_providers.console.base_url': endpoint, 'model_providers.console.env_key': 'OPENAI_API_KEY',
                   'model_providers.console.wire_api': 'responses'}
        argv[1:1] = [part for key, value in options.items() for part in ('-c', key+'='+json.dumps(value))]
    if name=='hermes' and profile.get('auth_mode')=='api_key':
        endpoint=endpoint or 'https://api.openai.com/v1'
        env['OPENAI_BASE_URL']=endpoint
        argv+=['--provider','custom','--base-url',endpoint]
    os.execvpe(argv[0], argv, env)


if __name__ == '__main__':
    main()
