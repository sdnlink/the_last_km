import english from './translations.js';

export function language(){return globalThis.document?.documentElement?.lang?.startsWith('en')?'en':'zh-CN';}
export function locale(){return language()==='en'?'en-US':'zh-CN';}
const normalize=value=>value.replace(/\s+/g,' ').trim();
function translated(value){
  if(language()!=='en')return value;
  const match=/^(\s*)([\s\S]*?)(\s*)$/.exec(value),key=normalize(match[2]);
  return Object.hasOwn(english,key)?match[1]+english[key]+match[3]:value;
}
function template(source,values){
  if(typeof source==='string')return {source,values:[]};
  return {source:source.map((part,index)=>part+(index<values.length?`{${index}}`:'')).join(''),values};
}
const substitute=(source,values)=>{
  // Inflect only catalog text, before inserting user content.
  if(language()==='en')source=source.replace(/\{(\d+)\} (rounds|tasks|cores|functions|items)\b/g,
    (match,index,word)=>Number(values[index])===1?`{${index}} ${word.slice(0,-1)}`:match);
  return source.replace(/\{(\d+)\}/g,(match,index)=>index<values.length?String(values[index]??''):match);
};
export function i18n(source,...values){const text=template(source,values);return substitute(translated(text.source),text.values);}
const escapeText=value=>value.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const escapeAttribute=value=>escapeText(value).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
export function localizeHTML(source){
  if(language()!=='en')return source;
  return source.replace(/<!--[\s\S]*?-->|<[^>]*>|[^<]+/g,part=>{
    if(part.startsWith('<!--'))return part;
    if(part.startsWith('<'))return part.replace(/(\b(?:title|placeholder|aria-label|alt|label)\s*=\s*)(["'])([\s\S]*?)\2/g,
      (all,prefix,quote,value)=>{const result=translated(value);return result===value?all:prefix+quote+escapeAttribute(result)+quote;});
    const result=translated(part);return result===part?part:escapeText(result);
  });
}
export function i18nHTML(source,...values){const text=template(source,values);return substitute(localizeHTML(text.source),text.values);}
export function initializeLanguage(){
  const field=document.querySelector('#language-select');if(!field)return;
  field.value=language();
  field.addEventListener('change',()=>{
    const value=field.value==='en'?'en':'zh-CN';
    document.cookie=`auto_moniter_lang=${value}; Path=/; Max-Age=31536000; SameSite=Lax`;
    location.reload();
  });
}
