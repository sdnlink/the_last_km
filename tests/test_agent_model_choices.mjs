import assert from 'node:assert/strict';
import {test} from 'node:test';
import {modelChoices,selectedModels} from '../console/static/dialogs.js';

const cfg={agent_catalog:{models:{grok:{models:[{id:'grok-4.7',label:'Grok'}]},codex:{models:[{id:'sol',label:'Sol'}]}},paseo_backends:[{id:'claude',available:true,models:[{id:'opus',label:'Opus'}]}]}};
function form(){return {elements:{provider:{value:'grok'},reviewer:{value:'codex'},model:{value:''},reviewer_model:{value:''},model_custom:{value:'',hidden:true},reviewer_model_custom:{value:'',hidden:true},paseo_backend:{value:'grok'},reviewer_paseo_backend:{value:'claude'}},querySelector(){return null;}};}

test('model choices follow each role and reset invalid models on agent switch',()=>{
 const f=form();modelChoices(f,cfg);assert.match(f.elements.model.innerHTML,/grok-4.7/);assert.doesNotMatch(f.elements.reviewer_model.innerHTML,/grok-4.7/);
 f.elements.model.value='grok-4.7';f.elements.provider.value='codex';modelChoices(f,cfg);
 assert.equal(f.elements.model.value,'');assert.match(f.elements.model.innerHTML,/sol/);
});
test('saved models outside the current catalog remain selectable and escaped',()=>{
 const f=form();modelChoices(f,cfg,{preserve:true,values:{model:'custom<&"'}});
 assert.equal(f.elements.model.value,'custom<&"');assert.match(f.elements.model.innerHTML,/custom&lt;&amp;&quot;/);
});
test('Paseo models use the reviewer backend instead of worker backend',()=>{
 const f=form();f.elements.reviewer.value='paseo';modelChoices(f,cfg);
 assert.match(f.elements.reviewer_model.innerHTML,/opus/);assert.doesNotMatch(f.elements.reviewer_model.innerHTML,/grok-4.7/);
});
test('custom model values become CLI model IDs, with empty IDs rejected',()=>{
 const f=form();f.elements.model_custom.value=' custom-id ';
 assert.deepEqual(selectedModels(f,{model:'__custom_model__',reviewer_model:'sol',model_custom:' custom-id '}),{model:'custom-id',reviewer_model:'sol'});
 f.elements.model_custom.value='';assert.throws(()=>selectedModels(f,{model:'__custom_model__'}),/自定义模型/);
});
