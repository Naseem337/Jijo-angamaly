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
