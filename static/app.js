// Мониторинг ремонтов — небольшой клиентский скрипт (без внешних библиотек).
function initImportPage(running, seq) {
  var pick = document.getElementById('pick');
  var path = document.getElementById('path');
  var msg = document.getElementById('pick-msg');
  if (pick) {
    pick.addEventListener('click', function () {
      pick.disabled = true;
      msg.textContent = 'Открывается окно выбора файла (оно может появиться за окном браузера)…';
      var body = new URLSearchParams();
      body.append('initial', path.value || '');
      fetch('/api/pick-file', {method: 'POST', body: body})
        .then(function (r) { return r.json(); })
        .then(function (d) {
          pick.disabled = false;
          if (d.path) { path.value = d.path; msg.textContent = 'Файл выбран. Нажмите «Проверить структуру» или «Загрузить данные».'; }
          else if (d.error) { msg.textContent = d.error; }
          else { msg.textContent = 'Файл не выбран.'; }
        })
        .catch(function () { pick.disabled = false; msg.textContent = 'Не удалось открыть окно выбора. Вставьте путь вручную.'; });
    });
  }
  document.querySelectorAll('a.recent').forEach(function (a) {
    a.addEventListener('click', function (e) { e.preventDefault(); path.value = a.dataset.path; });
  });
  if (!running) return;
  var bar = document.getElementById('bar');
  var stage = document.getElementById('job-stage');
  var title = document.getElementById('job-title');
  var card = document.getElementById('job-card');
  var start = document.getElementById('start');
  card.hidden = false;
  function poll() {
    fetch('/import/status').then(function (r) { return r.json(); }).then(function (st) {
      bar.style.width = (st.pct || 0) + '%';
      stage.textContent = (st.stage || '') + ' ' + (st.detail || '') + (st.elapsed ? ' · ' + Math.round(st.elapsed) + ' с' : '');
      if (st.running) { setTimeout(poll, 700); }
      else { title.textContent = 'Загрузка завершена'; window.location.reload(); }
    }).catch(function () { setTimeout(poll, 1500); });
  }
  if (start) start.disabled = true;
  poll();
}

// Мультивыбор: «Выбрать все» / «Снять все», текст на кнопке, закрытие по клику вне списка.
(function () {
  function refresh(ms) {
    var boxes = ms.querySelectorAll('input[type=checkbox]');
    var sel = Array.prototype.filter.call(boxes, function (b) { return b.checked; });
    var txt = ms.querySelector('.ms-text');
    var btn = ms.querySelector('.ms-btn');
    var all = sel.length === 0 || sel.length === boxes.length;
    if (all) { txt.textContent = 'Все'; }
    else if (sel.length === 1) { txt.textContent = sel[0].value; }
    else { txt.textContent = 'Выбрано ' + sel.length + ' из ' + boxes.length; }
    btn.classList.toggle('on', !all);
  }
  document.querySelectorAll('details.ms').forEach(function (ms) {
    ms.addEventListener('change', function () { refresh(ms); });
    ms.querySelectorAll('[data-ms]').forEach(function (b) {
      b.addEventListener('click', function () {
        var on = b.dataset.ms === 'all';
        ms.querySelectorAll('input[type=checkbox]').forEach(function (c) { c.checked = on; });
        refresh(ms);
      });
    });
  });
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.ms[open]').forEach(function (ms) {
      if (!ms.contains(e.target)) { ms.removeAttribute('open'); }
    });
  });
})();

// Комментарий прямо в строке таблицы (textarea.cm с data-et): сохраняется сразу по номеру ET.
// Если программа не отвечает — текст запоминается в браузере и сохраняется, как только она ответит.
// Используется в «Листе», «Сроке 10 дней» и «Где застряли». hooks.saved(textarea, ответ) — после сохранения
// для каждой ячейки этого ET; hooks.changed() — после любого сохранения.
function initCommentCells(scope, author, banner, hooks) {
  hooks = hooks || {};
  var KEY = 'rm_pending_comments';
  var pending = {};
  try { pending = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { pending = {}; }
  function storePending() { try { localStorage.setItem(KEY, JSON.stringify(pending)); } catch (e) {} }
  function areas(et) { return Array.prototype.slice.call(scope.querySelectorAll('textarea.cm[data-et="' + et + '"]')); }
  function grow(t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight + 2, 220) + 'px'; }
  function mark(t, st) {
    var td = t.closest('td');
    td.classList.remove('st-saving', 'st-saved', 'st-error', 'st-dirty');
    if (st) td.classList.add('st-' + st);
    if (st === 'saved') setTimeout(function () { td.classList.remove('st-saved'); }, 1800);
  }
  function showBanner() {
    if (!banner) return;
    var n = Object.keys(pending).length;
    if (!n) { banner.hidden = true; return; }
    banner.hidden = false;
    banner.textContent = 'Не сохранено комментариев: ' + n + '. Программа не отвечает — проверьте, что окно «Мониторинг ремонтов» открыто ' +
      '(или запустите run.bat). Текст остался в ячейках и запомнен в браузере; сохраню автоматически, как только программа ответит.';
  }
  function stamp(iso) { return iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4) + ' ' + iso.slice(11, 16) : ''; }
  function save(t) {
    var et = t.dataset.et, text = t.value.trim();
    if (text === (t.dataset.saved || '') && !(et in pending)) { mark(t, ''); return; }
    pending[et] = text; storePending(); mark(t, 'saving');
    fetch('/api/comment', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({et: et, text: text, author: author || ''})})
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        if (pending[et] === text) { delete pending[et]; storePending(); }
        areas(et).forEach(function (a) {  // один ремонт может стоять в нескольких строках
          var prev = a.dataset.saved || '', now = a.value.trim();
          a.dataset.saved = d.text;
          // обновляем все поля этого ET, кроме тех, где уже начали писать что-то новое
          if (a === t ? now === text : now === prev) a.value = d.text;
          a.closest('tr').classList.toggle('has-cm', !!d.text);
          if (d.updated_at) a.title = (d.text ? d.text + '\n— ' : '') + 'изменён ' + stamp(d.updated_at);
          if (hooks.saved) hooks.saved(a, d);
        });
        mark(t, 'saved'); showBanner();
        if (hooks.changed) hooks.changed();
      })
      .catch(function () { mark(t, 'error'); showBanner(); });
  }
  scope.querySelectorAll('textarea.cm').forEach(function (t) { t.dataset.saved = t.value; });
  // несохранённое с прошлого раза (например, программа была закрыта)
  Object.keys(pending).forEach(function (et) {
    var t = areas(et)[0];
    if (t && t.value.trim() !== pending[et]) { t.value = pending[et]; }
    if (t) save(t);
  });
  showBanner();
  setInterval(function () { Object.keys(pending).forEach(function (et) { var t = areas(et)[0]; if (t && document.activeElement !== t) save(t); }); }, 6000);
  window.addEventListener('beforeunload', function (e) {
    if (Object.keys(pending).length) { e.preventDefault(); e.returnValue = ''; }
  });
  scope.addEventListener('focusin', function (e) { if (e.target.matches('textarea.cm')) grow(e.target); });
  scope.addEventListener('input', function (e) { if (e.target.matches('textarea.cm')) { grow(e.target); mark(e.target, 'dirty'); } });
  scope.addEventListener('focusout', function (e) {
    var t = e.target;
    if (t.matches('textarea.cm')) { t.style.height = ''; save(t); }
  });
  scope.addEventListener('keydown', function (e) {
    var t = e.target;
    if (!t.matches('textarea.cm')) return;
    if (e.key === 'Escape') { t.value = t.dataset.saved || ''; mark(t, ''); t.blur(); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.ctrlKey) {  // сохранить и перейти на строку ниже
      e.preventDefault();
      var vis = Array.prototype.filter.call(scope.querySelectorAll('textarea.cm'), function (a) { return a.closest('tr').style.display !== 'none'; });
      var next = vis.slice(vis.indexOf(t) + 1).filter(function (a) { return a.dataset.et !== t.dataset.et; })[0];  // следующий ремонт
      t.blur();
      if (next) { next.focus(); next.setSelectionRange(next.value.length, next.value.length); }
    }
  });
}

// «Лист»: комментарий в строке (сохранение по номеру ET), сортировка и фильтры по столбцам.
function initSheet(author) {
  var table = document.getElementById('sheet');
  if (!table) return;
  var tbody = table.tBodies[0];
  var banner = document.getElementById('save-banner');
  var shownEl = document.getElementById('shown');
  var ncmEl = document.getElementById('n-cm');
  var exportLink = document.getElementById('export-link');
  var OFF = tbody.querySelector('td.pickcell') || table.querySelector('th.pickcol') ? 1 : 0;  // столбец выбора слева
  function rows() { return Array.prototype.slice.call(tbody.rows).filter(function (r) { return r.dataset.et; }); }
  function countCm() {
    if (ncmEl) ncmEl.textContent = rows().filter(function (r) { return r.style.display !== 'none' && r.classList.contains('has-cm'); }).length;
  }
  initCommentCells(tbody, author, banner, {
    saved: function (a, d) { a.parentNode.dataset.k = d.text.toLowerCase(); },
    changed: countCm,
  });

  // сортировка (как в Excel: по нажатию на заголовок)
  var heads = table.tHead.rows[0].cells;
  function exportHref() {
    var u = new URL(exportLink.href, location.href);
    for (var k of Array.from(u.searchParams.keys())) { if (/^cf\d+$/.test(k) || k === 'sort' || k === 'dir') u.searchParams.delete(k); }
    u.searchParams.set('sort', table.dataset.sort); u.searchParams.set('dir', table.dataset.dir);
    table.querySelectorAll('input.cf').forEach(function (inp) { if (inp.value.trim()) u.searchParams.set('cf' + inp.dataset.idx, inp.value.trim()); });
    exportLink.href = u.pathname + u.search;
    var p = new URL(location.href);
    p.searchParams.set('sort', table.dataset.sort); p.searchParams.set('dir', table.dataset.dir);
    table.querySelectorAll('input.cf').forEach(function (inp) { if (inp.value.trim()) p.searchParams.set('cf' + inp.dataset.idx, inp.value.trim()); else p.searchParams.delete('cf' + inp.dataset.idx); });
    try { history.replaceState(null, '', p.pathname + p.search); } catch (e) {}
  }
  Array.prototype.forEach.call(heads, function (th) {
    var b = th.querySelector('button.sh');
    if (!b) return;
    b.addEventListener('click', function () {
      var idx = +th.dataset.idx;
      var desc = String(idx) === table.dataset.sort ? table.dataset.dir !== 'desc' : true;
      table.dataset.sort = idx; table.dataset.dir = desc ? 'desc' : 'asc';
      var list = rows();
      list.sort(function (a, b2) {
        var x = a.cells[idx + OFF].dataset.k || '', y = b2.cells[idx + OFF].dataset.k || '';
        if (x === '' && y !== '') return 1;
        if (y === '' && x !== '') return -1;
        return (x < y ? -1 : x > y ? 1 : 0) * (desc ? -1 : 1);
      });
      list.forEach(function (r) { tbody.appendChild(r); });
      table.querySelectorAll('thead .arr').forEach(function (s) { s.textContent = ''; });
      th.querySelector('.arr').textContent = desc ? '↓' : '↑';
      exportHref();
    });
  });

  // фильтры по столбцам
  var timer = null;
  function applyFilters() {
    var f = [];
    table.querySelectorAll('input.cf').forEach(function (inp) { var v = inp.value.trim().toLowerCase(); if (v) f.push([+inp.dataset.idx, v]); });
    var n = 0;
    rows().forEach(function (r) {
      var ok = f.every(function (p) {
        var cell = r.cells[p[0] + OFF];
        var ta = cell.querySelector('textarea');
        var text = (ta ? ta.value : cell.textContent).trim().toLowerCase();
        return p[1] === '(пусто)' ? text === '' : text.indexOf(p[1]) >= 0;
      });
      r.style.display = ok ? '' : 'none';
      if (ok) n++;
    });
    shownEl.textContent = n;
    countCm();
    exportHref();
  }
  table.querySelectorAll('input.cf').forEach(function (inp) {
    inp.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(applyFilters, 150); });
  });
  if (Array.prototype.some.call(table.querySelectorAll('input.cf'), function (i) { return i.value.trim(); })) applyFilters();
  else exportHref();

  // выбор ремонтов и письмо в АСЦ
  var bar = document.getElementById('pickbar'), pn = document.getElementById('pick-n');
  var all = document.getElementById('pick-all');
  function picked() { return Array.prototype.map.call(tbody.querySelectorAll('input.pick:checked'), function (c) { return c.value; }); }
  function syncPicks() {
    var p = picked();
    if (bar) { bar.hidden = p.length === 0; pn.textContent = p.length; }
    tbody.querySelectorAll('input.pick').forEach(function (c) { c.closest('tr').classList.toggle('picked', c.checked); });
  }
  tbody.addEventListener('change', function (e) { if (e.target.matches('input.pick')) syncPicks(); });
  if (all) all.addEventListener('change', function () {
    rows().forEach(function (r) { if (r.style.display !== 'none') { var c = r.querySelector('input.pick'); if (c) c.checked = all.checked; } });
    syncPicks();
  });
  var clr = document.getElementById('pick-clear');
  if (clr) clr.addEventListener('click', function () { tbody.querySelectorAll('input.pick').forEach(function (c) { c.checked = false; }); if (all) all.checked = false; syncPicks(); });
  var dlg = document.getElementById('mail-dlg'), list = document.getElementById('mail-list');
  function esc(s) { return String(s).replace(/[&<>"]/g, function (ch) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]; }); }
  var mailBtn = document.getElementById('pick-mail');
  if (mailBtn) mailBtn.addEventListener('click', function () {
    mailBtn.disabled = true;
    fetch('/api/request', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ets: picked(), mode: table.dataset.mailMode || ''})})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        list.innerHTML = d.html;
        list.querySelectorAll('.mail').forEach(syncChips);
        if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
      })
      .catch(function () { alertBanner('Не удалось составить письмо: программа не отвечает.'); })
      .then(function () { mailBtn.disabled = false; });
  });
  function alertBanner(t) { banner.hidden = false; banner.textContent = t; }
  var dc = document.getElementById('dlg-close');
  if (dc) dc.addEventListener('click', function () { if (dlg.close) dlg.close(); else dlg.removeAttribute('open'); });
}

// Фильтры под заголовками обычной таблицы (раздел «Где застряли»): поиск по части текста, «(пусто)» — пустые.
// Значения попадают в адрес страницы, в ссылки этой же страницы (АСЦ, пороги, выгрузка в Excel) и в форму порогов.
function initColFilter(id) {
  var table = document.getElementById(id);
  if (!table) return;
  var tbody = table.tBodies[0];
  var inputs = Array.prototype.slice.call(table.querySelectorAll('input.cf'));
  var shown = document.getElementById('cf-shown'), of = document.getElementById('cf-of'), ncm = document.getElementById('cf-cm');
  var none = tbody.querySelector('tr.cf-none');
  var rows = Array.prototype.filter.call(tbody.rows, function (r) { return r.cells.length > 1; });
  var th = table.tHead;  // строка фильтров закреплена сразу под заголовками
  if (th && th.rows[1]) Array.prototype.forEach.call(th.rows[1].cells, function (c) { c.style.top = th.rows[0].offsetHeight + 'px'; });
  function norm(s) { return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  function cellText(c) { return c.hasAttribute('data-f') ? c.getAttribute('data-f') : norm(c.textContent.replace(/^—$/, '')); }
  function withCf(u) {
    for (var k of Array.from(u.searchParams.keys())) { if (/^cf\d+$/.test(k)) u.searchParams.delete(k); }
    inputs.forEach(function (inp) { if (inp.value.trim()) u.searchParams.set('cf' + inp.dataset.idx, inp.value.trim()); });
    return u.pathname + u.search;
  }
  function apply() {
    var f = [];
    inputs.forEach(function (inp) { var v = norm(inp.value); if (v) f.push([+inp.dataset.idx, v]); });
    var n = 0, c = 0;
    rows.forEach(function (r) {
      var ok = f.every(function (p) { var t = cellText(r.cells[p[0]]); return p[1] === '(пусто)' ? t === '' : t.indexOf(p[1]) >= 0; });
      r.style.display = ok ? '' : 'none';
      if (ok) { n++; if (r.classList.contains('has-cm')) c++; }
    });
    if (shown) shown.textContent = n;
    if (of) of.hidden = !f.length;
    if (ncm) ncm.textContent = c;
    if (none) none.hidden = !(rows.length && n === 0);
    try { history.replaceState(null, '', withCf(new URL(location.href))); } catch (e) {}
    document.querySelectorAll('input[name=back]').forEach(function (b) { b.value = location.pathname + location.search; });
  }
  var timer = null;
  inputs.forEach(function (inp) {
    inp.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(apply, 150); });
    inp.addEventListener('keydown', function (e) { if (e.key === 'Escape') { inp.value = ''; apply(); } });
  });
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[href]');
    if (!a || a.target) return;
    var u = new URL(a.href, location.href);
    if (u.origin === location.origin && u.pathname === location.pathname) a.href = withCf(u);
  }, true);
  if (inputs.some(function (i) { return i.value.trim(); })) apply();
  return {count: function () {  // пересчитать «с комментарием» среди видимых строк
    if (ncm) ncm.textContent = rows.filter(function (r) { return r.style.display !== 'none' && r.classList.contains('has-cm'); }).length;
  }};
}

// «Где застряли»: фильтры под заголовками + комментарий прямо в таблице «Ремонты».
function initStuckTable(author) {
  var cf = initColFilter('stuck-tbl');
  var table = document.getElementById('stuck-tbl');
  if (!table) return;
  initCommentCells(table.tBodies[0], author, document.getElementById('save-banner'), {
    saved: function (a, d) {
      var td = a.closest('td'), u = d.updated_at || '';
      var dt = d.text && u ? u.slice(8, 10) + '.' + u.slice(5, 7) + '.' + u.slice(0, 4) : '';
      td.setAttribute('data-f', (d.text + ' ' + dt).replace(/\s+/g, ' ').trim().toLowerCase());  // для фильтра по столбцу
      var s = td.querySelector('.cm-date');
      if (s) s.textContent = dt;
    },
    changed: function () { if (cf) cf.count(); },
  });
}

// Письмо в АСЦ: копирование, выбор адресатов, «Открыть в почте»
function mailAddrs(v) { return (v || '').split(/[;,\s]+/).map(function (x) { return x.trim(); }).filter(Boolean); }
function syncChips(box) {
  var to = mailAddrs(box.querySelector('.mail-to') && box.querySelector('.mail-to').value).map(function (x) { return x.toLowerCase(); });
  box.querySelectorAll('.chip-mail').forEach(function (c) { c.classList.toggle('on', to.indexOf(c.dataset.email.toLowerCase()) >= 0); });
}
document.querySelectorAll('.mail').forEach(syncChips);
function mailEdited(box) {  // тема или текст письма изменены вручную — показать «Вернуть исходный текст»
  var r = box.querySelector('[data-reset]'), s = box.querySelector('.mail-subj'), t = box.querySelector('.mail-body');
  if (r) r.hidden = s.value === s.defaultValue && t.value === t.defaultValue;
}
document.addEventListener('input', function (e) {
  if (e.target.matches('.mail-to')) syncChips(e.target.closest('.mail'));
  if (e.target.matches('.mail-subj, .mail-body')) mailEdited(e.target.closest('.mail'));
});
document.addEventListener('click', function (e) {
  var chip = e.target.closest('.chip-mail');
  if (chip) {
    var box = chip.closest('.mail'), inp = box.querySelector('.mail-to'), sep = box.dataset.sep || ';';
    var list = mailAddrs(inp.value), em = chip.dataset.email, i = list.map(function (x) { return x.toLowerCase(); }).indexOf(em.toLowerCase());
    if (i >= 0) list.splice(i, 1); else list.push(em);
    inp.value = list.join(sep + ' ');
    syncChips(box);
    return;
  }
  var rs = e.target.closest('[data-reset]');
  if (rs) {
    var bx0 = rs.closest('.mail'), s0 = bx0.querySelector('.mail-subj'), t0 = bx0.querySelector('.mail-body');
    s0.value = s0.defaultValue; t0.value = t0.defaultValue; mailEdited(bx0);
    var m0 = bx0.querySelector('.copy-msg'); if (m0) m0.textContent = 'Вернул исходный текст';
    return;
  }
  var mt = e.target.closest('[data-mailto]');
  if (mt) {
    var bx = mt.closest('.mail'), sp = bx.dataset.sep || ';';
    var to = mailAddrs(bx.querySelector('.mail-to').value).join(sp);
    var cc = mailAddrs(bx.querySelector('.mail-cc').value).join(sp);
    var q = [];
    if (cc) q.push('cc=' + cc);
    q.push('subject=' + encodeURIComponent(bx.querySelector('.mail-subj').value));
    q.push('body=' + encodeURIComponent(bx.querySelector('.mail-body').value.replace(/\r?\n/g, '\r\n')));
    var link = 'mailto:' + to + '?' + q.join('&');
    mt.href = mt.dataset.web ? mt.dataset.web + encodeURIComponent(link) : link;
    return;
  }
  var b = e.target.closest('[data-copy]');
  if (!b) return;
  var box2 = b.closest('.mail');
  var sel = {subj: '.mail-subj', body: '.mail-body', to: '.mail-to'}[b.dataset.copy];
  var el = box2.querySelector(sel);
  var msg = box2.querySelector('.copy-msg');
  var label = {subj: 'Тема скопирована', body: 'Текст скопирован', to: 'Адреса скопированы'}[b.dataset.copy];
  function done(ok) { if (msg) msg.textContent = ok ? label : 'Выделите и нажмите Ctrl+C'; }
  el.focus(); el.select();
  if (navigator.clipboard && window.isSecureContext !== false) {
    navigator.clipboard.writeText(el.value).then(function () { done(true); }, function () { var ok = false; try { ok = document.execCommand('copy'); } catch (x) {} done(ok); });
  } else { var ok2 = false; try { ok2 = document.execCommand('copy'); } catch (x) {} done(ok2); }
});
