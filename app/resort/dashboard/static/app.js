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
