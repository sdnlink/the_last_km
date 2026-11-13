import assert from 'node:assert/strict';
import {test} from 'node:test';
import {i18n,i18nHTML,language,locale,initializeLanguage} from '../console/static/i18n.js';
import {controlsHTML} from '../console/static/pagination.js';
import {timeLabel,dateTimeLabel} from '../console/static/ui.js';

test('English UI preserves interpolation values and escapes only translated HTML',()=>{
 const previous=globalThis.document;
 globalThis.document={documentElement:{lang:'en'}};
 try{
  assert.equal(language(),'en');assert.equal(locale(),'en-US');
  assert.equal(i18n('执行失败'),'Failed');
  const user='执行失败 <script>{0}</script>';
  assert.equal(i18n`任务 ${user}：`,'Task '+user+':');
  assert.equal(i18nHTML`<p>当前需求</p><pre>${user}</pre>`,'<p>Current requirement</p><pre>'+user+'</pre>');
  assert.equal(i18nHTML`<input aria-label="启用 ${user}" />`,`<input aria-label="启用 ${user}" />`); // Unknown keys are not guessed.
  assert.equal(i18n`每 ${1} 轮`,'Every 1 round');
  assert.equal(i18n`每 ${2} 轮`,'Every 2 rounds');
  const at='2026-11-13T03:24:00Z';
  assert.equal(timeLabel(at),new Date(at).toLocaleTimeString('en-US'));
  assert.equal(dateTimeLabel(at),new Date(at).toLocaleString('en-US'));
  document.documentElement.lang='zh-CN';
  assert.equal(timeLabel(at),new Date(at).toLocaleTimeString('zh-CN'));
 }finally{globalThis.document=previous;}
});

test('pagination uses native wording and language changes keep the current route',()=>{
 const previous={document:globalThis.document,location:globalThis.location};
 let listener,reloaded=false;
 const field={value:'',addEventListener:(name,handler)=>{assert.equal(name,'change');listener=handler;}};
 globalThis.document={documentElement:{lang:'en'},querySelector:()=>field};
 globalThis.location={reload:()=>{reloaded=true;}};
 try{
  const markup=controlsHTML('test',{start:10,end:20,total:25,page:2,pages:3,size:10});
  assert.match(markup,/Showing 11–20 of 25/);assert.match(markup,/Page <input/);assert.match(markup,/of 3/);
  assert.doesNotMatch(markup,/[\u3400-\u9fff]/);
  initializeLanguage();assert.equal(field.value,'en');
  field.value='zh-CN';listener();assert.ok(reloaded);
  assert.equal(document.cookie,'auto_moniter_lang=zh-CN; Path=/; Max-Age=31536000; SameSite=Lax');
  document.documentElement.lang='zh-CN';assert.equal(i18n('执行失败'),'执行失败');assert.equal(locale(),'zh-CN');
 }finally{Object.assign(globalThis,previous);}
});
