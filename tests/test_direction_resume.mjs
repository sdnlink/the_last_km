import assert from 'node:assert/strict';
import {afterEach,test} from 'node:test';
import {config} from '../console/static/core.js';

const names=['document','window','fetch','localStorage','setTimeout','clearTimeout','requestAnimationFrame'];
const originals=Object.fromEntries(names.map(key=>[key,globalThis[key]]));
let serial=0;
afterEach(()=>{for(const [key,value] of Object.entries(originals)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}});
const response=(value,status=200)=>({ok:status<400,status,json:async()=>value});
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
function element(){
 const listeners=new Map();
 return {value:'',checked:false,hidden:false,disabled:false,required:true,open:false,dataset:{},textContent:'',innerHTML:'',
  classList:{toggle(){}},querySelectorAll:()=>[],insertAdjacentHTML(){},
  addEventListener(type,fn){listeners.set(type,[...(listeners.get(type)||[]),fn]);},
  async emit(type){for(const fn of listeners.get(type)||[])await fn({target:this,preventDefault(){}});},
  showModal(){this.open=true;},async close(){this.open=false;await this.emit('close');}};
}
function task(id='A',extra={}){
 return {id,title:'Task '+id,status:'action_required',actions:['resume'],can_direct:true,reviewer:'codex',direction_delivery:'next_turn',
  directions:[{id:'saved-'+id,text:'previously saved requirement',at:'2026-11-12T12:27:45Z'}],...extra};
}
async function harness(){
 const nodes=Object.fromEntries(['dialog-styles','dialog-root','toast','direction-dialog','direction-form','direction-task-title','direction-delivery','direction-help','direction-pause-label','direction-pause','direction-text','direction-history','direction-submit','direction-error'].map(id=>[id,element()]));
 const routes=new Map(),requests=[],storage=new Map();
 globalThis.document={hidden:false,querySelector:selector=>nodes[selector.slice(1)]||null,querySelectorAll:()=>[],addEventListener(){}};
 globalThis.window={innerHeight:800};globalThis.requestAnimationFrame=()=>{};
 globalThis.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
 globalThis.setTimeout=(fn,delay,...args)=>[3000,5000].includes(delay)?-1:originals.setTimeout(fn,delay,...args);
 globalThis.clearTimeout=id=>{if(id!==-1)originals.clearTimeout(id);};
 globalThis.fetch=async (url,options={})=>{
  requests.push({url,options});const key=`${options.method||'GET'} ${url}`;
  if(routes.has(key))return routes.get(key);
  if(url==='/api/config')return response({csrf_token:'fixture'});
  if(url.startsWith('/fragments/'))return {ok:true,text:async()=>''};
  return response(task(url.split('/')[3]));
 };
 await config(true);
 const dialogs=await import(`../console/static/dialogs.js?direction-resume=${++serial}`);
 return {nodes,requests,storage,routes,dialogs};
}
test('existing saved requirements allow empty submit and one request resumes the task',async()=>{
 const h=await harness();await h.dialogs.openDirection('A');
 assert.equal(h.nodes['direction-text'].required,false);
 assert.match(h.nodes['direction-help'].textContent,/无需重复填写/);
 assert.match(h.nodes['direction-submit'].textContent,/恢复执行/);
 h.routes.set('POST /api/tasks/A/directions',response(task('A',{status:'starting',actions:[],direction_submission:{status:'resuming',message:'已启动恢复执行'}})));
 await h.nodes['direction-form'].emit('submit');
 const posts=h.requests.filter(r=>r.options.method==='POST');
 assert.deepEqual(posts.map(r=>r.url),['/api/tasks/A/directions']);
 assert.deepEqual(JSON.parse(posts[0].options.body),{text:'',pause:false});
 assert.match(h.nodes.toast.textContent,/已启动恢复执行/);
});
test('saved-but-blocked response clears persisted input and allows an empty retry',async()=>{
 const h=await harness();await h.dialogs.openDirection('A');
 h.nodes['direction-text'].value='new instruction';await h.nodes['direction-text'].emit('input');
 h.routes.set('POST /api/tasks/A/directions',response(task('A',{direction_submission:{status:'resume_blocked',message:'已保存，无需重复填写',error:'预算已耗尽'}})));
 await h.nodes['direction-form'].emit('submit');
 assert.equal(h.nodes['direction-text'].value,'');assert.equal(h.storage.size,0);
 assert.equal(h.nodes['direction-error'].hidden,false);assert.match(h.nodes['direction-error'].textContent,/预算已耗尽/);
 assert.equal(h.nodes['direction-text'].required,false);assert.equal(h.nodes['direction-submit'].disabled,false);
 h.routes.set('POST /api/tasks/A/directions',response(task('A',{direction_submission:{status:'resuming',message:'已启动恢复执行'}})));
 await h.nodes['direction-form'].emit('submit');
 const posts=h.requests.filter(r=>r.options.method==='POST');
 assert.equal(JSON.parse(posts[1].options.body).text,'');
});
test('failed save retains its draft; queued task without prior submissions requires text',async()=>{
 const h=await harness();h.routes.set('GET /api/tasks/A',response(task('A',{status:'queued',actions:['start'],directions:[]})));
 await h.dialogs.openDirection('A');assert.equal(h.nodes['direction-text'].required,true);
 h.nodes['direction-text'].value='keep this draft';await h.nodes['direction-text'].emit('input');
 h.routes.set('POST /api/tasks/A/directions',response({error:'save failed'},503));
 await h.nodes['direction-form'].emit('submit');
 assert.equal(h.nodes['direction-text'].value,'keep this draft');assert.equal(h.storage.get('auto-moniter-direction-A'),'keep this draft');
 assert.equal(h.nodes['direction-error'].hidden,false);
});
test('explicit pause remains available and duplicate submissions do not restart twice',async()=>{
 const h=await harness();h.routes.set('GET /api/tasks/A',response(task('A',{status:'running',actions:['pause','stop']})));
 await h.dialogs.openDirection('A');h.nodes['direction-pause'].checked=true;await h.nodes['direction-pause'].emit('change');
 assert.equal(h.nodes['direction-submit'].textContent,'保存并暂停');
 const pending=deferred();h.routes.set('POST /api/tasks/A/directions',pending.promise);
 const first=h.nodes['direction-form'].emit('submit');await new Promise(setImmediate);
 await h.nodes['direction-form'].emit('submit');
 assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
 pending.resolve(response(task('A',{status:'running',actions:['continue','stop'],direction_submission:{status:'pause_requested',message:'已保存并请求暂停'}})));await first;
 assert.equal(JSON.parse(h.requests.find(r=>r.options.method==='POST').options.body).pause,true);
});
test('late resume response cannot clear another task draft or show its error there',async()=>{
 const h=await harness();await h.dialogs.openDirection('A');h.nodes['direction-text'].value='A changes';
 const pending=deferred();h.routes.set('POST /api/tasks/A/directions',pending.promise);
 const posting=h.nodes['direction-form'].emit('submit');await new Promise(setImmediate);
 await h.nodes['direction-dialog'].close();await h.dialogs.openDirection('B');h.nodes['direction-text'].value='B draft';
 pending.resolve(response(task('A',{direction_submission:{status:'resume_blocked',message:'Saved',error:'A resume failed'}})));await posting;
 assert.equal(h.nodes['direction-text'].value,'B draft');assert.equal(h.nodes['direction-error'].hidden,true);
 assert.equal(h.nodes['direction-task-title'].textContent,'Task B');assert.match(h.nodes.toast.textContent,/任务 A/);
});
