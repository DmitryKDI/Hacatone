// Unit regression using a minimal DOM double; this is not visual/mobile acceptance.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

for (const warning of [true, false]) {
  test(`failed generation preserves ML message and warning=${warning}`, async () => {
    function element() {
      return {value:'', hidden:false, disabled:false, children:[], textContent:'',
        append(child){this.children.push(child);}, replaceChildren(){this.children=[];},
        addEventListener(){}};
    }
    const elements = new Map();
    const byId = id => {if(!elements.has(id)) elements.set(id,element());return elements.get(id);};
    byId('vehicle').value='TEST';byId('session').value='S-TEST';
    const document = {getElementById:byId, createElement:element};
    const calls=[];
    async function fetch(path, options) {
      calls.push(path);
      const bodies={
        '/api/session':{csrf:'test',revision:0,provider:'openai',corpus_ready:true},
        '/api/catalog':[{vehicle_id:'TEST',make:'TEST',model:null,engine_model:'TEST',sessions:[{session_id:'S-TEST',checked_at:'2026-11-26T10:00:00+03:00'}]}],
        '/api/select':{revision:1},
        '/api/action':{message:'Результат ML получен.',warning,error:'LLM недоступна.'}
      };
      assert.ok(path in bodies);
      return {ok:path!=='/api/action',json:async()=>bodies[path]};
    }
    const context=vm.createContext({document,fetch});
    vm.runInContext(fs.readFileSync('mechanic/static/app.js','utf8'),context);
    await new Promise(setImmediate); // Let the actual initialization promise chain finish.
    await vm.runInContext("action('diagnose')",context);
    const messages=byId('messages').children;
    assert.ok(messages.some(n=>n.textContent==='Результат ML получен.'));
    assert.ok(messages.some(n=>n.className==='message error' && n.textContent==='LLM недоступна.'));
    assert.equal(messages.filter(n=>n.className==='message warning').length,warning?1:0);
    assert.equal(byId('preview').hidden,true);
    assert.equal(byId('draft').hidden,true);
    assert.equal(byId('diagnose').disabled,false);
    assert.ok(!calls.includes('/api/send'));
  });
}

test('conflicting instructions show both literal quotes and source pages without a draft', async () => {
  function element() {
    return {value:'',hidden:false,disabled:false,children:[],textContent:'',
      append(child){this.children.push(child);},replaceChildren(){this.children=[];},addEventListener(){}};
  }
  const elements=new Map();
  const byId=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  byId('vehicle').value='TEST';byId('session').value='S-TEST';
  const quotes=['<test> Первая спорная формулировка.','Вторая спорная формулировка.'];
  const bodies={
    '/api/session':{csrf:'test',revision:0,provider:'openai',corpus_ready:true},
    '/api/catalog':[{vehicle_id:'TEST',make:'TEST',model:null,engine_model:'TEST',sessions:[{session_id:'S-TEST',checked_at:'2026-11-26T10:00:00+03:00'}]}],
    '/api/select':{revision:1},
    '/api/action':{status:'needs_clarification',message:'Противоречие: уточните у производителя.',claims:[],draft:null,can_draft:false,
      conflicts:[{kind:'uncertain_applicability',sources:[
        {quote:quotes[0],manual:'Документ А',doc:'a',pdf_page:19},
        {quote:quotes[1],manual:'Документ Б',doc:'b',pdf_page:20}]}]}
  };
  const calls=[];
  const context=vm.createContext({document:{getElementById:byId,createElement:element},fetch:async path=>{
    calls.push(path);assert.ok(path in bodies);return {ok:true,json:async()=>bodies[path]};
  }});
  vm.runInContext(fs.readFileSync('mechanic/static/app.js','utf8'),context);
  await new Promise(setImmediate);
  await vm.runInContext("action('diagnose')",context);
  const displayed=byId('messages').children.flatMap(n=>n.children);
  assert.deepEqual(displayed.filter(n=>n.textContent===quotes[0]||n.textContent===quotes[1]).map(n=>n.textContent),quotes);
  assert.deepEqual(displayed.filter(n=>n.className==='source').map(n=>n.href),['/manual/a/19','/manual/b/20']);
  assert.equal(byId('draft').hidden,true);assert.equal(byId('preview').hidden,true);
  assert.ok(!calls.includes('/api/send'));
});

async function uiContext(actionResult, selectionFails=false, manualPages=true) {
  function element(tagName='div') {
    return {tagName,value:'',hidden:false,disabled:false,children:[],textContent:'',
      append(child){this.children.push(child);},replaceChildren(){this.children=[];},addEventListener(){},scrollIntoView(){}};
  }
  const elements=new Map();const byId=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  byId('vehicle').value='TEST';byId('session').value='S-TEST';
  const calls=[];const bodies={
    '/api/session':{csrf:'test',revision:0,provider:'openai',corpus_ready:true,manual_pages:manualPages},
    '/api/catalog':[{vehicle_id:'TEST',make:'TEST',model:null,engine_model:'TEST',sessions:[{session_id:'S-TEST',checked_at:'2026-11-26T10:00:00+03:00'}]}],
    '/api/select':selectionFails?{detail:'Контекст не выбран'}:{revision:1},'/api/action':actionResult};
  const context=vm.createContext({document:{getElementById:byId,createElement:element},fetch:async path=>{
    calls.push(path);assert.ok(path in bodies);return {ok:!(selectionFails&&path==='/api/select'),json:async()=>bodies[path]};
  }});
  vm.runInContext(fs.readFileSync('mechanic/static/app.js','utf8'),context);await new Promise(setImmediate);
  return {context,byId,calls};
}

test('unconfirmed selection blocks all actions instead of diagnosing the previous vehicle', async () => {
  const {context,byId,calls}=await uiContext({},true);
  assert.equal(byId('diagnose').disabled,true);assert.equal(byId('ask').disabled,true);
  await vm.runInContext("action('diagnose')",context);
  assert.ok(!calls.includes('/api/action'));
});

test('compact verdict and six ordered detail sections retain literal evidence', async () => {
  const result={message:'Предварительный результат',summary:{vehicle_id:'TEST',session_id:'S-TEST',status:'Отклонение',
    type:'Тестовый тип',confidence:'69.99%',risk:'Риск не определён',next_action:'Уточнить по источникам'},
    observations:'Синтетическое наблюдение ML',claims:[{section:'checks',text:'Синтетическая проверка',quote:'Точная тестовая выдержка.',manual:'UI-документ',doc:'test',pdf_page:1}],can_draft:false};
  const {context,byId}=await uiContext(result);
  await vm.runInContext("action('diagnose')",context);
  function all(node){return [node,...node.children.flatMap(all)];}
  const nodes=all(byId('messages'));
  assert.deepEqual(nodes.filter(n=>n.tagName==='dt').map(n=>n.textContent),['Статус','Тип','Уверенность ML','Риск','Следующее действие']);
  assert.ok(nodes.some(n=>n.textContent==='69.99%'));
  assert.deepEqual(nodes.filter(n=>n.tagName==='h4').map(n=>n.textContent),['Наблюдения','Гипотезы','Проверки','Возможный ремонт','Источники','Ограничения']);
  assert.ok(nodes.some(n=>n.textContent==='Точная тестовая выдержка.'));
  assert.ok(nodes.some(n=>n.href==='/manual/test/1'));
});

test('disabled manual download preserves the document and page label', async () => {
  const result = {claims:[{section:'checks',text:'Действие',quote:'Точная выдержка.',
    manual:'Документ',doc:'test',pdf_page:19}]};
  const {context,byId}=await uiContext(result,false,false);
  await vm.runInContext("action('chat','Проверка')",context);
  function all(node){return [node,...node.children.flatMap(all)];}
  const nodes = all(byId('messages'));
  assert.ok(nodes.some(node=>node.textContent==='Документ · PDF-страница 19'));
  assert.ok(!nodes.some(node=>node.href));
});
