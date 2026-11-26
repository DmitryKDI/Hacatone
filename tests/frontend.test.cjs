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
