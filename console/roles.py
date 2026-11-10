"""Validate native provider/model overrides for a single stopped task."""
from types import SimpleNamespace
from auto_moniter.cli import build_parser
from auto_moniter.providers import resolve_provider
from auto_moniter.supervisor import RunConfig,Supervisor

FLAGS={'--provider','--reviewer','--model','--reviewer-model','--mode','--reviewer-mode'}

def prepare(arguments,data,cwd,*,settings_path=None):
    if set(data)-{'provider','reviewer','model','reviewer_model'}:raise ValueError('只允许修改 worker/reviewer 执行器和模型')
    args=build_parser().parse_args(arguments)
    if args.no_review:raise ValueError('本入口要求独立 reviewer')
    values={k:data.get(k,getattr(args,k) or '') for k in ('provider','reviewer','model','reviewer_model')}
    for k,v in values.items():
        if not isinstance(v,str) or '\0' in v or len(v)>200:raise ValueError('角色或模型格式无效')
        values[k]=v.strip()
    supported={'codex','claude','grok','gemini','cursor','antigravity','copilot','kimi','hermes','codebuddy','glm','deepseek'}
    if any(v not in supported for v in (values['provider'],values['reviewer'],args.provider,args.reviewer)):
        raise ValueError('临时角色切换仅支持原生执行器，不支持 daemon 或自定义命令')
    if values['provider']!=args.provider and args.extra_arg or values['reviewer']!=args.reviewer and args.reviewer_extra_arg:
        raise ValueError('原任务有后端附加参数，不能跨执行器复用；请建立后续任务')
    modes={k:getattr(args,k) if values['provider' if k=='mode' else 'reviewer']==getattr(args,'provider' if k=='mode' else 'reviewer') else '' for k in ('mode','reviewer_mode')}
    worker=resolve_provider(values['provider'],mode=modes['mode'],settings_path=settings_path)
    reviewer=resolve_provider(values['reviewer'],mode=modes['reviewer_mode'],role='reviewer',settings_path=settings_path);reviewer.validate_review(args.reviewer_extra_arg)
    config=RunConfig('',cwd,worker,reviewer=reviewer,review_isolation=args.review_isolation,worker_isolation=args.worker_isolation,
                     protect_paths=args.protect,extra_args=args.extra_arg,reviewer_extra_args=args.reviewer_extra_arg)
    output=[];skip=False
    for arg in arguments:
        if skip:skip=False;continue
        if arg.split('=',1)[0] in FLAGS:
            skip='=' not in arg;continue
        output.append(arg)
    for k,v in {**values,**modes}.items():
        if v:output.append('--'+k.replace('_','-')+'='+v)
    return output,values,Supervisor._provider_config(SimpleNamespace(config=config))
