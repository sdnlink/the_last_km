import {escape} from './ui.js';

export class Pager {
  constructor(){this.page=1;this.mode='auto';this.total=0;this.size=10;}
  view(total,autoSize=10){
    this.total=Math.max(0,total);
    const nextSize=this.mode==='auto'?Math.max(1,Math.min(100,Math.floor(autoSize))):Number(this.mode);
    if(nextSize!==this.size)this.page=Math.floor((this.page-1)*this.size/nextSize)+1;
    this.size=nextSize;
    const pages=Math.max(1,Math.ceil(this.total/this.size));
    this.page=Math.max(1,Math.min(pages,this.page));
    const start=(this.page-1)*this.size;
    return {page:this.page,pages,size:this.size,total:this.total,start,end:Math.min(this.total,start+this.size)};
  }
  setMode(mode){
    if(!['10','20','50','auto'].includes(mode))return;
    const start=(this.page-1)*this.size;
    this.mode=mode;
    if(mode!=='auto')this.size=Number(mode);
    this.page=Math.floor(start/this.size)+1;
  }
}

const pagers=new Map(),layouts=new Map(),listeners=new Set();
const adaptiveSizes=new Map(),initialized=new Set(),measurementPending=new Set();
let bound=false;
export function getPager(key){if(!pagers.has(key))pagers.set(key,new Pager());return pagers.get(key);}
export function resetPage(key){getPager(key).page=1;}
function fitSize(key){
  const layout=layouts.get(key)||{},root=document.querySelector(`[data-page-list="${key}"]`);
  const viewport=window.visualViewport?.height||window.innerHeight||800;
  const top=(root?.getBoundingClientRect().top||0)-(window.visualViewport?.offsetTop||0);
  const height=Math.max(160,viewport-Math.max(0,Math.min(top,viewport*.35))-100);
  let rowHeight=layout.rowHeight||64,columns=1;
  if(layout.rowHeight===20)rowHeight=parseFloat(root&&getComputedStyle(root).lineHeight)||20;
  const first=root?.firstElementChild;
  if(first){
    const row=first.matches('.conclusion-round')?first.querySelector('summary'):first;
    const measured=row.getBoundingClientRect().height;
    if(measured>0)rowHeight=measured+8;
    if(getComputedStyle(root).display==='grid')columns=getComputedStyle(root).gridTemplateColumns.split(' ').length;
  }
  return Math.max(1,Math.min(100,Math.floor(height/rowHeight)*columns));
}
export function pageView(key,total,options={}){
  layouts.set(key,options);
  const pager=getPager(key);
  if(!adaptiveSizes.has(key))adaptiveSizes.set(key,fitSize(key));
  const view=pager.view(total,adaptiveSizes.get(key));
  // Measure once after the initial records render, then keep this size until reload.
  if(total>0&&!initialized.has(key)&&!measurementPending.has(key)){
    measurementPending.add(key);
    requestAnimationFrame(()=>{
      measurementPending.delete(key);
      initialized.add(key);
      const next=fitSize(key);
      adaptiveSizes.set(key,next);
      if(pager.mode==='auto'&&next!==view.size)notify(key,'layout');
    });
  }
  return view;
}
export function controlsHTML(key,view,{unit='条'}={}){
  if(!view.total)return '';
  const pager=getPager(key),escaped=escape(key);
  return `<nav class="pagination" data-pagination="${escaped}" aria-label="记录分页"><span class="pagination-count">第 ${view.start+1}–${view.end} ${unit} / 共 ${view.total} ${unit}</span><label>每页 <select data-page-size aria-label="每页展示数量">${['auto','10','20','50'].map(mode=>`<option value="${mode}" ${pager.mode===mode?'selected':''}>${mode==='auto'?`随页面（${view.size} ${unit}）`:mode+' '+unit}</option>`).join('')}</select></label><div class="pagination-actions"><button type="button" data-page-number="1" ${view.page===1?'disabled':''} aria-label="第一页">«</button><button type="button" data-page-number="${view.page-1}" ${view.page===1?'disabled':''}>上一页</button><label>第 <input type="number" data-page-jump min="1" max="${view.pages}" value="${view.page}" aria-label="跳转页码" /> / ${view.pages} 页</label><button type="button" data-page-number="${view.page+1}" ${view.page===view.pages?'disabled':''}>下一页</button><button type="button" data-page-number="${view.pages}" ${view.page===view.pages?'disabled':''} aria-label="最后一页">»</button></div></nav>`;
}
export function paginate(key,items,options={}){
  const view=pageView(key,items.length,options);
  return {...view,items:items.slice(view.start,view.end),controls:controlsHTML(key,view,options)};
}
export function pagedList(key,items,render,{tag='ul',className='module-list',empty='',...options}={}){
  const page=paginate(key,items,options);
  return `<${tag} class="${className}" data-page-list="${escape(key)}" ${tag==='ol'?`start="${page.start+1}"`:''}>${page.items.map(render).join('')||empty}</${tag}>${page.controls}`;
}
function notify(key,reason){for(const callback of listeners)callback(key,reason);}
export function onPaginationChange(callback){
  listeners.add(callback);
  if(bound)return;
  bound=true;
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-pagination] button[data-page-number]');
    if(!button||button.disabled)return;
    const key=button.closest('[data-pagination]').dataset.pagination;
    getPager(key).page=Number(button.dataset.pageNumber);notify(key,'page');
  });
  document.addEventListener('change',event=>{
    const control=event.target,nav=control.closest('[data-pagination]');if(!nav)return;
    const key=nav.dataset.pagination,pager=getPager(key);
    if(control.matches('[data-page-size]'))pager.setMode(control.value);
    else if(control.matches('[data-page-jump]')){const page=Number(control.value);if(!Number.isInteger(page)||page<1)return;pager.page=page;}
    else return;
    notify(key,control.matches('[data-page-size]')?'size':'page');
  });
}
