'use strict';
const $ = id => document.getElementById(id);
let vehicles = [], csrf = '', revision = 0, stamp = 0, previewId = null, busy = false, selectionReady = false;
let manualPages = true, selectedVehicle = null, pendingVehicle = null;
let voiceWidget = null, voiceDialogVersion = 0;
const ticketLabels = {
  not_sent: 'Заявка не отправлена',
  awaiting_confirmation: 'Ожидает подтверждения заявки',
  unconfirmed: 'Отправка не подтверждена. Проверьте заявку в ТОиР.'
};
const recordingWarning = 'Запись с помехами или уверенность ML ниже 0,70. Вывод предварительный.';
const priorities = {
  high: ['Высокий', 'Дефектовка до выпуска в рейс'],
  medium: ['Средний', 'Дефектовка на ближайшем ТО, работа под наблюдением'],
  low: ['Низкий', 'Дефектовка при следующем осмотре']
};
function showLatestMessage() {
  const thread = $('messages');
  thread.scrollTop = thread.scrollHeight;
}

function message(text, kind = '') {
  const node = document.createElement('article');
  node.className = `message ${kind}`;
  node.textContent = text;
  $('messages').append(node);
  showLatestMessage();
  return node;
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { const error=new Error(data.error || data.detail || 'Не удалось выполнить запрос.'); error.data=data; throw error; }
  return data;
}
function clearPreview() { previewId=null; $('preview').hidden=true; $('draft').hidden=true; }
function controls(flag) {
  busy = flag;
  $('busy').hidden = !flag;
  for (const id of ['diagnose', 'ask', 'draft', 'send', 'marks-match', 'marks-mismatch']) {
    $(id).disabled = flag || !selectionReady;
  }
  $('send').disabled = $('preview-priority').disabled = flag || !selectionReady || !previewId;
}
async function selectPair() {
  const own = ++stamp;
  selectionReady = false;
  pendingVehicle = null;
  renderInspection(null);
  clearPreview();
  $('diagnosis-panel').hidden = true;
  $('messages').replaceChildren();
  $('question').value = '';
  controls(true);
  try {
    const data = await api('/api/select', {vehicle_id: selectedVehicle.vehicle_id, session_id: $('session').value});
    if (own !== stamp) return;
    revision = data.revision;
    selectionReady = true;
    const hint = document.createElement('p');
    hint.className = 'conversation-start';
    hint.textContent = 'Задайте вопрос или посмотрите результат проверки';
    $('messages').append(hint);
  } catch (error) {
    if (own === stamp) message(error.message, 'error');
  } finally {
    if (own === stamp) controls(false);
  }
}

function renderInspection(inspection) {
  $('inspection').hidden=!inspection;
  const labels={match:'Метки совпали. Отклонение по топливоподаче остаётся.',
    mismatch:'Метки не совпадают. Можно подготовить заявку на регулировку.'};
  $('inspection-status').textContent=labels[inspection?.result] || 'Отметьте результат осмотра. Измеренный угол вводить не нужно.';
  const source = $('inspection-source');
  source.replaceChildren();
  source.hidden = !inspection?.reference;
  if (inspection?.reference) sourceLink(source, inspection.reference);
  $('draft').textContent=inspection ? 'Подготовить заявку на регулировку' : 'Подготовить заявку на дефектовку';
}
async function saveInspection(result) {
  if(busy||!selectionReady)return;
  const own=stamp;clearPreview();controls(true);
  try {
    const data=await api('/api/inspection',{revision,result});
    if(own!==stamp)return;
    renderInspection(data.inspection);$('draft').hidden=!data.can_draft;message(data.message);
  }catch(error){if(own===stamp)message(error.message,'error');}
  finally{if(own===stamp)controls(false);}
}
function vehicleName(vehicle) {
  const model = vehicle.model?.trim() || '';
  const make = vehicle.make?.trim() || '';
  if (!model) return make || 'Машина';
  return !make || model.toLowerCase().startsWith(make.toLowerCase()) ? model : `${make} ${model}`;
}

function renderVehicle(vehicle, sessionId) {
  selectedVehicle = vehicle;
  $('vehicle-title').textContent = vehicleName(vehicle);
  $('plate').textContent = vehicle.vehicle_id;
  $('plate').hidden = false;
  $('engine').textContent = `Двигатель ${vehicle.engine_model}`;
  $('session').replaceChildren();
  for (const check of [...vehicle.sessions].reverse()) {
    const option = document.createElement('option');
    option.value = check.session_id;
    const date = new Date(check.checked_at).toLocaleString('ru-RU', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
    option.textContent = `${date} · ${check.session_id}`;
    $('session').append(option);
  }
  if (vehicle.sessions.some(check => check.session_id === sessionId)) $('session').value = sessionId;
  return selectPair();
}

function candidateButton(vehicle, handler) {
  const candidate = document.createElement('button');
  candidate.type = 'button';
  candidate.className = 'candidate';
  candidate.textContent = `${vehicle.vehicle_id} · ${vehicleName(vehicle)}`;
  candidate.addEventListener('click', () => handler(vehicle));
  return candidate;
}

function renderVehicleOptions() {
  const query = $('vehicle-query').value.trim();
  const options = $('vehicle-options');
  const plates = matchingPlates(query, vehicles);
  const cars = !query ? vehicles : plates.length ? plates : vehicles.filter(vehicle =>
    compactPlate(vehicle.vehicle_id).includes(compactPlate(query)) ||
    vehicleName(vehicle).toLowerCase().includes(query.toLowerCase()));
  options.replaceChildren();
  for (const vehicle of cars.slice(0, 10)) {
    options.append(candidateButton(vehicle, selected => {
      $('vehicle-picker').close();
      renderVehicle(selected);
    }));
  }
  if (!cars.length) paragraph(options, 'Машина не найдена.');
  if (cars.length > 10) paragraph(options, 'Уточните госномер или модель.');
}

async function openTarget(vehicle, question) {
  pendingVehicle = null;
  if (selectedVehicle !== vehicle || !selectionReady) {
    const selection = renderVehicle(vehicle);
    const own = stamp;
    await selection;
    if (own !== stamp || !selectionReady) return;
  }
  if (question) return action('chat', question);
}

async function handleQuestion(question) {
  if (/^отмена[.!?]?$/iu.test(question) && pendingVehicle) {
    pendingVehicle = null;
    message('Выбор машины отменён.');
    return;
  }
  const request = mentionedVehicle(question);
  if (!request) {
    if (pendingVehicle) {
      const matches = clarifyPlate(question, pendingVehicle.candidates);
      if (matches.length === 1) return openTarget(matches[0], pendingVehicle.question);
      message('Уточните буквы или регион из вариантов. Чтобы оставить текущую машину, напишите «Отмена».');
      return;
    }
    return action('chat', question);
  }
  const candidates = matchingPlates(request.query, vehicles);
  pendingVehicle = null;
  if (candidates.length === 1) return openTarget(candidates[0], request.question);
  dismissStart();
  message(question, 'user');
  if (!candidates.length) {
    message('Машина не найдена. Уточните буквы или введите полный госномер.');
    return;
  }
  const pending = {candidates, question: request.question};
  pendingVehicle = pending;
  const prompt = message('Нашёл несколько машин. Уточните буквы или регион.');
  const options = document.createElement('div');
  options.className = 'candidate-list';
  const own = stamp;
  for (const vehicle of candidates) {
    options.append(candidateButton(vehicle, selected => {
      if (own === stamp && selectionReady && pendingVehicle === pending) openTarget(selected, pending.question);
    }));
  }
  prompt.append(options);
  showLatestMessage();
}

function dismissStart() {
  $('messages').querySelector?.('.conversation-start')?.remove();
}
function paragraph(parent,text){const p=document.createElement('p');p.textContent=text;parent.append(p);return p;}
function renderWarning(data) { if(data?.warning&&!data.summary)message(recordingWarning,'warning'); }
function sourceLink(parent,source) {
  const label=`${source.manual} · PDF-страница ${source.pdf_page}`;
  if(!manualPages){paragraph(parent,label);return;}
  const link=document.createElement('a');link.className='source';link.textContent=label;
  link.href=`/manual/${encodeURIComponent(source.doc)}/${source.pdf_page}`;link.target='_blank';link.rel='noopener';parent.append(link);
}
function renderReply(data, intent) {
  if (data.summary) {
    renderDiagnosis(data);
    // ML facts live outside the conversation; replies contain only the answer or limitation.
    data = {...data, summary:null, observations:'', message:
      data.failure ? data.failure.next_action :
      data.incompatible ? data.message :
      data.conflicts?.length || data.claims?.length ? '' :
      intent === 'diagnose' ? data.summary.next_action : ''};
  }
  const summary = data.failure;
  const directClaims = (data.claims || []).filter(claim => claim.section !== 'hypotheses' && claim.section !== 'risks');
  const answer = directClaims.find(claim => /^(да|нет)[,.!\s]/i.test(claim.text)) || directClaims[0];
  const direct = answer?.text || (intent === 'chat' && !data.failure ? data.message : '');
  const explanation = data.conflicts || data.incompatible || !summary ? data.message : '';
  if (!summary && !direct && !explanation) return null;
  const node = message('', 'verdict');
  if (summary?.status && summary.status !== 'Норма') node.className += ' attention';
  const status = paragraph(node, direct || summary?.status || explanation);
  status.className = 'verdict-status';
  if (data.failure && data.message && data.message !== summary.next_action) paragraph(node, data.message);
  if (data.failure && data.error) paragraph(node, data.error).className = 'reply-error';
  if (!direct && explanation && summary) paragraph(node, explanation);
  if (answer) sourceLink(node, answer);
  if (summary?.next_action && !direct) {
    const title = document.createElement('h3');
    title.textContent = summary.next_action;
    node.append(title);
  }
  const recommended = priorities[data.recommended_urgency];
  if (data.can_draft && !data.draft) {
    paragraph(node, recommended
      ? `Предлагаемый приоритет — ${recommended[0].toLowerCase()}. ${recommended[1]}.`
      : 'Приоритет можно выбрать в предпросмотре заявки.');
  }
  if (data.ticket_status) paragraph(node, ticketLabels[data.ticket_status]).className = 'ticket-status';
  return node;
}

function renderDiagnosis(data) {
  const summary = data.summary;
  if (!summary) return;
  $('diagnosis-status').textContent = summary.status;
  $('diagnosis-type').textContent = summary.type || '';
  $('diagnosis-confidence').textContent = summary.confidence ? `Уверенность ML: ${summary.confidence}` : '';
  $('diagnosis-observations').textContent = data.observations ? `По данным ML: ${data.observations}` : '';
  $('diagnosis-warning').hidden = !data.warning;
  $('diagnosis-panel').className = summary.status === 'Норма' ? 'diagnosis-panel normal' : 'diagnosis-panel';
  $('diagnosis-panel').hidden = false;
}

function renderDetails(data) {
  if (data.summary) data = {...data, summary:null, observations:'', message:''};
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
function renderConflicts(conflicts=[], explanation='') {
  if (conflicts.length && explanation) message(explanation, 'warning');
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
  $('preview-priority').value = d.urgency;
  $('preview-warning').hidden = !result.warning;
  const [label, schedule] = priorities[d.urgency];
  const origin = result.priority_selected ? `Выбрано механиком. Рекомендация агента — ${priorities[result.recommended_urgency][0].toLowerCase()}.` : 'Рекомендация агента.';
  $('priority-description').textContent = `Приоритет: ${label.toLowerCase()}. ${schedule}. ${origin} ${ticketLabels.awaiting_confirmation}.`;
  const box=$('preview-body');
  paragraph(box,`ТС: ${d.vehicle_id} · Проверка: ${d.session_id} · Тип: ${d.anomaly_type}`);
  paragraph(box,d.conclusion);paragraph(box,'Работы и подтверждающие выдержки:');
  for(const operation of d.operations) {const quote=document.createElement('blockquote');quote.textContent=operation;box.append(quote);}
  paragraph(box,`Запчасти: ${d.spare_parts.length?d.spare_parts.join('; '):'не определены по документации, список пуст'}`);
  for(const ref of result.draft_sources || []) sourceLink(paragraph(box,''),ref);
  paragraph(box,result.priority_basis?.text || 'Агент рекомендовал средний приоритет по подтверждённым работам.');
}

async function changePriority() {
  if (busy || !previewId) return;
  const own = stamp;
  controls(true);
  try {
    const data = await api('/api/priority', {revision, preview_id: previewId, urgency: $('preview-priority').value});
    if (own === stamp) renderDraft(data);
  } catch (error) {
    if (own === stamp) {
      clearPreview();
      message(error.message, 'error');
    }
  } finally {
    if (own === stamp) {
      controls(false);
      if (previewId) $('preview-priority').focus?.();
    }
  }
}

async function sendTicket() {
  if (busy || !previewId) return;
  const own = stamp, id = previewId;
  controls(true);
  clearPreview();
  try {
    const data = await api('/api/send', {revision, preview_id: id});
    if (own === stamp) message(`Заявка ${data.ticket_id} создана в ТОиР.`);
  } catch (error) {
    if (own === stamp) {
      const status = error.data ? error.data.ticket_status || 'not_sent' : 'unconfirmed';
      message(`${ticketLabels[status]} ${error.data?.error || error.data?.detail || ''}`.trim(), 'error');
    }
  } finally {
    if (own === stamp) controls(false);
  }
}

async function action(intent,question='') {
  if(busy||!selectionReady)return;const own=stamp;clearPreview();controls(true);
  dismissStart();
  message(question || (intent==='diagnose'?'Показать результат диагностики':'Подготовить заявку на дефектовку'),'user');
  try {
    const data=await api('/api/action',{revision,intent,question});if(own!==stamp)return;
    const verdict=renderReply(data, intent);
    renderWarning(data);
    if(data.demo_condition)message(data.demo_condition,'warning');
    renderConflicts(data.conflicts, data.message);
    renderDetails(data);
    renderInspection(data.inspection);$('draft').hidden=!data.can_draft;renderDraft(data);
    if(verdict)verdict.scrollIntoView?.({block:'nearest'});
  }catch(error){
    if(own!==stamp)return;
    const data=error.data;
    const verdict=data && renderReply(data,intent);
    renderWarning(data);
    if(data){renderDetails(data);renderInspection(data.inspection);$('draft').hidden=!data.can_draft;}
    if (!data?.failure) message(error.message,'error');
    verdict?.scrollIntoView?.({block:'nearest'});
  }
  finally{if(own===stamp)controls(false);}
}
async function openVoiceInput() {
  const dialog = $('voice-dialog');
  const notice = $('voice-message');
  if (dialog.open) return;
  const version = ++voiceDialogVersion;
  notice.textContent = 'Проверяю локальный голосовой модуль…';
  dialog.showModal();
  if (!['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)) {
    notice.textContent = 'Откройте Hacatone на этом ноутбуке через localhost для локальной диктовки.';
    return;
  }
  try {
    const status = await api('/voice/status');
    if (status.ready !== true) throw new Error('Голосовой модуль недоступен.');
    if (!dialog.open || version !== voiceDialogVersion) return;
    const {mountVoiceKit} = await import('/static/voice/voice-kit.js');
    if (!dialog.open || version !== voiceDialogVersion) return;
    voiceWidget = mountVoiceKit($('voice-panel'), {streamPath: '/voice/stream'});
    notice.textContent = '';
  } catch {
    if (dialog.open && version === voiceDialogVersion) notice.textContent = 'Запустите Hacatone с локальным голосовым модулем для диктовки.';
  }
}

$('mic').addEventListener('click', openVoiceInput);
$('voice-dialog').addEventListener('close', () => {
  voiceDialogVersion++;
  voiceWidget?.destroy();
  voiceWidget = null;
});
$('voice-panel').addEventListener('voice-input:transcript', ({detail}) => {
  const text = detail.text.trim();
  if (!text) return;
  const input = $('question');
  const separator = input.value && !/\s$/.test(input.value) ? ' ' : '';
  if (input.value.length + separator.length + text.length > input.maxLength) {
    $('voice-message').textContent = 'Сократите текст, чтобы он поместился в вопрос.';
    return;
  }
  input.value += separator + text;
  $('voice-dialog').close();
  input.focus();
});
$('session').addEventListener('change', selectPair);
$('choose-vehicle').addEventListener('click', () => {
  $('vehicle-query').value = '';
  renderVehicleOptions();
  $('vehicle-picker').showModal();
  $('vehicle-query').focus();
});
$('vehicle-query').addEventListener('input', renderVehicleOptions);
$('diagnose').addEventListener('click', () => action('diagnose'));
$('draft').addEventListener('click',()=>action('draft',$('draft').textContent));
$('marks-match').addEventListener('click',()=>saveInspection('match'));
$('marks-mismatch').addEventListener('click',()=>saveInspection('mismatch'));
$('chat').addEventListener('submit', event => {
  event.preventDefault();
  const question = $('question').value.trim();
  if (!question || busy || !selectionReady) return;
  $('question').value = '';
  handleQuestion(question);
});
$('preview-priority').addEventListener('change', changePriority);
$('send').addEventListener('click', sendTicket);
(async () => {
  try {
    const state = await api('/api/session');
    csrf = state.csrf;
    revision = state.revision;
    manualPages = state.manual_pages !== false;
    $('provider').textContent = state.model || ({openai: 'OpenAI API', yandex: 'Yandex AI Studio'}[state.provider] || '');
    const problems = [];
    if (state.llm_problem) problems.push(state.llm_problem);
    if (!state.corpus_ready) problems.push('Локальный корпус ещё не подготовлен.');
    $('setup').textContent = problems.join(' ');
    $('service').className = problems.length ? 'service attention' : 'service';
    $('service-text').textContent = problems.length ? 'Требуется настройка' : 'Настройки стенда';
    vehicles = await api('/api/catalog');
    if (!vehicles.length) {
      $('engine').textContent = 'В каталоге пока нет машин.';
      return;
    }
    $('session').disabled = $('choose-vehicle').disabled = false;
    const vehicle = vehicles.find(item => item.vehicle_id === state.pair?.[0]) || vehicles[0];
    await renderVehicle(vehicle, state.pair?.[1]);
  } catch (error) {
    message(error.message, 'error');
  }
})();
