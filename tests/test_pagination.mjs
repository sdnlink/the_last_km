import assert from 'node:assert/strict';
import {test} from 'node:test';
import {Pager,pageView,getPager,onPaginationChange} from '../console/static/pagination.js';

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
test('initial size correction keeps the previous first record on the selected page',()=>{
  const pager=new Pager();pager.view(100);pager.page=3;pager.setMode('auto');
  assert.equal(pager.view(100,5).start,20);
  const resized=pager.view(100,8);
  assert.ok(resized.start<=20&&resized.end>20);
});
test('page size is measured at load and stays fixed through polling and zoom',()=>{
  const original=Object.fromEntries(['window','document','requestAnimationFrame','getComputedStyle'].map(key=>[key,globalThis[key]]));
  let height=800,rowHeight=40,measurements=0,rootReads=0;
  const frames=[],handlers=[];
  const first={matches:()=>false,getBoundingClientRect:()=>{measurements++;return {height:rowHeight};}};
  const root={firstElementChild:first,getBoundingClientRect:()=>({top:100})};
  globalThis.window={get innerHeight(){return height;},visualViewport:{get height(){return height;},offsetTop:0},
    addEventListener(){assert.fail('Pagination must not watch viewport changes');},
    matchMedia(){assert.fail('Pagination must not watch browser zoom');}};
  globalThis.document={querySelector:()=>{rootReads++;return root;},addEventListener:(...args)=>handlers.push(args)};
  globalThis.requestAnimationFrame=callback=>frames.push(callback);
  globalThis.getComputedStyle=()=>({display:'block'});
  try{
    onPaginationChange(()=>{});
    assert.deepEqual(handlers.map(([type])=>type),['click','change']);
    const firstView=pageView('load-only',100,{rowHeight:40});
    assert.equal(firstView.size,12);assert.equal(frames.length,1);
    // The initial render can reveal a different row height exactly once.
    rowHeight=50;frames.shift()();
    const settled=pageView('load-only',100,{rowHeight:40});
    assert.equal(settled.size,10);assert.equal(frames.length,0);
    const reads=rootReads,measured=measurements;
    height=1600;rowHeight=20;getPager('load-only').page=2;
    for(let n=0;n<3;n++){
      const view=pageView('load-only',110,{rowHeight:40});
      assert.equal(view.size,10);assert.equal(view.page,2);
    }
    assert.equal(rootReads,reads);assert.equal(measurements,measured);assert.equal(frames.length,0);
    const reloaded=pageView('fresh-document',100,{rowHeight:40});
    assert.ok(reloaded.size>settled.size,'A newly loaded page uses its current viewport');
    frames.shift()();
  }finally{
    for(const [key,value] of Object.entries(original)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
  }
});
