"""Explain terminal watcher outcomes without confusing them with agent claims."""
import json
import re
from pathlib import Path

from auto_moniter.redaction import redact, safe

TERMINAL = {'failed','invalid','action_required','budget_exceeded','recovery_failed',
            'interrupted','stopped','stalled','max_rounds','blocked','locked'}


def console_result(run):
    """Recover only the monitor's final structured result, never arbitrary prose."""
    path = Path(run)/'console.log'
    try:
        if path.is_symlink():
            return {}
        with path.open('rb') as stream:
            stream.seek(0, 2);stream.seek(max(0,stream.tell()-65536))
            text=stream.read(65536).decode(errors='replace')
    except OSError:
        return {}
    for match in list(re.finditer(r'(?m)^\s*\{',text))[-16:][::-1]:
        try:
            value,_=json.JSONDecoder().raw_decode(text[match.start():].lstrip())
        except ValueError:
            continue
        if (isinstance(value,dict) and value.get('status') in TERMINAL|{'done'}
                and type(value.get('rounds')) is int and isinstance(value.get('summary'),str)):
            return {'status':value['status'],'summary':redact(value['summary'][:4000])}
    return {}


def diagnose(status, state, meta, health, guardian, run, *, details=True):
    if status not in TERMINAL:
        return None
    raw=str(state.get('summary') or meta.get('error') or meta.get('exit_summary') or '')
    source='report.json' if health.get('health')=='finished' else 'state.json'
    if not state.get('summary') and meta.get('error'):
        source='任务启动记录'
    elif not state.get('summary') and meta.get('exit_summary'):
        source='启动进程结束记录'
    if not raw and details:
        value=console_result(run)
        if value.get('status')==status or status in {'failed','interrupted','invalid'}:
            raw=value.get('summary','');source='console.log'
    records=state.get('rounds') if isinstance(state.get('rounds'),list) else state.get('records',[])
    records=[r for r in records or [] if isinstance(r,dict)]
    last=records[-1] if records else {}
    brief=state.get('requirements_review') or {}
    if not raw and isinstance(brief,dict) and brief.get('status') in {'failed','needs_input'}:
        raw=str(brief.get('summary') or '');source='requirements_review'
    if not raw:
        raw=str(last.get('failure') or guardian.get('reason') or '')
    text=raw.lower()
    role=last.get('role','watcher');phase='监督器结束任务';actions=[]
    code=status;title='任务未完成';reason='任务已经停止，尚未通过完整交付验收。'
    if state.get('resume_error') or 'cannot resume checkpoint' in text:
        code='checkpoint_restore_failed';title='检查点恢复失败';role='watcher';phase='加载原运行记录'
        reason=raw
        source='resume-error.json' if state.get('resume_error') else source
        actions=['这是恢复程序或检查点错误，追加预算无法解决；先修复原始错误，再从保留的检查点恢复。']
    elif re.search(r'\b(?:NameError|ImportError|AttributeError|TypeError):',raw):
        code='supervisor_exception';title='监督器程序异常';role='watcher';phase='监督器执行'
        reason=raw
        actions=['先修复原始程序异常，再恢复原任务；增加运行时间或调用额度不能修复程序错误。']
    elif 'business head changed' in text:
        code='business_head_changed';role='watcher';phase='业务版本一致性检查'
        title='业务分支在任务外发生变化'
        reason='当前分支与任务绑定的审核基线不一致，watcher 为避免审核和提交错误版本而停止。worker 的完成声明尚未经过后续独立审核。'
        actions=['使用恢复执行，系统会识别同一业务分支的新提交，保留现有改动和审核问题并重新验证。',
                 '分支切换、历史重写或合并冲突需先处理，再恢复原任务。']
    elif 'ref changed' in text or 'audit branch changed' in text:
        code='history_ref_changed';title='任务 Git 引用发生外部变化';role='watcher';phase='Git 检查点校验'
        reason='保存的审计或候选引用与仓库不一致，任务需要核对版本后再继续。'
        actions=['检查 console.log 和 git-history.json，核对引用归属及变更来源，保留工作区和待恢复事务。']
    elif 'bwrap' in text or 'read-only probe failed' in text or 'strict read-only' in text:
        code='isolation_unavailable';title='只读隔离环境不可用';phase='启动隔离预检';role='watcher'
        reason='strict 审核所需的 Bubblewrap 或系统命名空间探测失败，未放宽隔离继续开发。'
        actions=['安装并验证 bwrap 与系统命名空间能力，再按原 strict 配置重试。']
    elif 'authentication' in text or re.search(r'\b401\b|unauthorized|not signed in|not logged in|invalid.api.key|grok login|set the xai_api_key',text):
        code='authentication';title='模型认证失败';reason='所选执行器未能通过认证，调用结果不能用于验收。'
        actions=['在启动控制台的同一用户环境中完成登录或配置对应密钥，再恢复原任务；不要在需求里填写密钥。']
    elif 'no output' in text or 'idle timeout' in text:
        code='idle_timeout';title='模型长时间没有输出';reason='调用达到无输出时限，被监督器中断；这与总运行预算、单轮硬上限不同。'
        actions=['检查该轮模型日志和连接状态；恢复时根据实际输出方式调整 idle-timeout，保留总预算。']
    elif 'timed out' in text or 'turn limit' in text:
        code='turn_timeout';title='模型或检查达到单次时限';reason='本轮未在配置时限内返回有效完成结论，已有代码需要继续验证。'
        actions=['查看本轮日志，缩小单次修复范围或明确调整单轮时限，再按原累计预算继续。']
    elif 'reserved for handoff' in text:
        code='runtime_handoff';title='已预留时间保存交接';role='watcher';phase='预算收尾'
        reason='剩余运行预算进入收尾区间，未再启动模型调用。已保存候选、未完成项和验证记录，不代表交付通过。'
        actions=['查看 handoff.json；需要继续时明确追加预算，再恢复原任务。']
    elif 'runtime budget' in text:
        code='runtime_budget';title='累计运行时间耗尽';role='watcher';phase='运行预算检查'
        usage=state.get('usage') or {};used=usage.get('elapsed_s');limit=usage.get('max_runtime_s')
        reason=f'任务达到累计运行时间上限（已用 {used} 秒，上限 {limit} 秒），未完成部分仍待验收。'
        actions=['查看已交付和未完成条目；仅在用户允许延长时明确追加预算，否则保留现场结束。']
    elif 'agent call budget' in text:
        code='call_budget';title='累计模型调用额度耗尽';role='watcher';phase='调用预算检查'
        reason='需求完善、worker、reviewer 及审核重试的累计调用已达到上限。'
        actions=['核对已用调用数与额度；仅在用户授权后追加额度，恢复不会清零计数。']
    elif status=='budget_exceeded':
        code='resource_budget';title='资源预算已耗尽';role='watcher';phase='资源预算检查'
        reason='运行目录、共享历史、模型会话或其他受控资源达到预算，任务已保存现有现场并停止。'
        actions=['查看原始原因中的具体资源及额度，处理对应容量或经用户授权调整限制后再继续。']
    elif re.search(r'\b50[0-9]\b|connection|network|temporarily unavailable|service unavailable',text):
        code='model_service_unavailable';title='模型服务不可用';reason='模型接口返回服务错误或连接中断，本轮没有得到可用于验收的结论。'
        actions=['检查所选模型服务的可用性；服务恢复后按原检查点继续，不因服务故障重写已完成代码。']
    elif 'executable not found' in text or 'could not invoke' in text:
        code='executable_unavailable';title='执行器无法启动';phase='执行器启动'
        reason='配置的 CLI 未找到、不可执行或无法创建进程。'
        actions=['检查服务进程的 PATH、CLI 安装位置和权限，再按保存的配置启动。']
    elif status=='stopped':
        title='任务已停止';reason='监督器收到停止请求或信号，当前成果尚未完整验收。'
        actions=['查看停止前记录；需要继续时使用原检查点和累计预算恢复。']
    elif status=='max_rounds':
        title='开发轮次上限已达到';reason='任务已达到 worker 轮次限制，最后一轮结论不代表整份需求已交付。'
        actions=['检查尚未关闭的审核问题和检查结果，再决定后续任务范围。']
    elif status=='stalled':
        title='任务缺少有效进展';reason='相同问题或缺少已验证进展达到停止阈值。'
        actions=['核对反复出现的缺口、验证记录和外部依赖，缩小修复范围后再安排后续工作。']
    elif status=='interrupted':
        title='监督进程已退出，缺少完整结束记录';reason='当前没有可确认的最终交付报告，不能根据最后一条模型消息推定完成。'
        actions=['查看 console.log、guardian.json 和残留进程记录，确认退出来源后再恢复。']
    else:
        reason='监督器或执行器异常结束，具体原始原因见下方记录。'
        actions=['核对原始原因及对应日志，修复指定问题后再安排恢复或后续任务。']
    if 'requirement reviewer' in text or (not records and isinstance(brief,dict) and brief.get('status') in {'reviewing','failed','needs_input'}):
        role='reviewer';phase='需求完善'
    elif role in {'worker','reviewer'} and phase=='监督器结束任务':
        phase='worker 实现' if role=='worker' else '独立审核'
    result={'code':code,'title':title,'reason':reason,'role':role,'phase':phase,'round':last.get('index'),
            'actions':actions,'raw_summary':redact(raw[:4000]) or '没有保存可确认的具体退出原因。',
            'exit_code':meta.get('exit_code'), 'source':source,
            'expected_commit':((state.get('git_history') or {}).get('business_head') or {}).get('commit',''),
            'recent_failures':[{'role':r.get('role'),'round':r.get('index'),
                                'reason':str(r.get('failure') or r.get('interruption'))[:1000],
                                'exit_code':r.get('exit_code')}
                               for r in records if r.get('failure') or r.get('interruption')][-3:]}
    return safe(result)
