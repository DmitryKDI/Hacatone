'use strict';
const $ = id => document.getElementById(id);
let vehicles = [], csrf = '', revision = 0, stamp = 0, previewId = null, busy = false, selectionReady = false;
let manualPages = true;
const message = (text, kind='') => {
  const node = document.createElement('article'); node.className = `message ${kind}`;
  node.textContent = text; $('messages').append(node); $('messages').scrollTop = $('messages').scrollHeight; return node;
};
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { const error=new Error(data.error || data.detail || 'Не удалось выполнить запрос.'); error.data=data; throw error; }
  return data;
}
function clearPreview() { previewId=null; $('preview').hidden=true; $('draft').hidden=true; }
function controls(flag) { busy=flag; $('busy').hidden=!flag; for(const id of ['diagnose','ask','draft','send']) $(id).disabled=flag||!selectionReady; }
async function selectPair() {
  const own=++stamp; selectionReady=false; clearPreview(); $('messages').replaceChildren(); $('question').value=''; controls(true);
  try { const data=await api('/api/select',{vehicle_id:$('vehicle').value,session_id:$('session').value}); if(own===stamp) {revision=data.revision;selectionReady=true;} }
  catch(e) { if(own===stamp) message(String(e.message),'error'); }
  finally { if(own===stamp) controls(false); }
}
function renderVehicle() {
  const vehicle=vehicles.find(v=>v.vehicle_id===$('vehicle').value); $('session').replaceChildren();
  $('engine').textContent=`Двигатель ${vehicle.engine_model}`;
  for(const s of [...vehicle.sessions].reverse()) {const option=document.createElement('option');option.value=s.session_id;option.textContent=`${new Date(s.checked_at).toLocaleString('ru-RU')} · ${s.session_id}`;$('session').append(option);}
  return selectPair();
}
function paragraph(parent,text){const p=document.createElement('p');p.textContent=text;parent.append(p);return p;}
function renderWarning(data) { if(data?.warning)message('Запись с помехами или уверенность ML ниже 0,70. Вывод предварительный.','warning'); }
function sourceLink(parent,source) {
  const label=`${source.manual} · PDF-страница ${source.pdf_page}`;
  if(!manualPages){paragraph(parent,label);return;}
  const link=document.createElement('a');link.className='source';link.textContent=label;
  link.href=`/manual/${encodeURIComponent(source.doc)}/${source.pdf_page}`;link.target='_blank';link.rel='noopener';parent.append(link);
}
function renderSummary(summary) {
  if(!summary)return null;
  const node=message('','verdict');const title=document.createElement('h3');title.textContent='Краткий вердикт';node.append(title);
  paragraph(node,`${summary.vehicle_id} · ${summary.session_id}`);
  const list=document.createElement('dl');
  for(const [label,key] of [['Статус','status'],['Тип','type'],['Уверенность ML','confidence'],['Риск','risk'],['Следующее действие','next_action']]) {
    const term=document.createElement('dt');term.textContent=label;const value=document.createElement('dd');value.textContent=summary[key];list.append(term);list.append(value);
  }
  node.append(list);return node;
}
function renderDetails(data) {
  const claims=data.claims||[];
  if(!data.summary&&!claims.length)return;
  const node=message('','details');const details=document.createElement('details');
  const toggle=document.createElement('summary');toggle.textContent='Подробности и источники';details.append(toggle);
  for(const [section,label] of [['observations','Наблюдения'],['hypotheses','Гипотезы'],['checks','Проверки'],['repair','Возможный ремонт'],['sources','Источники'],['limitations','Ограничения']]) {
    const part=document.createElement('section');const heading=document.createElement('h4');heading.textContent=label;part.append(heading);
    if(section==='observations'&&data.observations)paragraph(part,`По данным ML: ${data.observations}`);
    if(section==='sources') {
      const seen=new Set();for(const c of claims){const key=`${c.doc}:${c.pdf_page}`;if(!seen.has(key)){sourceLink(part,c);seen.add(key);}}
      if(!seen.size)paragraph(part,'Подтверждённые источники для технической рекомендации не представлены.');
    } else {
      const matching=claims.filter(c=>(c.section||'hypotheses')===section||(section==='hypotheses'&&c.section==='risks'));
      for(const c of matching){paragraph(part,(c.section==='risks'?'Риск: ':'')+c.text);const quote=document.createElement('blockquote');quote.textContent=c.quote;part.append(quote);sourceLink(part,c);}
      if(section==='observations'&&!data.observations&&!matching.length)paragraph(part,'Новые данные ML для этого ответа не получены.');
      if(!matching.length&&section!=='observations'&&section!=='limitations')paragraph(part,'Подтверждённые рекомендации не подготовлены.');
      if(section==='limitations') {if(data.message)paragraph(part,data.message);paragraph(part,'ML-вывод предварительный; отсутствие отклонения не гарантирует отсутствия всех неисправностей.');}
    }
    details.append(part);
  }
  node.append(details);
}
function renderConflicts(conflicts=[]) {
  for(const conflict of conflicts) {
    message(conflict.kind==='uncertain_applicability'?'Противоречащие инструкции: применимость к модификации не установлена.':'Противоречащие технические инструкции.','warning');
    for(const [index,source] of conflict.sources.entries()) {
      const node=message(`Формулировка ${index+1}`);const quote=document.createElement('blockquote');quote.textContent=source.quote;node.append(quote);
      sourceLink(node,source);
    }
  }
}
function renderDraft(result) {
  const d=result.draft;if(!d)return;previewId=result.preview_id;$('preview').hidden=false;$('preview-body').replaceChildren();
  const box=$('preview-body');
  paragraph(box,`ТС: ${d.vehicle_id} · Проверка: ${d.session_id} · Тип: ${d.anomaly_type}`);
  paragraph(box,`Приоритет: ${{low:'низкий',medium:'средний',high:'высокий'}[d.urgency]}`);
  paragraph(box,d.conclusion);paragraph(box,'Работы и подтверждающие выдержки:');
  for(const operation of d.operations) {const quote=document.createElement('blockquote');quote.textContent=operation;box.append(quote);}
  paragraph(box,`Запчасти: ${d.spare_parts.length?d.spare_parts.join('; '):'не определены по документации, список пуст'}`);
  for(const ref of d.references) paragraph(box,`${ref.manual} · PDF-страница ${ref.pdf_page}`);
  paragraph(box,result.priority_basis?.text || 'Средний приоритет — исходный уровень обоснованной дефектовки, без подтверждённых признаков крайних уровней.');
}
async function action(intent,question='') {
  if(busy||!selectionReady)return;const own=stamp;clearPreview();controls(true);
  message(question || (intent==='diagnose'?'Получить результат диагностики':'Подготовить заявку на дефектовку'),'user');
  try {
    const data=await api('/api/action',{revision,intent,question});if(own!==stamp)return;
    const verdict=renderSummary(data.summary);if(!verdict)message(data.message);
    renderWarning(data);
    renderConflicts(data.conflicts);
    renderDetails(data);
    $('draft').hidden=!data.can_draft;renderDraft(data);
    if(intent==='diagnose'&&verdict)verdict.scrollIntoView?.({block:'start'});
  }catch(e){if(own===stamp){const verdict=renderSummary(e.data?.summary);if(!verdict&&e.data?.message)message(e.data.message);renderWarning(e.data);if(e.data)renderDetails(e.data);message(e.message,'error');if(intent==='diagnose'&&verdict)verdict.scrollIntoView?.({block:'start'});}}
  finally{if(own===stamp)controls(false);}
}
$('vehicle').addEventListener('change',renderVehicle);$('session').addEventListener('change',selectPair);
$('diagnose').addEventListener('click',()=>action('diagnose'));
$('draft').addEventListener('click',()=>action('draft','Подготовь заявку на дефектовку по выбранной проверке.'));
$('chat').addEventListener('submit',event=>{event.preventDefault();const q=$('question').value.trim();if(q&&!busy){$('question').value='';action('chat',q);}});
$('send').addEventListener('click',async()=>{if(busy||!previewId)return;const own=stamp,id=previewId;controls(true);clearPreview();try{const data=await api('/api/send',{revision,preview_id:id});if(own===stamp)message(`Заявка ${data.ticket_id} создана в ТОиР.`);}catch(e){if(own===stamp)message(e.message,'error');}finally{if(own===stamp)controls(false);}});
(async () => {
  try {
    const state = await api('/api/session');
    csrf = state.csrf;
    revision = state.revision;
    manualPages = state.manual_pages !== false;
    $('provider').textContent = state.model;
    const problems = [];
    if (state.llm_problem) problems.push(state.llm_problem);
    if (!state.corpus_ready) problems.push('Локальный корпус ещё не подготовлен.');
    $('setup').textContent = problems.join(' ');
    vehicles = await api('/api/catalog');
    $('vehicle').replaceChildren();
    for (const vehicle of vehicles) {
      const option = document.createElement('option');
      option.value = vehicle.vehicle_id;
      option.textContent = `${vehicle.vehicle_id} · ${vehicle.make}${vehicle.model ? ' ' + vehicle.model : ''}`;
      $('vehicle').append(option);
    }
    $('vehicle').disabled = $('session').disabled = false;
    await renderVehicle();
  } catch (error) {
    message(error.message, 'error');
  }
})();
