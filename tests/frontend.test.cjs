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
    byId('session').value='S-TEST';
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
    vm.runInContext(fs.readFileSync('mechanic/static/vehicle-query.js','utf8'),context);
  vm.runInContext(fs.readFileSync('mechanic/static/app.js','utf8'),context);
    await new Promise(setImmediate); // Let the actual initialization promise chain finish.
    await vm.runInContext("action('diagnose')",context);
    const messages=byId('messages').children;
    assert.ok(messages.flatMap(n=>[n,...n.children]).some(n=>n.textContent==='Результат ML получен.'));
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
  byId('session').value='S-TEST';
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
  vm.runInContext(fs.readFileSync('mechanic/static/vehicle-query.js','utf8'),context);
  vm.runInContext(fs.readFileSync('mechanic/static/app.js','utf8'),context);
  await new Promise(setImmediate);
  await vm.runInContext("action('diagnose')",context);
  const displayed=byId('messages').children.flatMap(n=>n.children);
  assert.deepEqual(displayed.filter(n=>n.textContent===quotes[0]||n.textContent===quotes[1]).map(n=>n.textContent),quotes);
  assert.deepEqual(displayed.filter(n=>n.className==='source').map(n=>n.href),['/manual/a/19','/manual/b/20']);
  assert.equal(byId('draft').hidden,true);assert.equal(byId('preview').hidden,true);
  assert.ok(!calls.includes('/api/send'));
});

const testFleet = [{vehicle_id:'TEST',make:'TEST',model:null,engine_model:'TEST',
  sessions:[{session_id:'S-TEST',checked_at:'2026-11-26T10:00:00+03:00'}]}];

for (const intent of ['diagnose','chat']) {
  for (const blockedBy of ['conflicts','failure']) {
    test(`${intent}: ${blockedBy} does not replace the aggregate anomaly`, async () => {
      const result={message:'Противоречие в руководствах. Уточните исполнение.',
        summary:{status:'Отклонение',type:'F04 · Ремённый привод агрегатов',confidence:'88%',next_action:'Уточните исполнение'},
        observations:'Свист в передней части двигателя',claims:[],can_draft:false,ticket_status:'not_sent'};
      if (blockedBy === 'conflicts') result.conflicts=[{kind:'uncertain_applicability',sources:[]}];
      else result.failure={status:'Ответ не подтверждён',next_action:'Уточните исполнение'};
      const {context,byId,calls}=await uiContext(result);
      await vm.runInContext(`action('${intent}', 'Как поменять ремень?')`,context);
      const cards=byId('messages').children;
      assert.equal(byId('diagnosis-status').textContent,'Отклонение');
      assert.equal(byId('diagnosis-type').textContent,'F04 · Ремённый привод агрегатов');
      assert.equal(byId('diagnosis-confidence').textContent,'Уверенность ML: 88%');
      assert.equal(byId('diagnosis-observations').textContent,'По данным ML: Свист в передней части двигателя');
      assert.equal(byId('diagnosis-panel').hidden,false);
      assert.ok(!cards.flatMap(n=>[n,...n.children]).some(n=>n.textContent==='Отклонение'));
      assert.ok(cards.some(n=>n.className==='message warning'||n.className.includes('attention')));
      assert.equal(byId('draft').hidden,true);
      assert.ok(!calls.includes('/api/send'));
    });
  }
}

test('verification failure keeps ML result and reason in the same reply', async () => {
  const data={message:'Выявлено отклонение: Ремённый привод агрегатов.',
    error:'Цитата не подтверждает рекомендацию.',
    failure:{status:'Рекомендации не прошли проверку по руководству',next_action:'Уточните условия.'}};
  const {context,byId}=await uiContext(data);
  context.failureResult=data;
  vm.runInContext("renderReply(failureResult, 'diagnose')",context);
  const reply=byId('messages').children.at(-1);
  assert.ok(reply.children.some(n=>n.textContent===data.message));
  assert.ok(reply.children.some(n=>n.textContent===data.error));
});

test('followups retain one pinned diagnosis without echoing it and changing the pair clears it', async () => {
  const result={summary:{status:'Отклонение',type:'F04 · Ремённый привод агрегатов',confidence:'88%'},
    observations:'Свист в передней части двигателя',claims:[],can_draft:false};
  const {context,byId}=await uiContext(result);
  await vm.runInContext("action('diagnose')",context);
  await vm.runInContext("action('chat', 'Как поменять ремень?')",context);
  assert.equal(byId('diagnosis-panel').hidden,false);
  assert.equal(byId('diagnosis-type').textContent,result.summary.type);
  function all(node){return [node,...node.children.flatMap(all)];}
  const texts=all(byId('messages')).map(n=>n.textContent);
  for(const text of ['Отклонение',result.summary.type,'Уверенность ML: 88%',
                     'По данным ML: Свист в передней части двигателя']) assert.ok(!texts.includes(text));
  await vm.runInContext('selectPair()',context);
  assert.equal(byId('diagnosis-panel').hidden,true);
});

async function uiContext(actionResult, selectionFails=false, manualPages=true, fleet=testFleet, endpoints={}) {
  function element(tagName='div') {
    return {tagName,value:'',hidden:false,disabled:false,children:[],textContent:'',
      append(child){this.children.push(child);},replaceChildren(){this.children=[];},addEventListener(){},scrollIntoView(){}};
  }
  const elements=new Map();const byId=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  byId('session').value='S-TEST';
  const calls=[];const bodies={
    '/api/session':{csrf:'test',revision:0,provider:'openai',corpus_ready:true,manual_pages:manualPages},
    '/api/catalog':fleet,
    '/api/select':selectionFails?{detail:'Контекст не выбран'}:{revision:1},'/api/action':actionResult, ...endpoints};
  const context=vm.createContext({document:{getElementById:byId,createElement:element},fetch:async (path, options)=>{
    calls.push(path);assert.ok(path in bodies);
    const body=typeof bodies[path]==='function'?bodies[path](JSON.parse(options.body)):bodies[path];
    return {ok:!(selectionFails&&path==='/api/select'),json:async()=>body};
  }});
  vm.runInContext(fs.readFileSync('mechanic/static/vehicle-query.js','utf8'),context);
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
  assert.equal(byId('diagnosis-status').textContent,'Отклонение');
  assert.equal(byId('diagnosis-confidence').textContent,'Уверенность ML: 69.99%');
  assert.ok(nodes.some(n=>n.className==='verdict-status'&&n.textContent==='Синтетическая проверка'));
  assert.ok(!nodes.some(n=>n.textContent==='Отклонение'));
  assert.deepEqual(nodes.filter(n=>n.tagName==='h4').map(n=>n.textContent),['Наблюдения','Гипотезы','Проверки','Возможный ремонт','Источники','Ограничения']);
  assert.ok(nodes.some(n=>n.textContent==='Точная тестовая выдержка.'));
  assert.ok(nodes.some(n=>n.href==='/manual/test/1'));
});

test('normal diagnosis shows result, action and unsent status before details', async () => {
  const result = {summary:{status:'Норма',type:'В этой проверке отклонений нет',next_action:'Продолжить штатную эксплуатацию'},
    ticket_status:'not_sent',can_draft:false};
  const {context,byId,calls}=await uiContext(result);
  await vm.runInContext("action('diagnose')",context);
  const card=byId('messages').children.find(node=>node.className==='message verdict');
  assert.equal(byId('diagnosis-status').textContent,'Норма');
  assert.deepEqual(card.children.map(node=>node.textContent),
    ['Продолжить штатную эксплуатацию','Заявка не отправлена']);
  assert.equal(byId('draft').hidden,true);
  assert.ok(!calls.includes('/api/send'));
});

test('verified yes or no appears before the earlier ML result', async () => {
  const claim={section:'checks',text:'Нет. Достаточно отметки осмотра.',manual:'Учебное руководство',doc:'test',pdf_page:19};
  const result={claims:[{section:'hypotheses',text:'Возможное отклонение'},claim],ticket_status:'not_sent',
    summary:{status:'Отклонение',next_action:'Откройте подробности'}};
  const {context,byId}=await uiContext(result);
  await vm.runInContext("action('chat','Нужно ли ещё измерение?')",context);
  const card=byId('messages').children.find(node=>node.className.includes('verdict'));
  assert.equal(card.children[0].textContent,claim.text);
  assert.equal(card.children[1].href,'/manual/test/19');
});

test('an unsupported question gets a visible reply with a next step and no draft', async () => {
  const guidance = 'Общий ориентир. Уточните узел и условия появления симптома.';
  const result = {message:guidance, claims:[], ticket_status:'not_sent', can_draft:false,
    failure:{status:'Ответ по руководству не подтверждён',next_action:guidance}};
  const {context,byId,calls}=await uiContext(result);
  await vm.runInContext("action('chat','Как подтвердить неисправность?')",context);
  const card=byId('messages').children.find(node=>node.className.includes('verdict'));
  assert.deepEqual(card.children.map(node=>node.textContent),
    ['Ответ по руководству не подтверждён',guidance,'Заявка не отправлена']);
  assert.equal(byId('draft').hidden,true);
  assert.ok(!calls.includes('/api/send'));
});

test('eligible diagnosis shows the recommended priority before opening the draft', async () => {
  const result = {summary:{status:'Отклонение',type:'Отклонение',next_action:'Уточните симптом'},
    recommended_urgency:'medium',can_draft:true,ticket_status:'not_sent'};
  const {context,byId}=await uiContext(result);
  await vm.runInContext("action('diagnose')",context);
  const card=byId('messages').children.find(node=>node.className.includes('verdict'));
  assert.ok(card.children.some(node=>node.textContent.includes('Предлагаемый приоритет — средний')));
  assert.equal(byId('draft').hidden,false);
});

test('priority choice keeps the warning and sends only the updated confirmation', async () => {
  const draft={vehicle_id:'TEST',session_id:'S-TEST',anomaly_type:'F08',urgency:'medium',
    conclusion:'Учебный вывод',operations:['Учебная операция'],spare_parts:[]};
  const result={draft,preview_id:'first',warning:true,recommended_urgency:'medium',ticket_status:'awaiting_confirmation'};
  const endpoints={
    '/api/priority':body=>{
      assert.deepEqual(body,{revision:1,preview_id:'first',urgency:'high'});
      return {...result,draft:{...draft,urgency:'high'},preview_id:'changed',priority_selected:true};
    },
    '/api/send':body=>{
      assert.deepEqual(body,{revision:1,preview_id:'changed'});
      return {ticket_id:'TKT-TEST'};
    }
  };
  const {context,byId,calls}=await uiContext(result,false,true,testFleet,endpoints);
  await vm.runInContext("action('draft')",context);
  assert.match(byId('priority-description').textContent,/на ближайшем ТО/);
  byId('preview-priority').value='high';
  await vm.runInContext('changePriority()',context);
  assert.match(byId('priority-description').textContent,/до выпуска в рейс.*Выбрано механиком/);
  assert.equal(byId('preview-warning').hidden,false);
  assert.ok(!calls.includes('/api/send'));
  await vm.runInContext('sendTicket()',context);
  assert.ok(byId('messages').children.some(node=>node.textContent.includes('TKT-TEST')));
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


test('ambiguous typed vehicle command leaves selection unchanged until clarified', async () => {
  const fleet = ['P456AT178', 'X792BH98', 'T792HO98'].map(vehicle_id => ({
    ...testFleet[0], vehicle_id
  }));
  const {context, byId, calls} = await uiContext({}, false, true, fleet);
  await vm.runInContext("handleQuestion('Открой машину 792')", context);
  assert.equal(byId('plate').textContent, 'P456AT178');
  assert.equal(calls.filter(path => path === '/api/select').length, 1);
  const choices = byId('messages').children.at(-1).children[0].children;
  assert.deepEqual(choices.map(button => button.textContent), ['X792BH98 · TEST', 'T792HO98 · TEST']);
  await vm.runInContext("handleQuestion('НО')", context);
  assert.equal(byId('plate').textContent, 'T792HO98');
  assert.equal(calls.filter(path => path === '/api/select').length, 2);
  assert.ok(!calls.includes('/api/action'));
  await vm.runInContext("handleQuestion('Открой машину 999')", context);
  assert.equal(byId('plate').textContent, 'T792HO98');
  assert.equal(calls.filter(path => path === '/api/select').length, 2);
  assert.ok(!calls.includes('/api/action'));
  await vm.runInContext("handleQuestion('Открой машину Х792ВН98')", context);
  assert.equal(byId('plate').textContent, 'X792BH98');
});

test('spoken plate words preserve leading zeroes and an explicit region', async () => {
  const fleet = ['A007AM77', 'A007AM98', 'T792HO98'].map(vehicle_id => ({
    ...testFleet[0], vehicle_id
  }));
  const {context, byId, calls} = await uiContext({}, false, true, fleet);
  await vm.runInContext("handleQuestion('Открой машину ноль ноль семь')", context);
  assert.equal(byId('plate').textContent, 'A007AM77');
  assert.equal(calls.filter(path => path === '/api/select').length, 1);
  await vm.runInContext("handleQuestion('регион девяносто восемь')", context);
  assert.equal(byId('plate').textContent, 'A007AM98');
  assert.equal(calls.filter(path => path === '/api/select').length, 2);
  await vm.runInContext("handleQuestion('какое давление при 792 оборотах?')", context);
  assert.equal(byId('plate').textContent, 'A007AM98');
  assert.ok(calls.includes('/api/action'));
});

test('an original technical question survives an ambiguous vehicle clarification', async () => {
  const fleet = ['P456AT178', 'X792BH98', 'T792HO98'].map(vehicle_id => ({
    ...testFleet[0], vehicle_id
  }));
  const {context, byId, calls} = await uiContext({message:'Ответ',claims:[]}, false, true, fleet);
  const question = 'Какой источник по машине семьсот девяносто два?';
  await vm.runInContext(`handleQuestion(${JSON.stringify(question)})`, context);
  assert.ok(!calls.includes('/api/action'));
  await vm.runInContext("handleQuestion('эн о')", context);
  assert.equal(byId('plate').textContent, 'T792HO98');
  assert.equal(calls.filter(path => path === '/api/action').length, 1);
  assert.ok(byId('messages').children.some(item => item.textContent === question));
});
