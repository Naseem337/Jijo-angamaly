(function () {
  // Toast from ?toast=...  ("!" prefix = error); the message is put in as text, never HTML.
  var t = document.getElementById('toast');
  if (t && t.dataset.msg) {
    var m = t.dataset.msg, err = m.charAt(0) === '!';
    t.textContent = err ? m.slice(1) : m;
    t.className = 'toast show' + (err ? ' err' : '');
    setTimeout(function () { t.className = 'toast' + (err ? ' err' : ''); }, 3500);
    if (history.replaceState) {
      var u = new URL(location.href); u.searchParams.delete('toast'); history.replaceState(null, '', u);
    }
  }
  // Clickable table rows.
  document.addEventListener('click', function (e) {
    var row = e.target.closest('tr[data-href]');
    if (row && !e.target.closest('a,button,input,select,form')) location.href = row.dataset.href;
  });
  // Confirm dialogs on risky buttons.
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-confirm]');
    if (b && !confirm(b.dataset.confirm)) e.preventDefault();
  });
})();

// Chip inputs (staff numbers, highlights): chips + an Add button, kept in a hidden textarea, one value per line.
(function () {
  function phone(raw) {
    var s = raw.trim(), plus = s.charAt(0) === '+', d = s.replace(/\D/g, '');
    if (plus) return d.length >= 8 && d.length <= 15 ? '+' + d : null;
    if (d.length === 10) return '+91' + d;
    if (d.length === 12 && d.indexOf('91') === 0) return '+' + d;
    if (d.length === 11 && d.charAt(0) === '0') return '+91' + d.slice(1);
    return null;
  }
  document.querySelectorAll('[data-chips]').forEach(function (box) {
    var store = box.querySelector('textarea'), list = box.querySelector('.chip-list'),
        input = box.querySelector('input'), add = box.querySelector('button'),
        kind = box.dataset.kind, max = +box.dataset.max || 20;
    var items = store.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean);
    function sync() {
      store.value = items.join('\n');
      list.textContent = '';
      items.forEach(function (v, i) {
        var c = document.createElement('span'); c.className = 'chip';
        c.appendChild(document.createTextNode(kind === 'phone' ? v.replace(/^(\+\d{2})(\d{5})(\d+)$/, '$1 $2 $3') : v));
        var x = document.createElement('button'); x.type = 'button'; x.setAttribute('aria-label', 'Remove'); x.textContent = '×';
        x.onclick = function () { items.splice(i, 1); sync(); };
        c.appendChild(x); list.appendChild(c);
      });
      list.classList.toggle('empty', !items.length);
    }
    function addOne() {
      var raw = input.value.trim(); if (!raw) return;
      var v = kind === 'phone' ? phone(raw) : raw;
      if (!v) { input.classList.add('bad'); setTimeout(function () { input.classList.remove('bad'); }, 600); return; }
      if (items.length >= max) return;
      if (items.indexOf(v) < 0) items.push(v);
      input.value = ''; sync(); input.focus();
    }
    add.onclick = addOne;
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); addOne(); } });
    box.closest('form').addEventListener('submit', addOne);  // a number typed but not added is still saved
    sync();
  });
  // Live character counter for a textarea with data-count.
  document.querySelectorAll('textarea[data-count]').forEach(function (ta) {
    var out = document.getElementById(ta.dataset.count);
    function upd() { if (out) out.textContent = ta.value.length + ' / ' + ta.maxLength; }
    ta.addEventListener('input', upd); upd();
  });
})();

// Sidebar: property switcher (remembered in a cookie) and the phone menu.
var closeDrawer = function () {};
(function () {
  var scope = document.getElementById('scope');
  if (scope) scope.addEventListener('change', function () {
    document.cookie = 'rp=' + encodeURIComponent(scope.value) + '; path=/resort/admin; max-age=31536000; samesite=lax';
    var u = new URL(location.href); u.searchParams.delete('property_id'); u.searchParams.delete('room_type_id'); location.href = u;
  });
  var sb = document.getElementById('sidebar'), scrim = document.getElementById('scrim'), mb = document.getElementById('menu-btn');
  function menu(open) { if (sb) sb.classList.toggle('open', open); if (scrim) scrim.classList.toggle('show', open); }
  if (mb) mb.addEventListener('click', function () { menu(!sb.classList.contains('open')); });
  if (scrim) scrim.addEventListener('click', function () { menu(false); });
  // "/" jumps to the search box, like in Frappe.
  document.addEventListener('keydown', function (e) {
    var t = e.target, typing = t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
    if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey) {
      var s = document.getElementById('global-search'); if (s) { e.preventDefault(); s.focus(); s.select(); }
    }
    if (e.key === 'Escape') { menu(false); closeDrawer(); }
  });
})();

// Copy buttons, dialogs and quick amounts.
(function () {
  function say(msg) {
    var t = document.getElementById('toast'); if (!t) return;
    t.textContent = msg; t.className = 'toast show'; setTimeout(function () { t.className = 'toast'; }, 1800);
  }
  document.addEventListener('click', function (e) {
    var c = e.target.closest('[data-copy]');
    if (c) {
      var text = c.dataset.copy;
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { say('Copied'); });
      else { var a = document.createElement('textarea'); a.value = text; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); say('Copied'); }
    }
    var o = e.target.closest('[data-open]');
    if (o) { var d = document.getElementById(o.dataset.open); if (d && d.showModal) d.showModal(); }
    if (e.target.closest('[data-close]')) { var m = e.target.closest('dialog'); if (m) m.close(); }
    if (e.target.tagName === 'DIALOG') e.target.close();  // a click on the backdrop
    var q = e.target.closest('[data-amount]');
    if (q) { var f = document.getElementById('pay-amount'); if (f) f.value = q.dataset.amount; }
  });
})();

// Calendar: click a day and the side panel loads that day's bookings (HTML from the server).
(function () {
  var grid = document.getElementById('cal-grid'); if (!grid) return;
  var layout = grid.closest('.cal-layout'), body = document.getElementById('drawer-body'), current = null;
  var dayUrl = location.pathname.replace(/\/calendar.*$/, '') + '/calendar/day';
  closeDrawer = function () { layout.classList.remove('open'); if (current) current.classList.remove('sel'); current = null; };
  grid.addEventListener('click', function (e) {
    var cell = e.target.closest('.cal-day'); if (!cell) return;
    e.preventDefault();
    if (current) current.classList.remove('sel');
    current = cell; cell.classList.add('sel'); layout.classList.add('open');
    body.textContent = 'Loading...';
    var q = new URLSearchParams({ property_id: grid.dataset.prop, room_type_id: grid.dataset.room, date: cell.dataset.date });
    fetch(dayUrl + '?' + q).then(function (r) { return r.text(); }).then(function (html) { body.innerHTML = html; })
      .catch(function () { body.textContent = 'Could not load this day.'; });
  });
  document.addEventListener('click', function (e) { if (e.target.closest('[data-drawer-close]')) closeDrawer(); });
})();


// Dialog helpers: ?open=checkin opens a dialog, quick-fill buttons, "reference needed" per method, quick extras.
(function () {
  var want = new URLSearchParams(location.search).get('open');
  if (want) { var d = document.getElementById(want + '-dialog'); if (d && d.showModal) d.showModal(); }
  document.addEventListener('click', function (e) {
    var f = e.target.closest('[data-fill]');
    if (f) { var inp = f.closest('form').querySelector('[name="' + f.dataset.fill + '"]'); if (inp) inp.value = f.dataset.amount; }
    var x = e.target.closest('[data-extra]');
    if (x) {
      var form = document.getElementById('extra-form'); if (!form) return;
      var parts = x.dataset.extra.split('|');
      form.querySelector('[name="category"]').value = parts[0];
      var desc = form.querySelector('[name="description"]'); desc.value = parts[1];
      form.querySelector('[name="amount"]').focus();
    }
  });
  document.querySelectorAll('select[data-needs]').forEach(function (sel) {
    var need = sel.dataset.needs.split(',');
    var ref = sel.closest('form').querySelector('[data-ref]');
    function upd() { if (ref) ref.required = need.indexOf(sel.value) >= 0; }
    sel.addEventListener('change', upd); upd();
  });
})();
