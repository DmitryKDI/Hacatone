'use strict';
const $ = id => document.getElementById(id);
let vehicles = [], csrf = '', revision = 0, stamp = 0, previewId = null, busy = false;
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
function controls(flag) { busy=flag; $('busy').hidden=!flag; for(const id of ['diagnose','ask','draft','send']) $(id).disabled=flag; }
async function selectPair() {
  const own=++stamp; clearPreview(); $('messages').replaceChildren(); controls(true);
  try { const data=await api('/api/select',{vehicle_id:$('vehicle').value,session_id:$('session').value}); if(own===stamp) revision=data.revision; }
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
  if(busy)return;const own=stamp;clearPreview();controls(true);
  message(question || (intent==='diagnose'?'Получить результат диагностики':'Подготовить заявку на дефектовку'),'user');
  try {
    const data=await api('/api/action',{revision,intent,question});if(own!==stamp)return;
    message(data.message);
    renderWarning(data);
    for(const c of data.claims){const node=message(c.text);const quote=document.createElement('blockquote');quote.textContent=c.quote;node.append(quote);const link=document.createElement('a');link.className='source';link.textContent=`${c.manual} · PDF-страница ${c.pdf_page}`;link.href=`/manual/${encodeURIComponent(c.doc)}/${c.pdf_page}#page=${c.pdf_page}`;link.target='_blank';link.rel='noopener';node.append(link);}
    $('draft').hidden=!data.can_draft;renderDraft(data);
  }catch(e){if(own===stamp){if(e.data?.message)message(e.data.message);renderWarning(e.data);message(e.message,'error');}}
  finally{if(own===stamp)controls(false);}
}
$('vehicle').addEventListener('change',renderVehicle);$('session').addEventListener('change',selectPair);
$('diagnose').addEventListener('click',()=>action('diagnose'));
$('draft').addEventListener('click',()=>action('draft','Подготовь заявку на дефектовку по выбранной проверке.'));
$('chat').addEventListener('submit',event=>{event.preventDefault();const q=$('question').value.trim();if(q&&!busy){$('question').value='';action('chat',q);}});
$('send').addEventListener('click',async()=>{if(busy||!previewId)return;const own=stamp,id=previewId;controls(true);clearPreview();try{const data=await api('/api/send',{revision,preview_id:id});if(own===stamp)message(`Заявка ${data.ticket_id} создана в ТОиР.`);}catch(e){if(own===stamp)message(e.message,'error');}finally{if(own===stamp)controls(false);}});
(async()=>{try{const state=await api('/api/session');csrf=state.csrf;revision=state.revision;$('provider').textContent=state.provider==='yandex'?'Yandex AI Studio':'OpenAI API';const problems=[];if(state.llm_problem)problems.push(state.llm_problem);if(!state.corpus_ready)problems.push('Локальный корпус ещё не подготовлен.');$('setup').textContent=problems.join(' ');vehicles=await api('/api/catalog');$('vehicle').replaceChildren();for(const v of vehicles){const option=document.createElement('option');option.value=v.vehicle_id;option.textContent=`${v.vehicle_id} · ${v.make}${v.model?' '+v.model:''}`;$('vehicle').append(option);}$('vehicle').disabled=$('session').disabled=false;await renderVehicle();}catch(e){message(e.message,'error');}})();
