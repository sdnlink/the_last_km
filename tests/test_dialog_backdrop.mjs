import assert from 'node:assert/strict';
import {test,afterEach} from 'node:test';
import {initialize} from '../console/static/core.js';

afterEach(()=>{delete globalThis.document;delete globalThis.window;});
test('outside click closes the open dialog, while internal clicks and drags stay open',async()=>{
  const handlers=new Map();
  const dialog={open:true,getBoundingClientRect:()=>({left:100,right:500,top:100,bottom:400}),close(){this.open=false;}};
  globalThis.document={body:{dataset:{workspace:'/tmp',page:'tasks'}},
    querySelector:()=>({}),querySelectorAll:selector=>selector==='dialog[open]'&&dialog.open?[dialog]:[],
    addEventListener:(type,callback)=>handlers.set(type,callback)};
  globalThis.window={addEventListener:()=>{}};
  initialize();
  const event=(x,y)=>({button:0,clientX:x,clientY:y,target:{closest:()=>null}});
  handlers.get('pointerdown')(event(200,200));await handlers.get('click')(event(200,200));
  assert.equal(dialog.open,true,'clicking inside must preserve the dialog');
  handlers.get('pointerdown')(event(200,200));await handlers.get('click')(event(50,50));
  assert.equal(dialog.open,true,'dragging outside must preserve the dialog');
  for(const [x,y] of [[50,200],[600,200],[200,50],[200,500]]){
    dialog.open=true;handlers.get('pointerdown')(event(x,y));await handlers.get('click')(event(x,y));
    assert.equal(dialog.open,false,'every side of the backdrop closes the dialog');
  }
});
