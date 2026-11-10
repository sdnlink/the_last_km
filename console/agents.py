"""Persistent setup and explicit, asynchronous installation of known agent CLIs."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import shutil
import threading
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from auto_moniter.agent_settings import AGENTS, executable, read_settings
from auto_moniter.storage import write_json
from auto_moniter.redaction import redact, register_secrets


class AgentManager:
    def __init__(self, data_dir: Path, cwd: Path):
        self.path = data_dir / 'agents.json'
        self.tools = data_dir / 'agent-tools'
        self.cwd = cwd
        self.lock = threading.RLock()
        self.jobs = {}
        for profile in read_settings(self.path).get('agents', {}).values():
            register_secrets([profile.get('api_key', '')])

    def initialized(self):
        return bool(read_settings(self.path).get('initialized'))

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        data = read_settings(self.path)
        rows = []
        for name, spec in AGENTS.items():
            profile = data.get('agents', {}).get(name, {})
            binary = executable(name, profile, self.tools)
            rows.append({'id': name, **spec, 'installed': bool(binary), 'detected_executable': binary,
                         'enabled': profile.get('enabled', bool(binary) and name not in ('glm', 'deepseek')),
                         'config': {key: profile.get(key, spec.get(key, 'kimi' if key=='provider_type' else '')) for key in ('model', 'endpoint', 'executable', 'provider_type')},
                         'auth_mode': profile.get('auth_mode', 'api_key' if name in ('glm','deepseek') else 'existing'), 'api_key_set': bool(profile.get('api_key')),
                         'can_review': not spec.get('worker_only', False),
                         'install_method': 'manual' if spec.get('manual') else 'npm' if spec.get('package') else 'official_script'})
        return {'initialized': bool(data.get('initialized')), 'agents': rows,
                'jobs': [self._job_view(job) for job in self.jobs.values()]}

    def save(self, payload):
        if set(payload) - {'agents', 'complete'} or not isinstance(payload.get('agents'), list):
            raise ValueError('Agent 配置须为 agents 数组')
        if len(payload['agents']) > len(AGENTS) or type(payload.get('complete', False)) is not bool:
            raise ValueError('Agent 配置数量或完成状态无效')
        with self.lock:
            data = read_settings(self.path)
            profiles = dict(data.get('agents', {}))
            seen = set()
            for row in payload['agents']:
                if not isinstance(row, dict) or not isinstance(row.get('id'),str) or row['id'] not in AGENTS or row['id'] in seen:
                    raise ValueError('Agent 类型重复或不支持')
                name = row['id'];seen.add(name);spec = AGENTS[name]
                if set(row)-{'id','enabled','model','endpoint','executable','auth_mode','api_key','clear_api_key','provider_type'}:
                    raise ValueError('Agent 配置字段不支持')
                current = dict(profiles.get(name, {}))
                for key in ('enabled', 'clear_api_key'):
                    if key in row and type(row[key]) is not bool:
                        raise ValueError(key+' 必须为布尔值')
                for key, limit in [('model',200),('endpoint',2000),('executable',4000),('api_key',8192),('auth_mode',20),('provider_type',20)]:
                    if key not in row:continue
                    value = row[key]
                    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 for c in value):
                        raise ValueError(key+' 格式无效')
                    value=value.strip()
                    if key=='api_key' and not value:continue
                    current[key]=value
                if current.get('auth_mode','existing') not in ('existing','api_key'):
                    raise ValueError('认证方式不支持')
                if 'provider_type' in current and (name!='kimi' or current['provider_type'] not in ('kimi','openai','anthropic')):
                    raise ValueError('接口类型不支持')
                if current.get('auth_mode')=='api_key' and not spec.get('key_env'):
                    raise ValueError(spec['label']+' 请使用本机 CLI 登录配置')
                endpoint=current.get('endpoint','')
                if endpoint:
                    parsed=urlsplit(endpoint)
                    if not spec.get('url_env') or parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                        raise ValueError('服务地址须为不包含账号、密钥或查询参数的 HTTP(S) 地址')
                if current.get('executable') and not Path(current['executable']).expanduser().is_absolute():
                    raise ValueError('可执行文件位置须为绝对路径')
                if row.get('clear_api_key'):current.pop('api_key',None)
                if name=='kimi' and current.get('auth_mode')=='api_key' and current.get('api_key') and not current.get('model'):
                    raise ValueError('Kimi 的 API Key 模式需要填写模型 ID')
                if 'enabled' in row:current['enabled']=row['enabled']
                profiles[name]=current
            write_json(self.path, {'version':1,'initialized':data.get('initialized',False) or payload.get('complete',False),'agents':profiles}, durable=True)
            os.chmod(self.path,0o600)
            for profile in profiles.values():register_secrets([profile.get('api_key','')])
            return self.snapshot()

    def providers(self, discovered):
        data=read_settings(self.path)
        available=dict(discovered)
        for name in AGENTS:
            profile=data.get('agents',{}).get(name,{})
            if profile.get('executable') or (self.tools/'npm/bin'/AGENTS[name]['binary']).is_file():
                available[name]=bool(executable(name,profile,self.tools))
            elif name not in available:
                available[name]=bool(discovered.get(AGENTS[name]['backend'],False))
        return available

    def probe(self, name):
        if not isinstance(name,str) or name not in AGENTS:raise ValueError('Agent 类型不支持')
        binary=executable(name,read_settings(self.path).get('agents',{}).get(name,{}),self.tools)
        if not binary:return {'id':name,'installed':False,'message':'未发现 CLI，可安装或填写可执行文件位置。'}
        from auto_moniter.providers import PreparedCommand
        from auto_moniter.supervisor import run_command
        result=run_command(PreparedCommand([binary,'--version']),self.cwd,timeout_s=8,max_output_bytes=65536,detect_input_requests=False)
        return {'id':name,'installed':result.exit_code==0,'executable':binary,'message':redact((result.stdout or result.stderr)[-3000:]) or 'CLI 检测完成。'}

    @staticmethod
    def _job_view(job):
        return {key:job.get(key) for key in ('id','agent','status','message','output','started_at','finished_at')}

    def install(self, name):
        if not isinstance(name,str) or name not in AGENTS:raise ValueError('Agent 类型不支持')
        spec=AGENTS[name]
        if spec.get('manual'):raise ValueError('请从官方页面安装 '+spec['label']+'，再填写可执行文件位置并检测。')
        with self.lock:
            for job in self.jobs.values():
                if job['agent']==name and job['status'] in ('queued','running'):return self._job_view(job)
            if len([j for j in self.jobs.values() if j['status'] in ('queued','running')])>=2:
                raise ValueError('已有两个安装任务，请等待完成后重试')
            if len(self.jobs)>=30:
                for ident in list(self.jobs):
                    if self.jobs[ident]['status'] not in ('queued','running'):self.jobs.pop(ident);break
            job={'id':uuid.uuid4().hex,'agent':name,'status':'queued','message':'等待安装','output':'','started_at':datetime.now(timezone.utc).isoformat()}
            self.jobs[job['id']]=job
            threading.Thread(target=self._install,args=(job,),daemon=True).start()
            return self._job_view(job)

    def _install(self, job):
        from auto_moniter.providers import PreparedCommand
        from auto_moniter.supervisor import run_command
        name=job['agent'];spec=AGENTS[name];job.update(status='running',message='正在安装 '+spec['label'])
        try:
            profile=read_settings(self.path).get('agents',{}).get(name,{})
            if executable(name,profile,self.tools):
                job.update(status='succeeded',message='已检测到可用 CLI，保留现有安装与登录配置。')
                return
            self.tools.mkdir(parents=True,exist_ok=True)
            if spec.get('package'):
                npm=shutil.which('npm')
                if not npm:raise ValueError('请先安装 Node.js 和 npm，再重试安装。')
                command=[npm,'install','--global','--engine-strict','--prefix',str(self.tools/'npm'),spec['package']]
            else:
                url={'grok':'https://x.ai/cli/install.sh','hermes':'https://hermes-agent.nousresearch.com/install.sh',
                     'antigravity':'https://antigravity.google/cli/install.sh'}[name]
                with urlopen(Request(url,headers={'User-Agent':'AutoMoniter/1.0'}),timeout=30) as response:
                    content=response.read(2*1024*1024+1)
                if len(content)>2*1024*1024:raise ValueError('官方安装脚本超过大小限制')
                script=self.tools/(name+'-install.sh');script.write_bytes(content);os.chmod(script,0o700)
                command=['bash',str(script)]
                if name=='hermes':command.append('--non-interactive')
            job['message']='正在运行官方安装程序：'+shlex.join(command)
            def output(_channel,block):job['output']=redact((job['output']+block.decode('utf-8',errors='replace'))[-16000:])
            result=run_command(PreparedCommand(command),self.cwd,timeout_s=600,max_output_bytes=2*1024*1024,on_output=output,detect_input_requests=False)
            job['output']=redact((result.stdout+'\n'+result.stderr)[-16000:])
            if result.exit_code or result.timed_out or result.interruption:raise ValueError('安装未完成，请查看输出并修复环境后重试。')
            binary=executable(name,profile,self.tools)
            if not binary:raise ValueError('安装程序已结束，但未发现 CLI；请填写其可执行文件位置并检测。')
            self.save({'agents':[{'id':name,'enabled':True,'executable':binary}]})
            job.update(status='succeeded',message='安装完成，可以配置模型与认证。')
        except Exception as error:
            job.update(status='failed',message=redact(str(error)))
        finally:
            job['finished_at']=datetime.now(timezone.utc).isoformat()
