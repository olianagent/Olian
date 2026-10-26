'use strict';
// Olian admin panel. No dependencies. All untrusted text goes through textContent; links are validated.
const $ = (sel, el = document) => el.querySelector(sel);
const S = { token: sessionStorage.getItem('olian_token') || '', project: sessionStorage.getItem('olian_project') || '', me: null, tab: 'leads' };

// add(): append children, flattening arrays and skipping null/false (Element.append would print them as text).
function add(el, ...kids) {
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}
const put = (el, ...kids) => { el.replaceChildren(); return add(el, ...kids); };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? '' : v);
  }
  return add(el, ...kids);
}
const safeLink = (u) => (/^https:\/\/t\.me\/[\w\-./]+$/.test(u || '') ? u : '');
const fmt = (ts) => (ts ? new Date(ts * 1000).toLocaleString('uk-UA') : '—');
const pct = (v) => (v == null ? '—' : v + '%');

async function api(name, args) {
  const headers = { 'Authorization': 'Bearer ' + S.token, 'Content-Type': 'application/json' };
  if (S.project) headers['X-Olian-Project'] = S.project;
  const r = await fetch('/p/actions/' + name, { method: 'POST', headers, body: JSON.stringify(args || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
  return j.result;
}
async function me() {
  const headers = { 'Authorization': 'Bearer ' + S.token };
  if (S.project) headers['X-Olian-Project'] = S.project;
  const r = await fetch('/p/whoami', { headers });
  if (!r.ok) throw new Error('Невірний токен або проєкт');
  return r.json();
}
const can = (role) => ({ viewer: 1, manager: 2, admin: 3 }[S.me.role] >= { viewer: 1, manager: 2, admin: 3 }[role]);
const toast = (msg, bad) => { const t = $('#toast'); t.textContent = msg; t.className = bad ? 'err' : 'muted'; };
async function guard(fn) { try { await fn(); } catch (e) { toast(e.message, true); } }

function login() {
  const app = $('#app'); app.replaceChildren();
  const tok = h('input', { type: 'password', placeholder: 'Токен проєкту (olp_…) або адмін-ключ', autocomplete: 'off' });
  const proj = h('input', { placeholder: 'Проєкт (потрібен лише для адмін-ключа)', value: S.project });
  const err = h('div', { class: 'err' });
  const submit = async (ev) => {
    ev.preventDefault();
    S.token = tok.value.trim(); S.project = proj.value.trim();
    try { S.me = await me(); sessionStorage.setItem('olian_token', S.token); sessionStorage.setItem('olian_project', S.project); shell(); }
    catch (e) { err.textContent = e.message; }
  };
  app.append(h('form', { class: 'card login', onsubmit: submit }, h('h2', {}, 'Olian · Моніторинг'), tok, proj,
    h('button', { class: 'btn', type: 'submit' }, 'Увійти'), err,
    h('p', { class: 'muted' }, 'Токен зберігається лише в цій вкладці браузера.')));
}

function shell() {
  const app = $('#app'); app.replaceChildren();
  const tabs = [['leads', 'Ліди'], ['sources', 'Джерела'], ['rules', 'Правила'], ['report', 'Звіти']].concat(can('admin') ? [['accounts', 'Акаунти']] : []);
  const nav = h('nav', {}, tabs.map(([k, t]) => h('button', { class: S.tab === k ? 'on' : '', onclick: () => { S.tab = k; shell(); } }, t)));
  app.append(h('header', {}, h('h1', {}, `Моніторинг · ${S.me.name} `, h('span', { class: 'muted' }, `(${S.me.role})`)), nav,
    h('button', { class: 'btn sec', onclick: () => { sessionStorage.clear(); S.token = ''; login(); } }, 'Вийти')),
    h('main', {}, h('div', { id: 'toast', class: 'muted' }), h('div', { id: 'view' })));
  ({ leads: viewLeads, sources: viewSources, rules: viewRules, report: viewReport, accounts: viewAccounts })[S.tab]();
}

// ------------------------------------------------------------------ leads
const STATUSES = ['new', 'review', 'relevant', 'irrelevant', 'processed', 'excluded'];
const STATUS_UA = { new: 'Новий', review: 'На перевірці', relevant: 'Релевантний', irrelevant: 'Нерелевантний', processed: 'Опрацьований', excluded: 'Виключений' };
const badge = (s, label) => h('span', { class: 'badge s-' + s }, label || STATUS_UA[s] || s);

async function viewLeads() {
  const v = $('#view'); const f = S.filter || { status: '', category: '', q: '' }; S.filter = f;
  const st = h('select', {}, h('option', { value: '' }, 'Усі статуси'), STATUSES.map((s) => h('option', { value: s, selected: f.status === s }, STATUS_UA[s])));
  const cat = h('input', { placeholder: 'Категорія', value: f.category });
  const q = h('input', { placeholder: 'Пошук у тексті', value: f.q });
  const go = () => { f.status = st.value; f.category = cat.value.trim(); f.q = q.value.trim(); viewLeads(); };
  const body = h('tbody');
  put(v, h('div', { class: 'card' },
    h('div', { class: 'row' }, st, cat, q, h('button', { class: 'btn', onclick: go }, 'Знайти'),
      can('manager') && h('button', { class: 'btn sec', onclick: exportCsv }, 'Експорт CSV')),
    h('table', {}, h('thead', {}, h('tr', {}, ['Дата', 'Статус', 'Категорія', 'Оцінка', 'Текст', 'Джерело'].map((t) => h('th', {}, t)))), body)));
  await guard(async () => {
    const [leads, sources] = await Promise.all([api('monitor.leads.list', { status: f.status || undefined, category: f.category || undefined, q: f.q || undefined, limit: 200 }), api('monitor.sources.list')]);
    const names = Object.fromEntries(sources.map((s) => [s.id, s.title || s.chat_ref]));
    if (!leads.length) body.append(h('tr', {}, h('td', { colspan: 6, class: 'muted' }, 'Лідів немає.')));
    for (const l of leads) {
      body.append(h('tr', { class: 'click', onclick: () => openLead(l.id) },
        h('td', {}, fmt(l.message_date || l.created_at)), h('td', {}, badge(l.status)), h('td', {}, l.category),
        h('td', {}, `${l.relevance_score.toFixed(2)} / ${l.confidence.toFixed(2)}`),
        h('td', { class: 'txt' }, l.message_text.slice(0, 160) + (l.message_text.length > 160 ? '…' : '')), h('td', {}, names[l.source_id] || '—')));
    }
  });
}
async function exportCsv() {
  await guard(async () => {
    const headers = { 'Authorization': 'Bearer ' + S.token }; if (S.project) headers['X-Olian-Project'] = S.project;
    const qs = new URLSearchParams(); if (S.filter.status) qs.set('status', S.filter.status); if (S.filter.category) qs.set('category', S.filter.category);
    const r = await fetch('/p/monitor/export.csv?' + qs, { headers });
    if (!r.ok) throw new Error('Експорт недоступний (' + r.status + ')');
    const a = h('a', { href: URL.createObjectURL(await r.blob()), download: 'leads.csv' }); document.body.append(a); a.click(); a.remove();
  });
}
async function openLead(id) {
  await guard(async () => {
    const l = await api('monitor.leads.get', { id });
    const note = h('textarea', { placeholder: 'Нотатка / результат ручної обробки' });
    const outcome = h('input', { placeholder: 'Результат (необов’язково)', value: l.outcome });
    const dlg = h('dialog', {});
    const set = (status) => guard(async () => { await api('monitor.leads.set_status', { id, status, note: note.value, outcome: outcome.value }); dlg.close(); dlg.remove(); viewLeads(); });
    const link = safeLink(l.message_url);
    add(dlg, h('div', { class: 'row' }, badge(l.status), h('b', {}, l.category), h('span', { class: 'muted' }, `оцінка ${l.relevance_score.toFixed(2)} · впевненість ${l.confidence.toFixed(2)} · збігів ${l.hit_count} · ${l.qualifier}`)),
      h('p', { class: 'txt' }, l.message_text),
      link ? h('p', {}, h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, 'Відкрити оригінал у Telegram')) : h('p', { class: 'muted' }, 'Посилання на оригінал недоступне.'),
      h('p', {}, h('b', {}, 'Чому відібрано: '), l.qualification_reason),
      h('div', { class: 'notice' }, h('b', {}, 'Контакт. '), 'Автоматичних особистих повідомлень немає. ',
        l.contact_policy.allowed_ways.length ? 'Дозволено: ' + l.contact_policy.allowed_ways.join('; ') : 'Дозволені способи взаємодії не налаштовані.'),
      l.hits.length ? h('p', { class: 'muted' }, 'Повторні збіги: ' + l.hits.map((x) => `${x.kind} (${fmt(x.created_at)})`).join(', ')) : null,
      l.operator_note ? h('p', { class: 'txt muted' }, l.operator_note) : null,
      h('details', {}, h('summary', {}, 'Історія статусів'), h('ul', {}, l.history.map((x) => h('li', {}, `${fmt(x.created_at)} · ${x.actor}: ${x.from_status || '—'} → ${x.to_status}${x.note ? ' · ' + x.note : ''}`)))),
      can('manager') ? [note, outcome, h('div', { class: 'row' }, STATUSES.map((s) => h('button', { class: 'btn sec', onclick: () => set(s) }, STATUS_UA[s])),
        h('button', { class: 'btn sec', onclick: () => guard(async () => { await api('monitor.leads.note', { id, note: note.value }); dlg.close(); dlg.remove(); viewLeads(); }) }, 'Лише нотатка'))] : null,
      can('manager') && !['excluded', 'irrelevant'].includes(l.status) ? h('button', { class: 'btn', onclick: () => guard(async () => { const m = await api('monitor.leads.start_conversation', { id }); toast(`Діалог розпочато: чернетка #${m.id} (${m.status})`); dlg.close(); dlg.remove(); viewLeads(); }) }, 'Почати діалог (агент пише першим повідомленням)') : null,
      can('admin') ? h('button', { class: 'btn bad', onclick: () => confirm('Видалити лід і його історію?') && guard(async () => { await api('monitor.leads.delete', { id }); dlg.close(); dlg.remove(); viewLeads(); }) }, 'Видалити лід') : null,
      h('div', { class: 'row' }, h('button', { class: 'btn sec', onclick: () => { dlg.close(); dlg.remove(); } }, 'Закрити')));
    document.body.append(dlg); dlg.showModal();
  });
}

// ------------------------------------------------------------------ sources
async function viewSources() {
  const v = $('#view'); const body = h('tbody');
  const chat = h('input', { placeholder: 'ID чату (напр. -1001234567890)' }), title = h('input', { placeholder: 'Назва' }), user = h('input', { placeholder: 'username (якщо публічний)' });
  const cands = h('div');
  put(v, h('div', { class: 'card' }, h('table', {}, h('thead', {}, h('tr', {}, ['Джерело', 'Статус', 'Остання успішна', 'Повідомлень', 'Помилок', 'Дублікатів', ''].map((t) => h('th', {}, t)))), body)),
    can('admin') && h('div', { class: 'card' }, h('h3', {}, 'Додати погоджене джерело'),
      h('div', { class: 'row' }, chat, title, user, h('button', { class: 'btn', onclick: () => guard(async () => { await api('monitor.sources.add', { chat_ref: chat.value.trim(), title: title.value, username: user.value }); viewSources(); }) }, 'Додати')),
      h('p', { class: 'muted' }, 'Бот має бути доданий у чат і мати право читати повідомлення. Чужі чати не зберігаються.'), cands));
  await guard(async () => {
    for (const s of await api('monitor.sources.list')) {
      body.append(h('tr', {}, h('td', {}, h('b', {}, s.title || s.chat_ref), h('div', { class: 'muted' }, s.chat_ref)),
        h('td', {}, badge(s.status, s.status), s.status_reason ? h('div', { class: 'muted' }, s.status_reason) : null, s.last_error ? h('div', { class: 'err' }, s.last_error) : null),
        h('td', {}, fmt(s.last_ok_at)), h('td', {}, s.msg_count), h('td', {}, s.error_count), h('td', {}, s.dup_count),
        h('td', {}, can('admin') ? [
          s.status === 'paused' || s.status === 'unavailable' || s.status === 'auth_error'
            ? h('button', { class: 'btn sec', onclick: () => guard(async () => { await api('monitor.sources.update', { id: s.id, status: 'active' }); viewSources(); }) }, 'Увімкнути')
            : h('button', { class: 'btn sec', onclick: () => guard(async () => { await api('monitor.sources.update', { id: s.id, status: 'paused' }); viewSources(); }) }, 'Призупинити'),
          ' ', h('button', { class: 'btn bad', onclick: () => confirm('Видалити джерело та його збережені повідомлення?') && guard(async () => { await api('monitor.sources.remove', { id: s.id }); viewSources(); }) }, 'Видалити')] : null)));
    }
    if (can('admin')) {
      const c = await api('monitor.sources.candidates');
      if (c.length) cands.append(h('div', {}, h('b', {}, 'Чати, де є бот, але вони не погоджені:'), c.map((x) =>
        h('div', { class: 'row' }, `${x.title || '—'} (${x.chat_ref}, ${x.type})`, h('button', { class: 'btn sec', onclick: () => guard(async () => { await api('monitor.sources.add', { chat_ref: x.chat_ref, title: x.title, username: x.username }); viewSources(); }) }, 'Погодити')))));
    }
  });
}

// ------------------------------------------------------------------ rules
const lines = (t) => t.split('\n').map((x) => x.trim()).filter(Boolean);
async function viewRules() {
  const v = $('#view'); const list = h('div'); put(v, list);
  await guard(async () => {
    const rules = await api('monitor.rules.list');
    if (can('admin')) rules.push({ id: 0, name: '', category: 'general', phrases: [], all_of: [], exclude: [], min_hits: 1, weight: 1, enabled: true });
    for (const r of rules) list.append(ruleCard(r));
  });
}
function ruleCard(r) {
  const name = h('input', { value: r.name, placeholder: 'Назва набору' }), cat = h('input', { value: r.category, placeholder: 'Категорія' });
  const ph = h('textarea', { placeholder: 'Фрази, по одній у рядку (куп* = префікс)' }); ph.value = r.phrases.join('\n');
  const all = h('textarea', { placeholder: 'Обов’язкові терміни (усі), по одному у рядку' }); all.value = r.all_of.join('\n');
  const req = h('textarea', { placeholder: 'Ознаки наміру (достатньо однієї): купити, замовити, шукаю…' }); req.value = (r.require_any || []).join('\n');
  const ex = h('textarea', { placeholder: 'Виключення, по одному у рядку' }); ex.value = r.exclude.join('\n');
  const mh = h('input', { type: 'number', min: 1, max: 10, value: r.min_hits }), w = h('input', { type: 'number', step: '0.1', min: 0.1, max: 2, value: r.weight });
  const en = h('input', { type: 'checkbox', checked: r.enabled });
  const out = h('div', { class: 'muted' });
  const draft = () => ({ id: r.id || undefined, name: name.value, category: cat.value, phrases: lines(ph.value), all_of: lines(all.value), require_any: lines(req.value), exclude: lines(ex.value), min_hits: +mh.value, weight: +w.value, enabled: en.checked });
  const ro = !can('admin');
  [name, cat, ph, all, req, ex, mh, w, en].forEach((e) => { e.disabled = ro; });
  return h('div', { class: 'card' }, h('div', { class: 'row' }, name, cat, h('label', {}, en, ' увімкнено'), h('label', {}, 'мін. збігів ', mh), h('label', {}, 'вага ', w)), ph, all, req, ex,
    h('div', { class: 'row' },
      can('manager') && h('button', { class: 'btn sec', onclick: () => guard(async () => { const t = await api('monitor.rules.test', { rule: draft() }); out.textContent = `Збігів: ${t.matches} із ${t.scanned}. ` + t.samples.slice(0, 5).map((s) => '«' + s.text.slice(0, 80) + '»').join(' · '); }) }, 'Тест на збережених'),
      !ro && h('button', { class: 'btn', onclick: () => guard(async () => { await api('monitor.rules.save', { rule: draft() }); viewRules(); }) }, r.id ? 'Зберегти' : 'Створити'),
      !ro && r.id ? h('button', { class: 'btn bad', onclick: () => confirm('Видалити правило?') && guard(async () => { await api('monitor.rules.delete', { id: r.id }); viewRules(); }) }, 'Видалити') : null), out);
}

// ------------------------------------------------------------------ reports
async function viewReport() {
  const v = $('#view'); const days = h('select', {}, [7, 30, 90].map((d) => h('option', { value: d, selected: d === (S.days || 30) }, d + ' днів')));
  const box = h('div'); days.addEventListener('change', () => { S.days = +days.value; viewReport(); });
  put(v, h('div', { class: 'row' }, days), box);
  await guard(async () => {
    const r = await api('monitor.report', { days: S.days || 30 });
    const k = (t, val) => h('div', { class: 'kpi' }, h('b', {}, val), h('span', { class: 'muted' }, t));
    const max = Math.max(1, ...r.daily.map((d) => d.received));
    box.append(h('div', { class: 'grid' }, k('Отримано повідомлень', r.received), k('Збіги за словами', r.level1_matches), k('Створено лідів', r.leads_created),
      k('Відхилено кваліфікатором', r.rejected_by_qualifier), k('Дублікати', r.duplicates), k('Підтверджено релевантних', pct(r.confirmed_relevant_pct)),
      k('Помилки джерел', r.source_errors), k('Проблемні джерела', r.sources_unhealthy)),
      h('div', { class: 'card' }, h('h3', {}, 'Джерела'), h('table', {}, h('thead', {}, h('tr', {}, ['Джерело', 'Статус', 'Повідомл.', 'Лідів', 'Підтверджено', 'Відхилено оператором', 'Помилок'].map((t) => h('th', {}, t)))),
        h('tbody', {}, r.sources.map((s) => h('tr', {}, h('td', {}, s.title), h('td', {}, badge(s.status, s.status)), h('td', {}, s.messages), h('td', {}, s.leads), h('td', {}, s.confirmed), h('td', {}, s.rejected_by_operator), h('td', {}, s.errors)))))),
      h('div', { class: 'card' }, h('h3', {}, 'Правила'), h('table', {}, h('thead', {}, h('tr', {}, ['Правило', 'Збігів', 'Лідів'].map((t) => h('th', {}, t)))),
        h('tbody', {}, r.rules.map((x) => h('tr', {}, h('td', {}, x.rule), h('td', {}, x.matches), h('td', {}, x.leads)))))),
      h('div', { class: 'card' }, h('h3', {}, 'За днями'), r.daily.map((d) => h('div', { class: 'row' }, h('span', { class: 'muted' }, d.day), (() => { const b = h('div', { class: 'bar' }); b.style.width = Math.round(280 * d.received / max) + 'px'; return b; })(), `${d.received} / збігів ${d.matched || 0}`))));
  });
}

// ------------------------------------------------------------------ accounts (admin)
async function viewAccounts() {
  const v = $('#view'); const body = h('tbody'); const cold = h('div'); const form = h('div');
  put(v, h('div', { class: 'card' }, h('h3', {}, 'Акаунти Telegram для особистих повідомлень'),
    h('p', { class: 'muted' }, 'Нова сесія створюється за номером телефону й кодом, який Telegram надішле один раз. Далі номер і телефон не потрібні: сесія зберігається зашифрованою. Новий акаунт стартує з низького ліміту й поступово його збільшує.'),
    h('table', {}, h('thead', {}, h('tr', {}, ['Акаунт', 'Статус', 'Відправник', 'Сьогодні', ''].map((t) => h('th', {}, t)))), body)), form, cold);
  await guard(async () => {
    const [accs, st] = await Promise.all([api('tg.accounts.list'), api('outreach.settings')]);
    if (!accs.length) body.append(h('tr', {}, h('td', { colspan: 5, class: 'muted' }, 'Акаунтів ще немає.')));
    for (const a of accs) {
      const sd = a.sender;
      body.append(h('tr', {}, h('td', {}, h('b', {}, a.label), h('div', { class: 'muted' }, `${a.phone_hint} · @${a.me.username || '—'}`)),
        h('td', {}, badge(a.status === 'active' ? 'active' : 'unavailable', a.status), a.status_reason ? h('div', { class: 'err' }, a.status_reason) : null),
        h('td', {}, sd ? [badge(sd.status === 'active' ? 'active' : 'unavailable', sd.status), sd.reason ? h('div', { class: 'err' }, sd.reason) : null, sd.paused_until > Date.now() / 1000 ? h('div', { class: 'muted' }, 'пауза до ' + fmt(sd.paused_until)) : null] : '—'),
        h('td', {}, sd ? `${sd.sent_today} / ${sd.daily_cap}` : '—'),
        h('td', {}, sd && sd.status === 'tripped' ? h('button', { class: 'btn sec', onclick: () => confirm('Спершу з’ясуйте причину. Увімкнути відправник?') && guard(async () => { await api('outreach.reset_sender', { label: a.label }); viewAccounts(); }) }, 'Скинути вимикач') : null,
          ' ', h('button', { class: 'btn bad', onclick: () => confirm('Вийти з акаунта в Telegram і видалити сесію?') && guard(async () => { await api('tg.accounts.remove', { label: a.label }); viewAccounts(); }) }, 'Відключити'))));
    }
    const label = h('input', { placeholder: 'Назва (напр. acc1)' }), phone = h('input', { placeholder: '+380XXXXXXXXX' }), apiId = h('input', { placeholder: 'api_id', inputmode: 'numeric' }), apiHash = h('input', { placeholder: 'api_hash', type: 'password', autocomplete: 'off' });
    const code = h('input', { placeholder: 'Код з Telegram', autocomplete: 'off' }), pass = h('input', { placeholder: 'Пароль 2FA (якщо є)', type: 'password', autocomplete: 'off' }), step2 = h('div', { class: 'row' });
    step2.hidden = true;
    put(step2, code, pass, h('button', { class: 'btn', onclick: () => guard(async () => { const r = await api('tg.accounts.login_code', { label: label.value.trim(), code: code.value, password: pass.value || undefined }); toast('Підключено: ' + (r.me.username || r.me.first_name)); viewAccounts(); }) }, 'Підтвердити'));
    put(form, h('div', { class: 'card' }, h('h3', {}, 'Підключити акаунт'), h('div', { class: 'row' }, label, phone, apiId, apiHash,
      h('button', { class: 'btn', onclick: () => guard(async () => { const r = await api('tg.accounts.login_start', { label: label.value.trim(), phone: phone.value.trim(), api_id: +apiId.value, api_hash: apiHash.value.trim() }); apiHash.value = ''; step2.hidden = false; toast('Код надіслано на ' + r.phone); }) }, 'Надіслати код')), step2,
      h('p', { class: 'muted' }, 'api_id та api_hash беруться на my.telegram.org для цього ж акаунта. Не використовуйте основний особистий акаунт.')));
    const mode = h('select', {}, [['blocked', 'Закрито: лише за записаною підставою'], ['allowed', 'Дозволено: писати авторам знайдених дописів']].map(([k, t]) => h('option', { value: k, selected: st.cold_contact === k }, t)));
    put(cold, h('div', { class: 'card' }, h('h3', {}, 'Холодний контакт'), h('div', { class: 'row' }, mode,
      h('button', { class: 'btn', onclick: () => confirm('Ви берете на себе рішення писати людям без попередньої згоди. Усі ліміти й стоп-лист лишаються. Зберегти?') && guard(async () => { await api('outreach.cold_contact', { mode: mode.value }); viewAccounts(); }) }, 'Зберегти')),
      h('p', { class: 'muted' }, `Ліміти: ${st.max_per_lead} повідомлення на людину, пауза ${st.min_gap_hours} год, ${st.daily_cap_total}/день на проєкт, розгін нового акаунта ${st.ramp.join('→')}/день, вікно ${st.window.start}–${st.window.end} (UTC${st.window.utc_offset >= 0 ? '+' : ''}${st.window.utc_offset}). Підтвердження людиною: ${st.require_approval ? 'так' : 'ні'}.`)));
  });
}

(async function init() {
  if (!S.token) return login();
  try { S.me = await me(); shell(); } catch (e) { login(); }
})();
