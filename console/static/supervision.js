import {browserTimeZone,i18n} from './i18n.js';
import {$,dateTimeLabel} from './ui.js';
import {api} from './core.js';

export function mountSupervision(path,{setupFailed=false}={}){
 const form=$('#supervision-form'), fields=$('#supervision-fields');
 const save=$('#supervision-save'), toggle=$('#supervision-toggle'), refresh=$('#supervision-refresh');
 const status=$('#supervision-status'), next=$('#supervision-next'), error=$('#supervision-error');
 const labels={unconfigured:'尚未启用',active:'定时监督已启用',paused:'定时监督已暂停',expired:'日程已到期',completed:'日程已结束'};
 let current=null;
 async function update(data){
  fields.disabled=save.disabled=toggle.disabled=refresh.disabled=true;
  error.hidden=true;
  try{
   current=await api(path,data);
   status.textContent=i18n(labels[current.status]||'请核对 Paseo 日程状态');
   if(setupFailed&&current.status==='unconfigured'){
    error.textContent=i18n('开发任务已创建。监督日程创建失败，可检查配置后重试。');
    error.hidden=false;
   }
   form.elements.cron.value=current.cron;
   form.elements.timezone.value=current.timezone||browserTimeZone();
   form.elements.provider.value=current.provider;
   save.textContent=i18n(current.id?'保存日程':'启用监督');
   toggle.hidden=!['active','paused'].includes(current.status);
   toggle.textContent=i18n(current.status==='paused'?'恢复监督':'暂停监督');
   next.hidden=!current.next_run_at;
   const time=next.querySelector('time');
   time.dateTime=current.next_run_at||'';
   time.textContent=current.next_run_at?dateTimeLabel(current.next_run_at,current.timezone):'';
   time.title=current.timezone||'';
  }catch(exc){
   error.textContent=(setupFailed&&!current?i18n('开发任务已创建。监督日程创建失败，可检查配置后重试。')+' ':'')+exc.message;
   error.hidden=false;
   if(!current)status.textContent=i18n('无法读取监督日程');
  }finally{
   fields.disabled=save.disabled=toggle.disabled=!current;
   refresh.disabled=false;
  }
 }
 form.addEventListener('submit',event=>{
  event.preventDefault();
  if(save.disabled)return;
  const input=Object.fromEntries(new FormData(form));
  update({action:'save',...input});
 });
 toggle.addEventListener('click',()=>update({action:current.status==='paused'?'resume':'pause'}));
 refresh.addEventListener('click',()=>update());
 update();
}
