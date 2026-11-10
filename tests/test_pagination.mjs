import assert from 'node:assert/strict';
import {test} from 'node:test';
import {Pager} from '../console/static/pagination.js';

test('defaults to ten and keeps the selected page across refreshes',()=>{
  const pager=new Pager();
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
