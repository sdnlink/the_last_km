import assert from 'node:assert/strict';
import {test} from 'node:test';
import {modelChoices,selectedModels} from '../console/static/dialogs.js';

const catalog={agent_catalog:{models:{codex:{models:[{id:'model-a'},{id:'model-b'}]}},paseo_backends:[{id:'claude',models:[{id:'opus'}]}]}};
function form(){return {elements:{reviewer:{value:'codex'},reviewer_model:{value:'model-a'},referee_model:{value:''},referee_model_custom:{value:'',hidden:true}},querySelector(){return null;}};}

test('referee selects an independent model from its reviewer provider catalog',()=>{
 const f=form();modelChoices(f,catalog,{roles:['referee']});
 assert.match(f.elements.referee_model.innerHTML,/model-a/);
 assert.match(f.elements.referee_model.innerHTML,/model-b/);
 f.elements.referee_model.value='model-b';
 const data=selectedModels(f,{reviewer_model:'model-a',referee_model:'model-b'});
 assert.equal(data.reviewer_model,'model-a');assert.equal(data.referee_model,'model-b');
});

test('resume model catalog uses the saved provider without hidden provider controls',()=>{
 const f=form();delete f.elements.reviewer;delete f.elements.reviewer_model;
 modelChoices(f,catalog,{roles:['referee'],preserve:true,values:{reviewer:'paseo',reviewer_paseo_backend:'claude',referee_model:'saved-model'}});
 assert.match(f.elements.referee_model.innerHTML,/opus/);
 assert.equal(f.elements.referee_model.value,'saved-model');
});

test('custom referee model is serialized as a model id and never as a form sentinel',()=>{
 const f=form();f.elements.referee_model_custom.value=' independent-model ';
 assert.deepEqual(selectedModels(f,{referee_model:'__custom_model__',referee_model_custom:' independent-model '}),{referee_model:'independent-model'});
 f.elements.referee_model_custom.value='';
 assert.throws(()=>selectedModels(f,{referee_model:'__custom_model__'}),/自定义模型/);
});
