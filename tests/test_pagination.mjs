import assert from 'node:assert/strict';
import {test} from 'node:test';
import {Pager,observeLayoutChanges} from '../console/static/pagination.js';

test('adapts by default and keeps the selected page across refreshes',()=>{
  const pager=new Pager();
  assert.equal(pager.mode,'auto');
  assert.equal(pager.view(25,6).size,6);
  pager.setMode('10');
  assert.deepEqual(pager.view(25),{page:1,pages:3,size:10,total:25,start:0,end:10});
  pager.page=2;
  assert.equal(pager.view(28).start,10);
  pager.page=3;
  assert.equal(pager.view(28).end,28);
  assert.equal(pager.view(11).page,2);
  assert.deepEqual(pager.view(0),{page:1,pages:1,size:10,total:0,start:0,end:0});
});
test('size changes keep the previous first item in view and adaptive sizes stay bounded',()=>{
  const pager=new Pager();pager.view(100);pager.page=3;
  pager.setMode('20');
  assert.equal(pager.view(100).start,20);
  pager.setMode('auto');
  assert.equal(pager.view(100,5).size,5);
  assert.equal(pager.view(100,1000).size,100);
  assert.equal(pager.view(100,0).size,1);
  pager.setMode('invalid');assert.equal(pager.mode,'auto');
});
test('adaptive resizing keeps the previous first record on the selected page',()=>{
  const pager=new Pager();pager.view(100);pager.page=3;pager.setMode('auto');
  assert.equal(pager.view(100,5).start,20);
  const resized=pager.view(100,8);
  assert.ok(resized.start<=20&&resized.end>20);
});
test('window, visual viewport, zoom and container resizing trigger layout updates',()=>{
  const original=Object.fromEntries(['window','document','ResizeObserver','setTimeout','clearTimeout'].map(key=>[key,globalThis[key]]));
  let scheduled,observer,updates=0,disconnected=false;
  const target=()=>({handlers:new Map(),addEventListener(type,fn){this.handlers.set(type,fn);},removeEventListener(type){this.handlers.delete(type);}});
  const viewport=target(),window=target(),queries=[];
  window.visualViewport=viewport;window.devicePixelRatio=1;
  window.matchMedia=query=>{const item={...target(),query};queries.push(item);return item;};
  const main={getBoundingClientRect:()=>({width:1000})};
  globalThis.window=window;globalThis.document={querySelector:()=>main};
  globalThis.setTimeout=callback=>{scheduled=callback;return 1;};
  globalThis.clearTimeout=()=>{scheduled=null;};
  globalThis.ResizeObserver=class{constructor(callback){observer=callback;}observe(el){assert.equal(el,main);}disconnect(){disconnected=true;}};
  const flush=()=>{const fn=scheduled;scheduled=null;fn?.();};
  let stop;
  try{
    stop=observeLayoutChanges(()=>updates++);
    window.handlers.get('resize')();flush();assert.equal(updates,1);
    viewport.handlers.get('resize')();flush();assert.equal(updates,2);
    window.devicePixelRatio=2;queries[0].handlers.get('change')();flush();
    assert.equal(updates,3);assert.match(queries[1].query,/2dppx/);
    observer([{contentRect:{width:600}}]);flush();assert.equal(updates,4);
    observer([{contentRect:{width:600}}]);flush();assert.equal(updates,4);
    stop();stop=null;assert.equal(disconnected,true);assert.equal(window.handlers.size,0);assert.equal(viewport.handlers.size,0);
  }finally{
    stop?.();
    for(const [key,value] of Object.entries(original)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
  }
});
