import assert from 'node:assert/strict';
import {afterEach,test} from 'node:test';
import {config} from '../console/static/core.js';
import {positionOptions,requirementList,requirementQueueMarkup} from '../console/static/requirement-queue.js';

test('queue page shows the current request and full waiting requirements in execution order',()=>{
 const data={status:'running',original_requirement:'Original',active:{id:'active',text:'CURRENT'},waiting:[
  {id:'A',position:1,text:'First requirement\nfull detail'},
  {id:'B',position:2,text:'Second requirement with <script>not HTML</script>'}],completed:[{id:'done',text:'Delivered requirement'}]};
 const html=requirementQueueMarkup(data);
 assert.match(html,/CURRENT/);assert.match(html,/等待处理 · 2 条/);assert.match(html,/full detail/);
 assert.ok(html.indexOf('First requirement')<html.indexOf('Second requirement'));
 assert.match(html,/Delivered requirement/);assert.doesNotMatch(html,/<script>/);
 assert.match(positionOptions(data.waiting),/第 2 位/);
 assert.doesNotMatch(requirementList(data.waiting),/data-remove-requirement/);
 assert.match(requirementList(data.waiting,{editable:true}),/data-remove-requirement="A"/);
});
test('empty and delivered queue states are explicit',()=>{
 const html=requirementQueueMarkup({status:'done',original_requirement:'Original',active:null,waiting:[],completed:[]});
 assert.match(html,/当前没有正在处理/);assert.match(html,/等待处理 · 0 条/);assert.match(html,/暂无需求/);
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
