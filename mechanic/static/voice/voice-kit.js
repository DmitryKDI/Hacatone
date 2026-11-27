const PREFS_KEY = 'hacatone.voice-kit.v1';
const FALLBACK_PREFS = {name: 'Механик', voice: '', rate: 0.9};
const validName = name => typeof name === 'string' && name.length <= 36 &&
  /^[А-ЯЁа-яё]{2,20}(?:[ -][А-ЯЁа-яё]{2,20}){0,2}$/u.test(name);
const states = {
  off: 'Микрофон выключен',
  opening: 'Подключаю микрофон…',
  waiting: 'Жду команду',
  recording: 'Записываю вопрос',
  processing: 'Завершаю распознавание…',
  paused: 'Прослушивание приостановлено',
  error: 'Микрофон недоступен',
};

const markup = `
  <div class="vk-heading">
    <div><h2>Голосовой ввод</h2><p class="vk-status" data-status role="status"></p></div>
    <button type="button" class="vk-quiet" data-toggle>Включить микрофон</button>
  </div>
  <p class="vk-phrase">Команда <strong data-phrase></strong></p>
  <div class="vk-row">
    <button type="button" class="vk-primary" data-talk disabled>Говорить</button>
    <button type="button" class="vk-quiet" data-stop hidden>Стоп</button>
  </div>
  <p class="vk-note" data-notice role="status" hidden></p>
  <label class="vk-field"><span>Распознанный вопрос</span>
    <textarea data-transcript rows="3" maxlength="1500"></textarea>
  </label>
  <button type="button" class="vk-quiet" data-insert disabled>Вставить в вопрос</button>
  <details class="vk-settings">
    <summary>Имя и голос помощника</summary>
    <div class="vk-grid">
      <label class="vk-field"><span>Как назвать помощника?</span>
        <input data-name maxlength="36" autocomplete="off">
      </label>
      <label class="vk-field"><span>Русский голос</span><select data-voice></select></label>
    </div>
    <div class="vk-row">
      <button type="button" class="vk-quiet" data-save-name>Сохранить имя</button>
      <button type="button" class="vk-quiet" data-test-name disabled>Проверить команду</button>
    </div>
    <label class="vk-field"><span>Темп озвучки</span>
      <input data-rate type="range" min="0.7" max="1.2" step="0.05">
    </label>
    <div class="vk-row">
      <button type="button" class="vk-quiet" data-sample>Послушать голос</button>
      <button type="button" class="vk-quiet" data-stop-voice disabled>Остановить озвучку</button>
    </div>
  </details>
`;

function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY)) ?? {};
    return {
      name: validName(saved.name) ? saved.name : FALLBACK_PREFS.name,
      voice: typeof saved.voice === 'string' ? saved.voice : FALLBACK_PREFS.voice,
      rate: Number.isFinite(saved.rate) && saved.rate >= 0.7 && saved.rate <= 1.2 ? saved.rate : FALLBACK_PREFS.rate,
    };
  } catch {
    return {...FALLBACK_PREFS};
  }
}

function remember(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // Voice input still works if browser storage is unavailable.
  }
}

function endpoint(path) {
  const url = new URL(path, location.href);
  if (url.origin !== location.origin || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
    throw new Error('Распознавание доступно только на этом компьютере.');
  }
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}

function beep(context) {
  if (!context || context.state !== 'running') return;
  const oscillator = context.createOscillator();
  const volume = context.createGain();
  const now = context.currentTime;
  oscillator.frequency.value = 540;
  volume.gain.setValueAtTime(0.045, now);
  volume.gain.exponentialRampToValueAtTime(0.001, now + 0.12);
  oscillator.connect(volume).connect(context.destination);
  oscillator.onended = () => {
    oscillator.disconnect();
    volume.disconnect();
  };
  oscillator.start();
  oscillator.stop(now + 0.12);
}

export function mountVoiceKit(root, {streamPath = '/voice/stream'} = {}) {
  root.classList.add('voice-kit');
  root.innerHTML = markup;
  const field = name => root.querySelector(`[data-${name}]`);
  const prefs = loadPrefs();
  const transcript = field('transcript');
  const speech = globalThis.speechSynthesis;
  let current = 'off';
  let connection = null;
  let synthesis = null;
  let nextFrameAt = 0;
  let voices = [];
  let pendingName = null;
  let testTimer = null;
  const listeners = new AbortController();

  field('name').value = prefs.name;
  field('rate').value = prefs.rate;

  function on(element, type, handler) {
    element.addEventListener(type, handler, {signal: listeners.signal});
  }

  function sayStatus(message = '') {
    field('notice').textContent = message;
    field('notice').hidden = !message;
  }

  function refreshPhrase() {
    field('phrase').textContent = `«${prefs.name}, слушай»`;
  }

  function setState(state) {
    current = state;
    root.dataset.voiceState = state;
    field('status').textContent = states[state];
    field('toggle').textContent = connection ? 'Выключить микрофон' : 'Включить микрофон';
    field('talk').disabled = state !== 'waiting';
    field('test-name').disabled = state !== 'waiting';
    field('save-name').disabled = ['recording', 'processing', 'opening'].includes(state);
    field('stop').hidden = state !== 'recording';
    transcript.readOnly = ['recording', 'processing'].includes(state);
    field('insert').disabled = transcript.readOnly || !transcript.value.trim();
  }

  function send(action, args = {}) {
    if (connection?.socket?.readyState !== WebSocket.OPEN) return false;
    connection.socket.send(JSON.stringify({action, ...args}));
    return true;
  }

  function capture(enabled) {
    if (!connection) return;
    for (const track of connection.stream?.getAudioTracks() ?? []) track.enabled = enabled;
    connection.node?.port.postMessage({enabled});
  }

  function clearTest() {
    clearTimeout(testTimer);
    testTimer = null;
  }

  function close(error = '') {
    const resource = connection;
    connection = null;
    pendingName = null;
    clearTest();
    if (['recording', 'processing'].includes(current)) transcript.value = '';
    if (resource) {
      resource.stream?.getTracks().forEach(track => track.stop());
      resource.node?.disconnect();
      resource.input?.disconnect();
      resource.context?.close().catch(() => {});
      if (resource.socket && resource.socket.readyState < WebSocket.CLOSING) resource.socket.close();
    }
    setState(error ? 'error' : 'off');
    sayStatus(error);
  }

  function fromServer(data) {
    if (data.type === 'state' && Object.hasOwn(states, data.state)) {
      if (data.state === 'waiting' && pendingName && data.phrase === `${pendingName}, слушай`) {
        prefs.name = pendingName;
        pendingName = null;
        remember(prefs);
        refreshPhrase();
      }
      if (data.state === 'recording') {
        transcript.value = '';
        clearTest();
        if (data.activated) {
          nextFrameAt = performance.now() + 155;
          beep(connection.context);
        }
      }
      setState(data.state);
      return;
    }
    if (data.type === 'partial' && current === 'recording') {
      transcript.value = data.text;
    } else if (data.type === 'final') {
      transcript.value = data.text;
      sayStatus(data.text ? 'Проверьте текст перед вставкой.' : 'Речь не распознана. Попробуйте ещё раз.');
    } else if (data.type === 'testing') {
      sayStatus(`Произнесите «${prefs.name}, слушай».`);
      clearTest();
      testTimer = setTimeout(() => {
        send('configure', {name: prefs.name});
        sayStatus('Команда не распознана. Попробуйте снова или нажмите «Говорить».');
      }, 8500);
    } else if (data.type === 'tested') {
      clearTest();
      nextFrameAt = performance.now() + 155;
      beep(connection.context);
      sayStatus('Команда распознана.');
    } else if (data.type === 'error') {
      pendingName = null;
      sayStatus(data.message);
      if (current === 'opening') close(data.message);
    }
  }

  async function open() {
    if (connection) return;
    if (synthesis || !navigator.mediaDevices?.getUserMedia || !globalThis.AudioWorkletNode) {
      close('Для голосового ввода откройте локальную страницу в актуальном Chrome или Edge и остановите озвучку.');
      return;
    }
    const resource = {};
    connection = resource;
    setState('opening');
    sayStatus();
    try {
      const url = endpoint(streamPath);
      resource.context = new AudioContext({sampleRate: 16000});
      await resource.context.resume();
      if (connection !== resource) return;
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}, video: false,
      });
      if (connection !== resource) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      resource.stream = stream;
      if (resource.context.sampleRate !== 16000) throw new Error('Браузер не поддерживает звук 16 кГц.');
      await resource.context.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url));
      if (connection !== resource) return;
      for (const track of stream.getAudioTracks()) {
        on(track, 'ended', () => { if (connection === resource) close('Микрофон отключён.'); });
      }
      resource.node = new AudioWorkletNode(resource.context, 'voice-kit-pcm', {
        numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
      });
      resource.input = resource.context.createMediaStreamSource(stream);
      resource.input.connect(resource.node);
      resource.node.connect(resource.context.destination);
      resource.socket = new WebSocket(url);
      on(resource.socket, 'open', () => {
        if (connection === resource) send('configure', {name: prefs.name});
      });
      on(resource.socket, 'message', ({data}) => {
        if (connection === resource) fromServer(JSON.parse(data));
      });
      on(resource.socket, 'close', () => {
        if (connection === resource) close('Локальное распознавание остановлено. Проверьте стенд и включите микрофон снова.');
      });
      resource.node.port.onmessage = ({data}) => {
        if (connection !== resource || !['waiting', 'recording'].includes(current) ||
            performance.now() < nextFrameAt || resource.socket.readyState !== WebSocket.OPEN) return;
        if (resource.socket.bufferedAmount >= 64000) {
          close('Распознавание не успевает. Включите микрофон повторно.');
          return;
        }
        resource.socket.send(data);
      };
    } catch (error) {
      if (connection !== resource) return;
      const reason = error.name === 'NotAllowedError' ? 'Разрешите доступ к микрофону в браузере.'
        : error.name === 'NotFoundError' ? 'Подключите микрофон и повторите попытку.'
          : 'Проверьте микрофон и запустите локальный голосовой модуль.';
      close(reason);
    }
  }

  function listVoices() {
    voices = (speech?.getVoices() ?? []).filter(voice => voice.localService && /^ru(?:[-_]|$)/i.test(voice.lang));
    const select = field('voice');
    select.replaceChildren(...voices.map(voice => new Option(voice.name, voice.voiceURI)));
    if (voices.some(voice => voice.voiceURI === prefs.voice)) select.value = prefs.voice;
    if (!voices.length) select.add(new Option('Локальный русский голос не установлен', ''));
    select.disabled = !voices.length;
    field('sample').disabled = !voices.length;
  }

  function stopSpeaking() {
    if (!synthesis) return;
    const running = synthesis;
    synthesis = null;
    speech.cancel();
    field('stop-voice').disabled = true;
    if (connection && running.resume) {
      send('resume');
      nextFrameAt = performance.now() + 350;
      capture(true);
    }
  }

  function speak(text) {
    if (typeof text !== 'string' || !text.trim()) return false;
    if (!voices.length) {
      sayStatus('Для озвучки установите локальный русский голос Windows.');
      return false;
    }
    if (['recording', 'processing', 'opening'].includes(current)) {
      sayStatus('Завершите диктовку перед озвучкой.');
      return false;
    }
    stopSpeaking();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.voice = voices.find(voice => voice.voiceURI === field('voice').value) ?? voices[0];
    utterance.lang = utterance.voice.lang;
    utterance.rate = prefs.rate;
    const running = {resume: Boolean(connection)};
    synthesis = running;
    capture(false);
    send('pause');
    if (running.resume) setState('paused');
    field('stop-voice').disabled = false;
    const done = () => { if (synthesis === running) stopSpeaking(); };
    utterance.onend = done;
    utterance.onerror = () => {
      if (synthesis !== running) return;
      done();
      sayStatus('Озвучка не запустилась. Проверьте русский голос Windows.');
    };
    speech.speak(utterance);
    return true;
  }

  on(field('toggle'), 'click', () => connection ? close() : open());
  on(field('talk'), 'click', () => { sayStatus(); send('start'); });
  on(field('stop'), 'click', () => { if (send('finish')) setState('processing'); });
  on(transcript, 'input', () => setState(current));
  on(field('insert'), 'click', () => {
    const text = transcript.value.trim();
    if (text && !transcript.readOnly) root.dispatchEvent(new CustomEvent('voice-input:transcript', {
      detail: {text}, bubbles: true,
    }));
  });
  on(field('save-name'), 'click', () => {
    const name = field('name').value.trim();
    if (!validName(name)) {
      sayStatus('Введите короткое имя кириллицей, например «Саша».');
      return;
    }
    clearTest();
    if (connection && current === 'waiting') {
      pendingName = name;
      send('configure', {name});
    } else {
      prefs.name = name;
      remember(prefs);
      refreshPhrase();
    }
    sayStatus('Проверьте произношение новой команды с включённым микрофоном.');
  });
  on(field('test-name'), 'click', () => send('test'));
  on(field('voice'), 'change', () => { prefs.voice = field('voice').value; remember(prefs); });
  on(field('rate'), 'input', () => { prefs.rate = Number(field('rate').value); remember(prefs); });
  on(field('sample'), 'click', () => speak('Здравствуйте. Говорите в своём темпе, я слушаю.'));
  on(field('stop-voice'), 'click', stopSpeaking);
  if (speech) on(speech, 'voiceschanged', listVoices);
  on(window, 'pagehide', () => { close(); stopSpeaking(); });
  on(document, 'visibilitychange', () => {
    if (document.hidden && connection) {
      close();
      sayStatus('Микрофон выключен, пока вкладка скрыта. Включите его снова, когда вернётесь.');
    }
  });

  refreshPhrase();
  listVoices();
  setState('off');
  return {speak, destroy() { close(); stopSpeaking(); listeners.abort(); root.replaceChildren(); }};
}
