/* /weight — the bodyweight log: an import box on top, every reading below.

   The page has ONE action. A connected scale records each morning to its vendor's
   app, the user exports that app's spreadsheet every so often, and this hands the
   file to the server. Nothing else here writes — the rows are read-only, because a
   reading is a measurement and correcting one belongs at the scale, not in a
   text field that would disagree with it.

   Upload-then-reload, the shape the whole page used to use for saving: the rows are
   server-rendered and each one's delta depends on its neighbour, so patching an
   import's worth of new rows in place would mean a second copy of that arithmetic
   in JS. The one thing that must NOT be cut off by the reload is the confetti when
   the import brings in a new all-time low. */
(function () {
  'use strict';

  var box = document.querySelector('[data-import]');
  if (!box) return;

  var BASE = (function () {
    // Same trick the chat panel uses: derive the mount path from this script's own
    // src, so the file needs no server-rendered constant.
    var el = document.querySelector('script[src*="/static/weight.js"]');
    var src = el ? el.getAttribute('src') : '';
    return src.replace(/\/static\/weight\.js.*$/, '');
  })();

  var pick = box.querySelector('[data-import-pick]');
  var file = box.querySelector('[data-import-file]');
  var msg = box.querySelector('[data-import-msg]');
  var err = box.querySelector('[data-import-err]');

  // A burst has ~2s to play. Reloading under it would cut it off, so a new low holds
  // the reload — but only if a burst actually STARTED. burst() declines under reduced
  // motion, and holding the page for an animation nobody is going to see is a dead
  // 2.4 seconds inflicted on precisely the person who asked for less motion.
  function finish(celebrate) {
    var thrown = celebrate && window.Confetti && window.Confetti.burst(box);
    setTimeout(function () { location.reload(); }, thrown ? 2400 : 0);
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  pick.addEventListener('click', function () { file.click(); });

  file.addEventListener('change', function () {
    var f = file.files && file.files[0];
    if (!f) return;
    err.hidden = true;
    msg.hidden = false;
    msg.textContent = 'Reading ' + f.name + '…';
    pick.disabled = true;

    // The file goes up as the raw request body rather than as multipart — one upload
    // in the whole app doesn't justify a form-parser dependency on the server.
    fetch(BASE + '/weight/import', { method: 'POST', body: f })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok || (j && j.error)) throw new Error((j && j.error) || 'Could not import that file.');
          return j;
        });
      })
      .then(function (j) {
        if (!j.imported) {
          // Not an error, and deliberately not styled as one: re-uploading an
          // overlapping export is the normal way this gets used.
          msg.textContent = 'Nothing new — all ' + plural(j.readings, 'reading') +
                            ' in that file were already logged.';
          pick.disabled = false;
          file.value = '';
          return;
        }
        msg.textContent = 'Imported ' + plural(j.imported, 'reading') +
                          (j.skipped ? ' (' + j.skipped + ' already logged)' : '') + '…';
        finish(j.new_low);
      })
      .catch(function (ex) {
        msg.hidden = true;
        err.textContent = ex.message;
        err.hidden = false;
        pick.disabled = false;
        file.value = '';
      });
  });
})();
