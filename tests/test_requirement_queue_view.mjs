import assert from 'node:assert/strict';
import {afterEach,test} from 'node:test';
import {config} from '../console/static/core.js';
import {positionOptions,requirementList,requirementQueueMarkup} from '../console/static/requirement-queue.js';

test('queue page shows complete waiting requests newest first with their execution positions',()=>{
 const data={status:'running',original_requirement:'Original',active:{id:'active',text:'CURRENT'},waiting:[
  {id:'A',position:1,text:'First requirement\nfull detail',at:'2026-11-27T10:00:00.123456Z'},
  {id:'B',position:2,text:'Second requirement with <script>not HTML</script>',at:'2026-11-27T18:00:00.123457+08:00'}],
  completed:[{id:'old',text:'Earlier delivery',at:'2026-11-27T15:00:00+08:00'},
   {id:'recent',text:'Delivered requirement',at:'2026-11-27T08:00:00Z'}]};
 const html=requirementQueueMarkup(data);
 assert.match(html,/CURRENT/);assert.match(html,/等待处理 · 2 条/);assert.match(html,/full detail/);
 assert.match(html,/<strong>第 2 位<\/strong>.*Second requirement/);
 assert.ok(html.indexOf('Second requirement')<html.indexOf('First requirement'));
 assert.ok(html.indexOf('Delivered requirement')<html.indexOf('Earlier delivery'));
 assert.match(html,/Delivered requirement/);assert.doesNotMatch(html,/<script>/);
 assert.ok(positionOptions(data.waiting).indexOf('第 1 位')<positionOptions(data.waiting).indexOf('第 2 位'));
 assert.deepEqual(data.waiting.map(item=>item.id),['A','B']);
 assert.doesNotMatch(requirementList(data.waiting),/data-remove-requirement/);
 assert.match(requirementList(data.waiting,{editable:true}),/data-remove-requirement="A"/);
});
test('empty and delivered queue states are explicit',()=>{
 const html=requirementQueueMarkup({status:'done',original_requirement:'Original',active:null,waiting:[],completed:[]});
 assert.match(html,/当前没有正在处理/);assert.match(html,/等待处理 · 0 条/);assert.match(html,/暂无需求/);
});
test('the queue shows planning status and linked acceptance supplements',()=>{
 const html=requirementQueueMarkup({status:'running',original_requirement:'Original',waiting:[],completed:[],
  amendments:[{text:'<Pending>',planned:false,at:'2026-11-27T10:00:00Z'},
   {text:'Reviewed',planned:true,at:'2026-11-27T11:00:00Z'}],
  check_supplements:[{cycle:1,task_id:'V2-Verify',criterion_ids:['req:1:2'],tree:'123456789abcde',count:1,at:'2026-11-27T10:00:00Z'},
   {cycle:1,task_id:'V3-Verify',criterion_ids:['req:1:3'],tree:'abcdef01234567',count:1,at:'2026-11-27T11:00:00Z'}]});
 assert.match(positionOptions([]),/value="current"/);
 assert.match(html,/等待 Reviewer 规划.*&lt;Pending&gt;/);
 assert.match(html,/已纳入评审计划.*Reviewed/);
 assert.match(html,/V2-Verify.*req:1:2.*123456789abc/);
 assert.ok(html.indexOf('Reviewed')<html.indexOf('&lt;Pending&gt;'));
 assert.ok(html.indexOf('V3-Verify')<html.indexOf('V2-Verify'));
});

const globals=['document','fetch','localStorage','setTimeout','clearTimeout'];
const originals=Object.fromEntries(globals.map(key=>[key,globalThis[key]]));
let serial=0;
afterEach(()=>{for(const [key,value] of Object.entries(originals)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}});
function element(){
 const listeners=new Map();
 return {value:'',open:false,disabled:false,hidden:false,dataset:{},textContent:'',innerHTML:'',scrollTop:0,
  classList:{toggle(){}},querySelectorAll:()=>[],insertAdjacentHTML(){},
  addEventListener(type,fn){listeners.set(type,[...(listeners.get(type)||[]),fn]);},
  async emit(type){for(const fn of listeners.get(type)||[])await fn({target:this,preventDefault(){}});},
  showModal(){this.open=true;},async close(){this.open=false;await this.emit('close');}};
}
const response=value=>({ok:true,status:200,json:async()=>value});
function queue(id){return {id,title:'Task '+id,status:'running',can_append:true,original_requirement:'Original '+id,
 active:null,waiting:[{id:id+'1',text:'Waiting '+id,position:1}],completed:[]};}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
async function harness(){
 const ids=['dialog-styles','dialog-root','toast','requirement-queue-dialog','requirement-queue-form','requirement-queue-title',
  'requirement-queue-notice','requirement-queue-current','requirement-queue-waiting','requirement-queue-completed',
  'requirement-queue-completed-title','requirement-queue-text','requirement-queue-position','requirement-queue-error','requirement-queue-submit'];
 const nodes=Object.fromEntries(ids.map(id=>[id,element()])),routes=new Map(),storage=new Map(),requests=[];
 globalThis.document={hidden:false,querySelector:selector=>nodes[selector.slice(1)]||null};
 globalThis.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
 globalThis.setTimeout=(fn,delay,...args)=>[3000,5000].includes(delay)?-1:originals.setTimeout(fn,delay,...args);
 globalThis.clearTimeout=id=>{if(id!==-1)originals.clearTimeout(id);};
 globalThis.fetch=async(url,options={})=>{
  requests.push({url,options});const key=`${options.method||'GET'} ${url}`;
  if(routes.has(key))return routes.get(key);
  if(url==='/api/config')return response({csrf_token:'test'});
  if(url.startsWith('/fragments/'))return {ok:true,text:async()=>''};
  return response(queue(url.split('/')[3]));
 };
 await config(true);
 const module=await import(`../console/static/requirement-queue.js?lifecycle=${++serial}`);
 return {nodes,routes,storage,requests,module};
}
test('the queue dialog also shows newest requests first without changing insertion options',async()=>{
 const h=await harness(),value=queue('A');
 value.waiting[0].at='2026-11-27T10:00:00Z';
 value.waiting.push({id:'A2',text:'Newest A',position:2,at:'2026-11-27T11:00:00Z'});
 h.routes.set('GET /api/tasks/A/requirement-queue',response(value));
 await h.module.openRequirementQueue('A');
 const waiting=h.nodes['requirement-queue-waiting'].innerHTML;
 const options=h.nodes['requirement-queue-position'].innerHTML;
 assert.ok(waiting.indexOf('Newest A')<waiting.indexOf('Waiting A'));
 assert.ok(options.indexOf('Waiting A')<options.indexOf('Newest A'));
});
test('late queue reads do not overwrite a newly opened task',async()=>{
 const h=await harness(),old=deferred();h.routes.set('GET /api/tasks/A/requirement-queue',old.promise);
 const opening=h.module.openRequirementQueue('A');await new Promise(setImmediate);
 await h.nodes['requirement-queue-dialog'].close();await h.module.openRequirementQueue('B');
 old.resolve(response(queue('A')));await opening;
 assert.equal(h.nodes['requirement-queue-title'].textContent,'Task B');
 assert.match(h.nodes['requirement-queue-waiting'].innerHTML,/Waiting B/);
});
test('late append results preserve the new task draft and never retarget the write',async()=>{
 const h=await harness();await h.module.openRequirementQueue('A');
 h.nodes['requirement-queue-text'].value='Append to A';await h.nodes['requirement-queue-text'].emit('input');
 const old=deferred();h.routes.set('POST /api/tasks/A/requirement-queue',old.promise);
 const posting=h.nodes['requirement-queue-form'].emit('submit');await new Promise(setImmediate);
 await h.nodes['requirement-queue-dialog'].close();await h.module.openRequirementQueue('B');
 h.nodes['requirement-queue-text'].value='New B draft';await h.nodes['requirement-queue-text'].emit('input');
 old.resolve(response(queue('A')));await posting;
 const posts=h.requests.filter(row=>row.options.method==='POST');
 assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/tasks/A/requirement-queue');
 assert.deepEqual(JSON.parse(posts[0].options.body),{text:'Append to A',position:'last'});
 assert.equal(h.nodes['requirement-queue-title'].textContent,'Task B');
 assert.equal(h.nodes['requirement-queue-text'].value,'New B draft');
 assert.equal(h.storage.get('auto-moniter-requirement-queue-B'),'New B draft');
 assert.equal(h.nodes['requirement-queue-submit'].disabled,false);
});
