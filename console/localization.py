"""Locale selection and translations for console-owned presentation only."""

import html
import json
import re
from functools import lru_cache
from http.cookies import CookieError, SimpleCookie
from pathlib import Path


@lru_cache(maxsize=1)
def messages():
    source = (Path(__file__).with_name('static') / 'translations.js').read_text(encoding='utf-8')
    return json.loads(source.removeprefix('export default ').rstrip().removesuffix(';'))


def select_language(cookie='', accept_language=''):
    try:
        jar=SimpleCookie();jar.load(cookie)
        preferred=jar.get('auto_moniter_lang')
        if preferred and preferred.value in {'zh-CN','en'}:
            return preferred.value
    except CookieError:
        pass
    weighted=[]
    for index, item in enumerate(accept_language.split(',')):
        parts=item.strip().split(';');name=parts[0].lower()
        try:quality=float(next((part.strip()[2:] for part in parts[1:] if part.strip().startswith('q=')),'1'))
        except ValueError:continue
        if 0<quality<=1:weighted.append((quality,-index,name))
    for _,_,name in sorted(weighted,reverse=True):
        if name.startswith('zh'):return 'zh-CN'
        if name.startswith('en'):return 'en'
    return 'en' if accept_language.strip() else 'zh-CN'


def translate(text, language='zh-CN'):
    if language!='en' or not isinstance(text,str):return text
    key=re.sub(r'\s+',' ',text).strip()
    translated=messages().get(key)
    if translated is not None:
        match=re.fullmatch(r'(\s*)([\s\S]*?)(\s*)',text)
        return match[1]+translated+match[3]
    for pattern, target in patterns():
        match=pattern.fullmatch(text)
        if match:
            return re.sub(r'\{(\d+)\}',lambda item:match.group('v'+item[1]),target)
    for prefix,target in messages().items():
        if prefix.endswith(('：',':')) and text.startswith(prefix):
            return target+(' ' if target.endswith(':') else '')+translate(text[len(prefix):],language)
    return text


@lru_cache(maxsize=1)
def patterns():
    result=[]
    # Prefer specific messages over short count templates such as “{0} 轮{1}”.
    ordered = sorted(messages().items(), key=lambda pair: len(re.sub(r'\{\d+\}', '', pair[0])), reverse=True)
    for source,target in ordered:
        if not re.search(r'\{\d+\}',source):continue
        expression='';end=0;seen=set()
        for slot in re.finditer(r'\{(\d+)\}',source):
            expression+=re.escape(source[end:slot.start()])
            index=slot[1]
            expression+=f'(?P<v{index}>.*?)' if index not in seen else f'(?P=v{index})'
            seen.add(index);end=slot.end()
        expression+=re.escape(source[end:])
        result.append((re.compile(expression,re.DOTALL),target))
    return result


def translate_html(source,language='zh-CN'):
    if language!='en':return source
    def part(match):
        value=match[0]
        if value.startswith('<!--'):return value
        if value.startswith('<'):
            def attribute(item):
                result=translate(item[3],language)
                return item[0] if result==item[3] else item[1]+item[2]+html.escape(result,quote=True)+item[2]
            return re.sub(r'(\b(?:title|placeholder|aria-label|alt|label)\s*=\s*)(["\'])([\s\S]*?)\2',attribute,value)
        result=translate(value,language)
        return value if result==value else html.escape(result,quote=False)
    return re.sub(r'<!--[\s\S]*?-->|<[^>]*>|[^<]+',part,source)


def localize_response(value,language,path):
    if language!='en' or not isinstance(value,dict):return value
    result=dict(value)
    for key in ('error','control_message','resume_unavailable','direction_error','launch_error','acceptance_warning','coverage_warning'):
        if isinstance(result.get(key),str):result[key]=translate(result[key],language)
    if isinstance(result.get('direction_submission'),dict):
        result['direction_submission']={key:translate(item,language) if key in {'message','error'} else item
                                        for key,item in result['direction_submission'].items()}
    if isinstance(result.get('failure'),dict):
        failure = result['failure']
        raw_reason = not failure.get('raw_summary_generated') and failure.get('reason') == failure.get('raw_summary')
        result['failure']={key:translate(item,language) if key in {'title','reason','phase','source'} and not (key=='reason' and raw_reason) else
            [translate(text,language) for text in item] if key=='actions' else item
            for key,item in result['failure'].items()}
        if result['failure'].get('raw_summary_generated'):
            result['failure']['raw_summary'] = translate(result['failure'].get('raw_summary',''),language)
    if isinstance(result.get('discovery'),dict):
        result['discovery']={**result['discovery'],'warnings':[translate(item,language) for item in result['discovery'].get('warnings',[])]}
    if isinstance(result.get('acceptance_plan'),dict):
        plan = result['acceptance_plan'] = dict(result['acceptance_plan'])
        plan['selected'] = [{key:translate(item,language) if key in {'title','purpose','effects','estimate'} else item
                             for key,item in check.items()} for check in plan.get('selected',[])]
    if isinstance(result.get('tasks'),list):
        result['tasks']=[localize_response(task,language,path) for task in result['tasks']]
    if isinstance(result.get('work'),dict):
        work = result['work'] = dict(result['work'])
        work['roles'] = {name:dict(role) for name,role in work.get('roles',{}).items()}
        for role in work['roles'].values():
            if role.get('wait_reason_parts'):
                role['wait_reason'] = ' '.join(translate(part,language) for part in role['wait_reason_parts'])
            if role.get('current_work_generated'):
                role['current_work'] = translate(role.get('current_work',''),language)
        if result.get('failure') and 'watcher' in work['roles']:
            work['roles']['watcher']['current_work'] = result['failure']['title']+': '+result['failure']['reason']
        from console.activity import EVENT_LABELS
        work['workflow'] = [dict(event) for event in work.get('workflow',[])]
        for event in work['workflow']:
            name = event.get('console_event')
            if name not in EVENT_LABELS:
                continue
            original = event.get('text','')
            label = EVENT_LABELS[name]
            if name == 'agent_start':
                role = 'Reviewer' if event.get('role')=='reviewer' else 'Worker'
                event['display_text'] = f"Round {event.get('round','?')} · {role} started"
            elif name == 'git_checkpoint':
                tail = original.split('\n',1)
                event['display_text'] = translate(label,language)+f": round {event.get('round','?')} "+(event.get('role') or 'worker')
                if len(tail)>1:event['display_text']+='\n'+tail[1]
            elif name == 'check_finished':
                event['display_text'] = translate(label,language)+translate(original[len(label):],language)
            elif original.startswith(label):
                tail = original[len(label):]
                event['display_text'] = translate(label,language)+(': '+tail[1:] if tail.startswith('：') else tail)
    if path.startswith(('/api/acceptance/','/api/agents/')) or path=='/api/config':
        def owned(item,key=''):
            if isinstance(item,dict):return {name:owned(child,name) for name,child in item.items()}
            if isinstance(item,list):return [owned(child,key) for child in item]
            return translate(item,language) if isinstance(item,str) and key in {
                'title','purpose','effects','estimate','notice','note','message','error','errors','unverified'} else item
        result=owned(result)
    return result
