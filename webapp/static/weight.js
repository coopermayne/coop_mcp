/* /weight — the bodyweight log: a form on top, every reading below, each one
   correctable and deletable.

   Fetch-then-reload throughout, the /food Targets popover's shape. The page is
   server-rendered (the rows need ids, and each row's delta depends on its
   neighbour), so patching one row in place would mean recomputing the deltas
   around it in JS — two implementations of the same arithmetic. A reload is
   honest and this is not a page you hammer.

   The one thing that must NOT be interrupted by that reload is the confetti: when
   a new all-time low actually throws one, the reload waits for it. That hold is
   the whole reason the weigh-in stopped living behind the /trainer card's Finish
   redirect, so it would be a poor joke to reintroduce it here. */
(function () {
  'use strict';

  var box = document.querySelector('[data-weighin]');
  if (!box) return;

  var BASE = (function () {
    // Same trick the chat panel uses: derive the mount path from this script's own
    // src, so the file needs no server-rendered constant.
    var el = document.querySelector('script[src*="/static/weight.js"]');
    var src = el ? el.getAttribute('src') : '';
    return src.replace(/\/static\/weight\.js.*$/, '');
  })();

  var input = box.querySelector('[data-weighin-input]');
  var dateEl = box.querySelector('[data-weighin-date]');
  var save = box.querySelector('[data-weighin-save]');
  var err = box.querySelector('[data-weighin-err]');
  var list = document.querySelector('[data-wlog]');
  var listErr = document.querySelector('[data-wlog-err]');

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || (j && j.error)) throw new Error((j && j.error) || 'Could not save.');
        return j;
      });
    });
  }

  // A burst has ~2s to play. Reloading under it would cut it off, so a new low holds
  // the reload — but only if a burst actually STARTED. burst() declines under reduced
  // motion, and holding the page for an animation nobody is going to see is a dead
  // 2.4 seconds inflicted on precisely the person who asked for less motion.
  function finish(celebrate) {
    var thrown = celebrate && window.Confetti && window.Confetti.burst(box);
    setTimeout(function () { location.reload(); }, thrown ? 2400 : 0);
  }

  /* ---------- the entry form ---------------------------------------------- */

  // Typo guard: a value outside a plausible range turns the field yellow. It never
  // BLOCKS — a real reading can be anything, and capture must not argue. It earns its
  // place on dropped leading digits, which is the failure mode this log has a history
  // of: the day still reads correctly afterwards (latest reading wins) while the bad
  // row quietly owns MIN(weight_lbs) and disables every "lowest ever".
  var LO = 160, HI = 230;
  function paint() {
    var v = input.value.trim(), n = parseFloat(v);
    var bad = v !== '' && !(n >= LO && n <= HI);
    input.classList.toggle('border-yellow-400', bad);
    input.classList.toggle('bg-yellow-50', bad);
    input.classList.toggle('border-gray-200', !bad);
    save.disabled = !(n > 0);
  }
  input.addEventListener('input', function () { err.hidden = true; paint(); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); save.click(); }
  });
  dateEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); save.click(); }
  });
  paint();

  save.addEventListener('click', function () {
    var v = parseFloat(input.value);
    if (!(v > 0)) { input.focus(); return; }
    save.disabled = true;
    err.hidden = true;
    post(BASE + '/weight', { weight_lbs: v, weigh_date: dateEl.value || null })
      .then(function (j) { finish(j.new_low); })
      .catch(function (ex) {
        err.textContent = ex.message; err.hidden = false; save.disabled = false;
      });
  });

  /* ---------- correcting / deleting a row --------------------------------- */

  if (!list) return;

  function fail(msg) { listErr.textContent = msg; listErr.hidden = false; }

  list.addEventListener('click', function (e) {
    var row = e.target.closest('[data-wrow]');
    if (!row) return;
    var id = row.dataset.wid;
    var val = row.querySelector('[data-wval]');
    var inp = row.querySelector('[data-winput]');

    if (e.target.closest('[data-wedit]')) {
      listErr.hidden = true;
      if (inp.hidden) {                 // first tap opens the field
        val.hidden = true; inp.hidden = false; inp.focus(); inp.select();
      } else {                          // second tap commits
        var v = parseFloat(inp.value);
        if (!(v > 0)) { inp.focus(); return; }
        post(BASE + '/weight/' + id, { weight_lbs: v })
          .then(function () { location.reload(); })
          .catch(function (ex) { fail(ex.message); });
      }
      return;
    }

    if (e.target.closest('[data-wdel]')) {
      listErr.hidden = true;
      var label = val.textContent.trim();
      var when = row.querySelector('span').textContent.trim();
      if (!window.confirm('Delete the ' + label + ' reading from ' + when + '?')) return;
      post(BASE + '/weight/' + id + '/delete')
        .then(function () { location.reload(); })
        .catch(function (ex) { fail(ex.message); });
    }
  });

  // Enter commits an open field, Escape abandons it.
  list.addEventListener('keydown', function (e) {
    if (!e.target.matches('[data-winput]')) return;
    var row = e.target.closest('[data-wrow]');
    if (e.key === 'Enter') { e.preventDefault(); row.querySelector('[data-wedit]').click(); }
    else if (e.key === 'Escape') {
      e.preventDefault();
      row.querySelector('[data-winput]').hidden = true;
      row.querySelector('[data-wval]').hidden = false;
    }
  });
})();
