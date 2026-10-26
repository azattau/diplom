'use strict';
const $ = id => document.getElementById(id);
const localHost = ['localhost', '127.0.0.1'].includes(location.hostname);
const safeStorage = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* private mode */ } }
};
const state = {
  url: safeStorage.get('serverUrl') || (localHost ? location.origin : 'http://127.0.0.1:8765'),
  token: safeStorage.get('token') || '', reports: [], connected: false, busy: false,
  model: safeStorage.get('model') || '', markers: new Map(), lastSignature: '', polling: false
};
const fragmentToken = new URLSearchParams(location.hash.slice(1)).get('token');
if (fragmentToken) {
  state.token = fragmentToken;
  safeStorage.set('token', fragmentToken);
  history.replaceState(null, '', location.pathname + location.search);
}
let map, markerLayer;
if (window.L) {
  map = L.map('map', {zoomControl: false}).setView([54.5, 56.3], 7);
  L.control.zoom({position: 'bottomright'}).addTo(map);
  let tileErrors = 0;
  const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);
  tiles.on('tileerror', () => { if (++tileErrors > 4) $('mapError').hidden = false; });
  tiles.on('tileload', () => { tileErrors = 0; $('mapError').hidden = true; });
  markerLayer = L.layerGroup().addTo(map);
} else $('mapError').hidden = false;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function formatTime(value) {
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Yekaterinburg'
  }).format(new Date(value));
}
function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').className = 'notice' + (error ? ' error' : '');
  $('notice').hidden = false;
}
function setConnected(value) {
  state.connected = value;
  $('statusDot').classList.toggle('connected', value);
  $('connectionLabel').textContent = value ? 'Локальный сервер подключён' : 'Подключить Ollama';
  $('offlineBanner').hidden = value;
  $('submitButton').disabled = !value || !state.model || state.busy;
  $('mapSubtitle').textContent = value ? 'Башкортостан · время UTC+5' : 'Подключите локальный сервер';
}
function normalizeServer(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Используйте локальный адрес, например http://127.0.0.1:8765.');
  }
  return url.origin;
}
async function api(path, body, timeout = 200000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(state.url + '/api/' + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: 'Bearer ' + state.token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal, credentials: 'omit', cache: 'no-store'
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) setConnected(false);
      throw new Error(data.error || 'Ошибка сервера: ' + response.status);
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Время ожидания истекло. Сервер мог сохранить сообщение — обновите ленту перед повторной отправкой.');
    if (error instanceof TypeError) {
      setConnected(false);
      throw new Error('Нет связи с Python-сервером. Запустите start.bat. На GitHub Pages разрешите доступ к локальной сети; если браузер блокирует его, откройте локальную версию из окна запуска.');
    }
    throw error;
  } finally { clearTimeout(timer); }
}
function openConnection() {
  $('serverUrl').value = state.url;
  $('connectionToken').value = state.token;
  $('connectionError').hidden = true;
  $('connectionDialog').showModal();
}
async function connect() {
  const result = await api('status', undefined, 12000);
  const previous = state.model;
  $('model').replaceChildren();
  if (!result.models.length) $('model').append(new Option('Нет локальных моделей', ''));
  for (const model of result.models) $('model').append(new Option(model, model));
  state.model = result.models.includes(previous) ? previous : result.models[0] || '';
  $('model').value = state.model;
  safeStorage.set('model', state.model);
  setConnected(true);
  await refresh(true);
  if (!result.ollama) notice(result.error, true);
  else if (!result.models.length) notice('Ollama запущена, но локальных моделей нет. Скачайте модель через ollama pull, затем подключитесь повторно.', true);
  return result;
}
function popup(report, observation) {
  const box = node('div');
  box.append(node('h3', observation.location.name, 'popup-title'));
  box.append(node('p', observation.location.district, 'popup-meta'));
  box.append(node('p', 'Получено: ' + formatTime(report.created_at) + ' (UTC+5)', 'popup-meta'));
  if (observation.observed_time_text) box.append(node('p', 'Время из сообщения: ' + observation.observed_time_text, 'popup-meta'));
  box.append(node('p', report.text, 'popup-text'));
  box.append(node('p', 'Координаты населённого пункта из справочника. Положение птицы приблизительное.', 'popup-meta'));
  return box;
}
function focusObservation(id) {
  const marker = state.markers.get(id);
  if (marker && map) {
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 11));
    marker.openPopup();
    if (innerWidth <= 680) $('map').scrollIntoView({behavior: 'smooth', block: 'center'});
  }
}
function fitAll() {
  if (!map) return;
  const points = Array.from(state.markers.values(), marker => marker.getLatLng());
  if (points.length) map.fitBounds(L.latLngBounds(points), {padding: [80, 100], maxZoom: 11});
  else map.setView([54.5, 56.3], 7);
}
function renderMap() {
  const openId = Array.from(state.markers).find(([, m]) => m.isPopupOpen())?.[0];
  if (markerLayer) markerLayer.clearLayers();
  state.markers.clear();
  let count = 0;
  const colocated = new Map();
  for (const report of state.reports) for (const item of report.observations) {
    if (item.status !== 'resolved' || !item.location) continue;
    count++;
    if (!map) continue;
    const key = `${item.location.lat},${item.location.lon}`;
    if (!colocated.has(key)) colocated.set(key, []);
    colocated.get(key).push({report, item});
  }
  for (const group of colocated.values()) {
    const first = group[0].item.location;
    const marker = L.marker([first.lat, first.lon], {
      title: first.name, icon: L.divIcon({className: 'bird-pin', html: '', iconSize: [28, 28], iconAnchor: [14, 14]})
    }).addTo(markerLayer);
    const content = node('div');
    if (group.length > 1) content.append(node('p', `Сообщений в этом месте: ${group.length}`, 'popup-meta'));
    group.forEach(({report, item}, index) => {
      if (index) content.append(node('hr'));
      content.append(popup(report, item));
      state.markers.set(item.id, marker);
    });
    marker.bindPopup(content, {maxWidth: 330});
  }
  if (openId) state.markers.get(openId)?.openPopup();
  $('pointCount').textContent = count;
}
function renderReports() {
  const query = $('search').value.trim().toLocaleLowerCase('ru');
  const reports = state.reports.filter(r => (r.text + ' ' + r.observations.map(i => i.place).join(' ')).toLocaleLowerCase('ru').includes(query));
  $('reportCount').textContent = state.reports.length;
  const list = $('reports');
  list.replaceChildren();
  if (!reports.length) {
    const empty = node('div', undefined, 'empty');
    empty.append(node('span', '⌁', 'empty-icon'), node('h3', query ? 'Ничего не найдено' : 'Начните с наблюдения'),
      node('p', query ? 'Попробуйте другое слово.' : 'Введите сообщение выше — здесь появятся его текст, время и найденное место.'));
    list.append(empty);
    return;
  }
  for (const report of reports) {
    const card = node('article', undefined, 'report-card');
    const top = node('div', undefined, 'report-top');
    top.append(node('span', 'СООБЩЕНИЕ'), node('time', formatTime(report.created_at) + ' · UTC+5'));
    card.append(top, node('p', report.text, 'report-body'));
    if (!report.observations.length) card.append(node('p', 'Место наблюдения не указано или не распознано. Уточните сообщение и отправьте заново.', 'report-status'));
    for (const item of report.observations) {
      if (item.status === 'resolved') {
        const button = node('button', '⌖ ' + item.location.name, 'location-button');
        button.type = 'button';
        button.addEventListener('click', () => focusObservation(item.id));
        card.append(button);
      } else if (item.status === 'ambiguous') {
        card.append(node('p', `${item.place}: найдено ${item.candidates.length} вариантов. Уточните место.`, 'report-status'));
        const select = node('select', undefined, 'resolve-select');
        select.setAttribute('aria-label', 'Уточнить населённый пункт ' + item.place);
        select.append(new Option('Выберите населённый пункт', ''));
        for (const candidate of item.candidates) select.append(new Option(`${candidate.name} — ${candidate.district}, ${candidate.municipality}`, candidate.id));
        const button = node('button', 'Подтвердить место', 'resolve-button');
        button.type = 'button'; button.disabled = true;
        select.addEventListener('change', () => { button.disabled = !select.value; });
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            const updated = await api('resolve', {report_id: report.id, observation_id: item.id, candidate_id: select.value});
            state.reports = state.reports.map(r => r.id === updated.id ? updated : r);
            renderReports(); renderMap(); focusObservation(item.id);
            notice('Место уточнено. Наблюдение добавлено на карту.');
          } catch (error) { notice(error.message, true); button.disabled = false; }
        });
        card.append(select, button);
      } else card.append(node('p', `${item.place}: нет точного совпадения в справочнике с учётом района. Проверьте название и отправьте уточнённое сообщение.`, 'report-status'));
    }
    const details = node('details', undefined, 'report-details');
    details.append(node('summary', 'Результат анализа'));
    details.append(node('p', 'Модель: ' + report.model));
    for (const item of report.observations) {
      details.append(node('p', `${item.place}${item.district ? ', ' + item.district : ''} · ${item.relation === 'near' ? 'рядом' : item.relation === 'over' ? 'над' : 'в населённом пункте'}`));
      details.append(node('p', 'Цитата: «' + item.evidence + '»'));
      if (item.observed_time_text) details.append(node('p', 'Время наблюдения из текста: ' + item.observed_time_text));
      if (item.resolved_by === 'operator') details.append(node('p', 'Место уточнено оператором.'));
    }
    card.append(details); list.append(card);
  }
}
async function refresh(fit = false) {
  const data = await api('reports', undefined, 10000);
  const signature = JSON.stringify(data.reports);
  if (signature !== state.lastSignature) {
    state.reports = data.reports; state.lastSignature = signature;
    renderReports(); renderMap();
  }
  if (fit) fitAll();
}
$('reportForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.busy || !state.connected || !state.model) return;
  const text = $('message').value.trim();
  if (text.length < 3) { notice('Введите сообщение длиной не менее 3 символов.', true); return; }
  state.busy = true; $('message').readOnly = true; $('exampleButton').disabled = true; $('model').disabled = true;
  $('submitButton').replaceChildren(node('span', undefined, 'spinner'), node('span', 'Анализируем сообщение…'));
  $('submitButton').disabled = true;
  notice('Ollama анализирует текст. При первом запросе загрузка модели может занять несколько минут.');
  let submitted = false;
  try {
    const report = await api('reports', {text, model: state.model});
    submitted = true;
    state.reports.unshift(report); renderReports(); renderMap();
    const placed = report.observations.filter(o => o.status === 'resolved');
    $('message').value = ''; $('charCount').textContent = '0 / 4000';
    if (placed.length) {
      fitAll(); state.markers.get(placed[0].id)?.openPopup();
      notice(`Сообщение сохранено. Мест на карте: ${placed.length}.` + (placed.length < report.observations.length ? ' Остальные места требуют уточнения в ленте.' : ''));
    } else notice(report.observations.length ? 'Сообщение сохранено. Уточните место в ленте сообщений.' : 'Сообщение сохранено, но место наблюдения не найдено. Добавьте название населённого пункта.', true);
  } catch (error) { notice(error.message, true); }
  finally {
    state.busy = false; $('message').readOnly = false; $('exampleButton').disabled = false; $('model').disabled = false;
    $('submitButton').textContent = '＋ Нанести на карту';
    setConnected(state.connected);
    if (submitted) state.lastSignature = '';
  }
});
$('message').addEventListener('input', () => { $('charCount').textContent = `${$('message').value.length} / 4000`; });
$('exampleButton').addEventListener('click', () => {
  $('message').value = 'В Уфе возле парка заметили птицу в 14:20.';
  $('message').dispatchEvent(new Event('input')); $('message').focus();
});
$('model').addEventListener('change', () => { state.model = $('model').value; safeStorage.set('model', state.model); setConnected(state.connected); });
$('search').addEventListener('input', renderReports);
$('fitButton').addEventListener('click', fitAll);
$('connectionButton').addEventListener('click', openConnection);
$('bannerConnect').addEventListener('click', openConnection);
$('closeDialog').addEventListener('click', () => $('connectionDialog').close());
$('connectionForm').addEventListener('submit', async event => {
  event.preventDefault();
  $('connectSubmit').disabled = true; $('connectionError').hidden = true;
  try {
    state.url = normalizeServer($('serverUrl').value.trim());
    state.token = $('connectionToken').value.trim();
    if (!state.token) throw new Error('Введите код подключения из окна запуска.');
    await connect();
    safeStorage.set('serverUrl', state.url); safeStorage.set('token', state.token);
    $('connectionDialog').close();
  } catch (error) {
    setConnected(false);
    $('connectionError').textContent = error.message; $('connectionError').hidden = false;
  } finally { $('connectSubmit').disabled = false; }
});
setInterval(async () => {
  if (!state.connected || state.busy || state.polling || document.hidden) return;
  state.polling = true;
  try { await refresh(); } catch (error) { notice(error.message, true); } finally { state.polling = false; }
}, 5000);
if (state.token) {
  try { state.url = normalizeServer(state.url); connect().catch(error => notice(error.message, true)); }
  catch (error) { notice(error.message, true); }
}
