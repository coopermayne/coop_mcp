/*
 * Trainer plan card. Renders the active workout plan into #plan-root and handles the
 * write paths the page owns directly: tap-to-complete a planned set, edit a logged
 * ('done') set to fix a data-entry error, drop or replace an exercise (the per-exercise
 * "..." menu), and finish the session. A per-exercise "i" opens saved technique notes +
 * a YouTube search link. Building/swapping the routine happens in the chat, which calls
 * window.TrainerPlan.refresh() after each write (see _trainer_chat_panel).
 *
 * One render path: the server bootstraps the initial plan as JSON; every update
 * (tap, edit, finish, or chat-driven refresh) re-renders from a fresh plan object.
 */
(function () {
  var root = document.getElementById('plan-root');
  if (!root) return;
  var base = root.dataset.base || '';
  var editingSetId = null; // only one inline set editor open at a time
  var openPanel = null;    // {eid, kind:'info'|'menu'} — at most one info/menu panel open
  var currentPlan = null;  // last rendered plan (Finish reads its bodyweight/progress)
  var pendingBodyweight = ''; // weigh-in typed but not yet saved — submitted only on Finish,
                              // kept here so set-by-set re-renders don't wipe what you typed
  var reordering = false;  // reorder mode: arrows to the left of each exercise, header "Done"
  var reorderList = null;  // working copy of the visible exercises while reordering

  // A programmatic focus() pops the mobile soft keyboard, which covers the weight
  // steppers and the Easy/Med/Hard buttons — the very controls that let you log a
  // set without typing. So on a touch-primary device we skip auto-focusing the
  // editor's inputs; on a mouse/desktop there's no keyboard to get in the way, so
  // focusing still helps (Enter-to-submit, caret ready for typing).
  var isTouch = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  function maybeFocus(inp) { if (!isTouch) inp.focus(); }

  function num(x) {
    if (x === null || x === undefined || x === '') return '';
    return (+x).toString();
  }

  // Difficulty is entered as Easy/Med/Hard but stored as RPE (1-10), the server's field.
  // These are the canonical mappings; rpeToLabel buckets any RPE (e.g. one set via chat)
  // to its nearest word for display + prefilling the buttons.
  var DIFFICULTY = [{ key: 'Easy', rpe: 5 }, { key: 'Med', rpe: 7 }, { key: 'Hard', rpe: 9 }];
  function rpeToLabel(rpe) {
    if (rpe === null || rpe === undefined) return null;
    var best = null, bestDist = Infinity;
    DIFFICULTY.forEach(function (d) {
      var dist = Math.abs(d.rpe - rpe);
      if (dist < bestDist) { bestDist = dist; best = d.key; }
    });
    return best;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // Compact duration: '45m', '1h05m', '30s' — mirrors app.py's dur_label.
  function durLabel(sec) {
    sec = Math.round(sec);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + 'h' + String(m).padStart(2, '0') + 'm';
    if (m) return m + 'm';
    return s + 's';
  }

  // Label for a set: weight × reps for lifts, distance · time for cardio (+ @rpe),
  // using actuals when done, targets when not. Cardio metrics are actual-only (no
  // target columns), so they show whenever present. Mirrors app.py's set_label.
  function setText(s, done) {
    var w = done ? s.weight_lbs : s.target_weight_lbs;
    var r = done ? s.reps : s.target_reps;
    var dur = s.duration_seconds, dist = s.distance_miles;
    var parts;
    if (w != null && r != null) parts = num(w) + ' × ' + r;
    else if (r != null) parts = r + ' rep' + (r === 1 ? '' : 's');
    else if (w != null) parts = num(w) + ' lb';
    else if (dist != null || dur != null) {
      var cardio = [];
      if (dist != null) cardio.push(num(dist) + ' mi');
      if (dur != null) cardio.push(durLabel(dur));
      parts = cardio.join(' · ');
    }
    else parts = '—';
    if (done && s.rpe != null) parts += '  · ' + rpeToLabel(s.rpe);
    return parts;
  }

  async function postJSON(url, body) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    var data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, data: data };
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  function liveSets(ex) {
    return ex.sets.filter(function (s) { return s.status !== 'skipped'; });
  }

  function render(plan) {
    root.innerHTML = '';
    editingSetId = null;
    openPanel = null;
    currentPlan = plan;
    if (!plan || !plan.active) { reordering = false; reorderList = null; renderEmpty(plan); return; }

    var pr = plan.progress || { done: 0, total: 0 };
    var visible = plan.exercises.filter(function (ex) { return liveSets(ex).length; });

    // Header: focus + progress, with a reorder toggle on the right (Done while reordering).
    var head = el('div', 'flex items-end justify-between mb-4');
    var left = el('div');
    left.appendChild(el('p', 'text-[10px] uppercase tracking-widest text-gray-400 mb-1',
      'Next Plan' + (plan.focus ? ' · ' + plan.focus : '')));
    left.appendChild(el('p', 'text-lg font-semibold tracking-tight',
      pr.done + ' / ' + pr.total + ' sets'));
    head.appendChild(left);
    if (reordering) {
      head.appendChild(reorderDoneBtn());
    } else {
      // Reorder toggle (when there's more than one exercise to sort) sits left of a
      // plan-level "..." menu that tucks away the destructive "Delete plan" action.
      var ctrls = el('div', 'flex items-center gap-1 shrink-0');
      if (visible.length > 1) ctrls.appendChild(reorderToggleBtn());
      ctrls.appendChild(planMenuBtn());
      head.appendChild(ctrls);
    }
    root.appendChild(head);

    // Reorder mode: just the exercises with ↑/↓ arrows; weigh-in & Finish are hidden.
    if (reordering) {
      reorderList.forEach(function (ex, i) { root.appendChild(renderReorderRow(ex, i)); });
      root.appendChild(el('p', 'text-[11px] text-gray-400 mt-3 mb-1',
        'Reorder with the arrows, then tap Done.'));
      return;
    }

    // Exercises (fully swapped-out ones are hidden).
    visible.forEach(function (ex) { root.appendChild(renderExercise(ex)); });

    // Weigh-in box — its own row under the sets, encouraging (not requiring) a
    // bodyweight while you're at the gym.
    root.appendChild(renderBodyweight(plan, pr));

    // The big full-width Finish ("Done") button at the bottom.
    root.appendChild(renderFinish());
  }

  // ── Reorder mode ────────────────────────────────────────────────────────────

  function reorderToggleBtn() {
    var b = el('button', 'shrink-0 w-8 h-8 flex items-center justify-center rounded-[4px] ' +
      'text-gray-400 hover:text-black hover:bg-gray-100 transition-colors', null);
    b.type = 'button';
    b.setAttribute('aria-label', 'Reorder exercises');
    b.title = 'Reorder exercises';
    b.appendChild(reorderIcon());
    b.addEventListener('click', function () {
      reordering = true;
      reorderList = (currentPlan.exercises || []).filter(function (ex) { return liveSets(ex).length; });
      render(currentPlan);
    });
    return b;
  }

  // The plan-level "..." menu in the header. Tucks the destructive "Delete plan" out of
  // the way (one tap to reveal, a confirmation modal to commit) so it can't be hit by
  // accident the way an always-visible button could.
  function planMenuBtn() {
    var wrap = el('div', 'relative shrink-0');
    var b = el('button', 'w-8 h-8 flex items-center justify-center rounded-[4px] ' +
      'text-gray-400 hover:text-black hover:bg-gray-100 transition-colors', null);
    b.type = 'button';
    b.setAttribute('aria-label', 'Plan options');
    b.title = 'Plan options';
    b.appendChild(el('span', 'text-lg leading-none', '⋯'));

    var menu = el('div', 'hidden absolute right-0 top-9 z-20 min-w-[10rem] bg-white ' +
      'border border-gray-200 rounded-[4px] shadow-lg py-1');
    var del = el('button', 'w-full text-left px-3 py-2 text-sm text-red-500 ' +
      'hover:bg-red-50 transition-colors', 'Delete plan');
    del.type = 'button';
    del.addEventListener('click', function () { menu.classList.add('hidden'); confirmDiscard(); });
    menu.appendChild(del);

    function closeMenu() {
      menu.classList.add('hidden');
      document.removeEventListener('click', closeMenu);
    }
    b.addEventListener('click', function (e) {
      e.stopPropagation();
      if (menu.classList.contains('hidden')) {
        menu.classList.remove('hidden');
        // Defer so this same click doesn't immediately close it.
        setTimeout(function () { document.addEventListener('click', closeMenu); }, 0);
      } else {
        closeMenu();
      }
    });
    wrap.appendChild(b);
    wrap.appendChild(menu);
    return wrap;
  }

  // A simple centered confirmation modal (overlay + card). Returns nothing; calls
  // opts.onConfirm() when the user commits. Esc or a click on the backdrop cancels.
  function confirmModal(opts) {
    var overlay = el('div', 'fixed inset-0 z-50 flex items-center justify-center px-4 bg-black/40');
    var card = el('div', 'bg-white rounded-[6px] shadow-xl max-w-sm w-full p-6');
    card.appendChild(el('p', 'text-base font-semibold mb-2', opts.title));
    card.appendChild(el('p', 'text-sm text-gray-500 leading-relaxed mb-6', opts.body));

    var rowBtns = el('div', 'flex justify-end gap-2');
    var cancel = el('button', 'px-4 h-10 rounded-[4px] text-sm text-gray-500 ' +
      'hover:text-black hover:bg-gray-100 transition-colors', 'Cancel');
    cancel.type = 'button';
    var ok = el('button', 'px-4 h-10 rounded-[4px] bg-red-500 text-white text-sm ' +
      'font-medium hover:bg-red-600 transition-colors', opts.confirmText || 'Delete');
    ok.type = 'button';

    function close() {
      document.removeEventListener('keydown', onKey);
      overlay.remove();
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    cancel.addEventListener('click', close);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    ok.addEventListener('click', function () { close(); opts.onConfirm(); });
    document.addEventListener('keydown', onKey);

    rowBtns.appendChild(cancel);
    rowBtns.appendChild(ok);
    card.appendChild(rowBtns);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
  }

  function confirmDiscard() {
    confirmModal({
      title: 'Delete this plan?',
      body: 'This clears the whole workout plan, including any sets you’ve already ' +
        'logged. It won’t be saved to your training history and can’t be undone.',
      confirmText: 'Delete plan',
      onConfirm: discardPlan,
    });
  }

  async function discardPlan() {
    var r = await postJSON(base + '/trainer/plan/discard', {});
    if (r.ok && r.data && !r.data.error) render(r.data);
    else refresh();
  }

  function reorderDoneBtn() {
    var b = el('button', 'shrink-0 px-4 h-8 rounded-[4px] bg-black text-white text-xs ' +
      'uppercase tracking-widest font-semibold hover:bg-gray-800 transition-colors', 'Done');
    b.type = 'button';
    b.addEventListener('click', submitReorder);
    return b;
  }

  function renderReorderRow(ex, i) {
    var box = el('div', 'border border-gray-200 rounded-[4px] pl-2 pr-4 py-3 mb-2 flex items-center gap-3');
    var arrows = el('div', 'flex flex-col shrink-0');
    var up = arrowBtn('up', i === 0);
    up.addEventListener('click', function () { moveReorder(i, -1); });
    var down = arrowBtn('down', i === reorderList.length - 1);
    down.addEventListener('click', function () { moveReorder(i, 1); });
    arrows.appendChild(up);
    arrows.appendChild(down);
    box.appendChild(arrows);
    box.appendChild(el('p', 'text-sm font-medium', ex.name));
    return box;
  }

  function moveReorder(i, dir) {
    var j = i + dir;
    if (j < 0 || j >= reorderList.length) return;
    var tmp = reorderList[i];
    reorderList[i] = reorderList[j];
    reorderList[j] = tmp;
    render(currentPlan);
  }

  async function submitReorder() {
    var order = (reorderList || []).map(function (ex) { return ex.exercise_id; });
    reordering = false;
    reorderList = null;
    var r = await postJSON(base + '/trainer/reorder', { order: order });
    if (r.ok && r.data && !r.data.error) render(r.data);
    else refresh();
  }

  // A small up/down arrow for a reorder row (disabled at the ends).
  function arrowBtn(dir, disabled) {
    var b = el('button', 'w-7 h-6 flex items-center justify-center rounded text-gray-400 ' +
      'hover:text-black hover:bg-gray-100 transition-colors ' +
      'disabled:opacity-20 disabled:hover:bg-transparent disabled:hover:text-gray-400', null);
    b.type = 'button';
    b.disabled = !!disabled;
    b.setAttribute('aria-label', dir === 'up' ? 'Move up' : 'Move down');
    b.appendChild(chevron(dir));
    return b;
  }

  function svgEl(view, cls) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', view); s.setAttribute('fill', 'none');
    s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '2');
    s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
    s.setAttribute('class', cls);
    return s;
  }

  function chevron(dir) {
    var s = svgEl('0 0 24 24', 'w-4 h-4');
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', dir === 'up' ? 'M18 15l-6-6-6 6' : 'M6 9l6 6 6-6');
    s.appendChild(p);
    return s;
  }

  // The reorder toggle's glyph: two opposed arrows (⇅).
  function reorderIcon() {
    var s = svgEl('0 0 24 24', 'w-5 h-5');
    [['M8 4v16', 'M4 8l4-4 4 4'], ['M16 20V4', 'M20 16l-4 4-4-4']].forEach(function (d) {
      d.forEach(function (path) {
        var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        p.setAttribute('d', path); s.appendChild(p);
      });
    });
    return s;
  }

  // The big full-width Finish button — bold white "Done" on yellow, anchoring the card.
  function renderFinish() {
    var b = el('button', 'w-full py-5 mt-1 rounded-[4px] bg-yellow-400 hover:bg-yellow-500 ' +
      'text-white font-bold text-lg uppercase tracking-widest transition-colors', 'Done');
    b.type = 'button';
    b.addEventListener('click', onFinish);
    return b;
  }

  // A full-width box mirroring the exercise cards: a label + a number entry for
  // today's bodyweight. The entry starts EMPTY and is never submitted on its own —
  // there's no log/update button; Finish reads it and saves it (so a weigh-in commits
  // with the workout, not before). The input goes yellow as a typo guard whenever a
  // value is entered that isn't a plausible reading (between 160 and 230 lbs).
  function renderBodyweight(plan, pr) {
    var box = el('div', 'border border-gray-200 rounded-[4px] px-5 sm:px-6 py-4 mb-3');
    box.dataset.weighIn = '1';
    box.appendChild(el('p', 'text-sm font-medium mb-3', 'Bodyweight'));

    var INPUT_BASE = 'w-full border rounded-[4px] px-3 py-2 text-base focus:outline-none transition-colors';
    var inp = el('input', INPUT_BASE);
    inp.type = 'number'; inp.inputMode = 'decimal'; inp.step = 'any'; inp.min = '0';
    inp.placeholder = 'lbs';
    inp.value = pendingBodyweight; // empty by default; restored across re-renders

    // Yellow when a value is present but outside a plausible range — catches typos
    // (a dropped/extra digit) without ever blocking the entry.
    function paint() {
      var v = inp.value.trim();
      var n = parseFloat(v);
      var bad = v !== '' && !(n >= 160 && n <= 230);
      inp.className = INPUT_BASE + (bad
        ? ' border-yellow-400 bg-yellow-50 focus:border-yellow-500'
        : ' border-gray-200 focus:border-black');
    }
    inp.addEventListener('input', function () { pendingBodyweight = inp.value; paint(); });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
    });
    paint();
    box.appendChild(inp);
    return box;
  }

  // Scroll the weigh-in box into view and focus it — the post-last-set nudge.
  function nudgeWeighIn() {
    var box = root.querySelector('[data-weigh-in]');
    if (!box) return;
    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    var inp = box.querySelector('input');
    if (inp) inp.focus({ preventScroll: true });
  }

  // A small round icon button for the per-exercise controls.
  function iconBtn(kind, label) {
    var b = el('button', 'w-7 h-7 flex items-center justify-center rounded-full ' +
      'text-gray-300 hover:text-black hover:bg-gray-100 transition-colors');
    b.type = 'button';
    b.setAttribute('aria-label', label);
    b.title = label;
    if (kind === 'info') {
      b.appendChild(el('span',
        'w-4 h-4 flex items-center justify-center rounded-full border border-current ' +
        'text-[10px] font-semibold leading-none', 'i'));
    } else {
      b.appendChild(el('span', 'text-lg leading-none', '⋯'));
    }
    return b;
  }

  function renderExercise(ex) {
    var box = el('div', 'border border-gray-200 rounded-[4px] px-5 sm:px-6 py-4 mb-3');

    var head = el('div', 'flex items-center justify-between mb-3 gap-2');
    head.appendChild(el('p', 'text-sm font-medium', ex.name));
    var ctrls = el('div', 'flex items-center gap-1 shrink-0');
    var info = iconBtn('info', 'How to do ' + ex.name);
    info.addEventListener('click', function () { toggleInfo(ex); });
    var menu = iconBtn('menu', 'More options for ' + ex.name);
    menu.addEventListener('click', function () { toggleMenu(ex); });
    ctrls.appendChild(info);
    ctrls.appendChild(menu);
    head.appendChild(ctrls);
    box.appendChild(head);

    var rowWrap = el('div', 'flex flex-wrap items-center gap-2');
    ex.sets.forEach(function (s) { rowWrap.appendChild(setChip(ex, s)); });
    box.appendChild(rowWrap);

    // Inline editor slot (filled when a set chip is tapped).
    var slot = el('div', 'mt-3');
    slot.dataset.editorSlot = String(ex.exercise_id);
    box.appendChild(slot);

    // Panel slot for the "i" info card / "..." menu (one at a time).
    var panel = el('div', 'mt-3');
    panel.dataset.panelSlot = String(ex.exercise_id);
    box.appendChild(panel);
    return box;
  }

  function setChip(ex, s) {
    if (s.status === 'done') {
      // Tappable so a data-entry error can be corrected after the fact.
      var done = el('button',
        'set-pill !border-black bg-black text-white gap-1 hover:bg-gray-800 transition-colors', null);
      var check = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      check.setAttribute('viewBox', '0 0 24 24'); check.setAttribute('fill', 'none');
      check.setAttribute('stroke', 'currentColor'); check.setAttribute('stroke-width', '3');
      check.setAttribute('class', 'w-3 h-3');
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M20 6 9 17l-5-5'); check.appendChild(path);
      done.appendChild(check);
      done.appendChild(document.createTextNode(setText(s, true)));
      done.addEventListener('click', function () { openEditor(ex, s); });
      return done;
    }
    if (s.status === 'skipped') {
      return el('span', 'set-pill text-gray-300 line-through', setText(s, false));
    }
    // pending → tappable
    var chip = el('button', 'set-pill hover:border-black hover:text-black transition-colors',
      setText(s, false));
    chip.addEventListener('click', function () { openEditor(ex, s); });
    return chip;
  }

  // ── Set editor (log a pending set, or correct a done one) ───────────────────

  // A plain labelled number field (reps).
  function numField(label, value, step, min) {
    var w = el('label', 'flex flex-col gap-1');
    w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', label));
    var inp = el('input', 'w-20 border border-gray-200 rounded-[4px] px-2.5 py-1.5 text-sm focus:outline-none focus:border-black transition-colors');
    inp.type = 'number'; inp.inputMode = 'decimal';
    if (step != null) inp.step = step;
    if (min != null) inp.min = min;
    if (value != null) inp.value = num(value);
    w.appendChild(inp);
    return { wrap: w, input: inp };
  }

  // Nudge the weight input by a signed delta (weight itself can be negative, for assisted
  // work). Rounds to kill float drift; an empty field counts as 0.
  function nudgeWeight(inp, delta) {
    var cur = parseFloat(inp.value);
    if (isNaN(cur)) cur = 0;
    inp.value = num(Math.round((cur + delta) * 100) / 100);
    maybeFocus(inp);
  }

  function stepBtn(label, delta, inp) {
    var b = el('button', 'shrink-0 w-9 h-9 flex items-center justify-center rounded-[4px] ' +
      'border border-gray-200 text-xs text-gray-600 hover:border-black hover:text-black transition-colors',
      label);
    b.type = 'button';
    b.addEventListener('click', function () { nudgeWeight(inp, delta); });
    return b;
  }

  // Weight as a centered editable number flanked by graduated steppers — −5/−1/−.5 on the
  // left, +.5/+1/+5 on the right — so a working weight is a few taps, not a keyboard entry,
  // while the field itself stays editable for anything the buttons don't cover.
  function weightField(value) {
    var w = el('div', 'flex flex-col gap-1');
    w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', 'Weight'));
    var row = el('div', 'flex items-center gap-1.5');
    var inp = el('input', 'flex-1 min-w-0 text-center border border-gray-200 rounded-[4px] ' +
      'px-2 py-1.5 text-sm focus:outline-none focus:border-black transition-colors');
    inp.type = 'number'; inp.inputMode = 'decimal'; inp.step = 'any';
    if (value != null) inp.value = num(value);
    [['−5', -5], ['−1', -1], ['−.5', -0.5]].forEach(function (st) {
      row.appendChild(stepBtn(st[0], st[1], inp));
    });
    row.appendChild(inp);
    [['+.5', 0.5], ['+1', 1], ['+5', 5]].forEach(function (st) {
      row.appendChild(stepBtn(st[0], st[1], inp));
    });
    w.appendChild(row);
    return { wrap: w, input: inp };
  }

  // Difficulty as an Easy/Med/Hard toggle, mapped to RPE behind the scenes. Prefilled from
  // the set's RPE (done) or the trainer's planned target_rpe (pending); tapping the active
  // choice again clears it. getRpe() yields the stored number, or null when none is picked.
  function difficultyField(initialRpe) {
    var w = el('div', 'flex flex-col gap-1');
    w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', 'Difficulty'));
    var row = el('div', 'flex gap-2');
    var selected = rpeToLabel(initialRpe);
    var BASE = 'flex-1 h-9 rounded-[4px] text-sm transition-colors ';
    var ON = 'bg-black text-white';
    var OFF = 'border border-gray-200 text-gray-500 hover:border-black hover:text-black';
    var btns = [];
    function paint() {
      btns.forEach(function (o) { o.btn.className = BASE + (o.d.key === selected ? ON : OFF); });
    }
    DIFFICULTY.forEach(function (d) {
      var b = el('button', '', d.key);
      b.type = 'button';
      b.addEventListener('click', function () {
        selected = (selected === d.key) ? null : d.key;
        paint();
      });
      btns.push({ btn: b, d: d });
      row.appendChild(b);
    });
    paint();
    w.appendChild(row);
    return {
      wrap: w,
      getRpe: function () {
        var hit = DIFFICULTY.filter(function (d) { return d.key === selected; })[0];
        return hit ? hit.rpe : null;
      },
    };
  }

  function openEditor(ex, s) {
    closePanels();
    var slot = root.querySelector('[data-editor-slot="' + ex.exercise_id + '"]');
    if (!slot) return;
    if (editingSetId === s.set_id) { slot.innerHTML = ''; editingSetId = null; return; }
    editingSetId = s.set_id;
    // close any other open editor
    root.querySelectorAll('[data-editor-slot]').forEach(function (n) {
      if (n !== slot) n.innerHTML = '';
    });
    slot.innerHTML = '';
    var done = s.status === 'done';

    var form = el('div', 'flex flex-col gap-3 border-t border-gray-100 pt-3');

    // Done sets prefill their actuals (you're correcting them); pending prefill targets —
    // weight, reps, and the planned difficulty (target_rpe) the trainer set.
    // Weight is signed: negative = assistance (band/machine), 0 = bodyweight, positive = added.
    var weight = weightField(done ? s.weight_lbs : s.target_weight_lbs);
    var reps = numField('Reps', done ? s.reps : s.target_reps, '1', 0);
    var diff = difficultyField(done ? s.rpe : s.target_rpe);

    var actions = el('div', 'flex items-center gap-3 pt-1');
    var save = el('button', 'h-[34px] px-3 bg-black text-white rounded-[4px] text-sm hover:bg-gray-800 transition-colors',
      done ? 'Save' : 'Log set');
    var cancel = el('button', 'h-[34px] px-3 text-sm text-gray-400 hover:text-black transition-colors', 'Cancel');
    cancel.addEventListener('click', function () { slot.innerHTML = ''; editingSetId = null; });
    save.addEventListener('click', function () {
      if (done) saveSet(s.set_id, weight.input, reps.input, diff, save);
      else completeSet(s.set_id, weight.input, reps.input, diff, save);
    });
    actions.appendChild(save);
    actions.appendChild(cancel);

    form.appendChild(weight.wrap);
    form.appendChild(reps.wrap);
    form.appendChild(diff.wrap);
    form.appendChild(actions);
    slot.appendChild(form);
    // Bring the just-opened editor into view (it expands below the set chips, which
    // can sit below the fold) without yanking focus into a field — on touch the
    // keyboard would cover the steppers/difficulty buttons we want you tapping.
    if (slot.scrollIntoView) slot.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    // For a logged set, blanking reps clears it — surface that gesture.
    if (done) {
      slot.appendChild(el('p', 'text-[11px] text-gray-400 mt-2',
        'Clear reps and save to remove this set.'));
    }
    // Enter in any field submits.
    form.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); save.click(); }
    });
    maybeFocus(weight.input);
  }

  async function completeSet(setId, weightInp, repsInp, diff, btn) {
    btn.disabled = true;
    var r = await postJSON(base + '/trainer/set/' + setId + '/complete', {
      weight_lbs: weightInp.value, reps: repsInp.value, rpe: diff.getRpe(),
    });
    if (!r.ok || (r.data && r.data.error)) {
      btn.disabled = false; btn.textContent = 'Error';
      return;
    }
    editingSetId = null;
    render(r.data); // response is the updated plan
    // If that was the last set and there's no weight in yet, nudge a weigh-in.
    var p = r.data, pr = p && p.progress;
    if (pr && pr.total > 0 && pr.done === pr.total &&
        p.bodyweight == null && pendingBodyweight.trim() === '') nudgeWeighIn();
  }

  async function saveSet(setId, weightInp, repsInp, diff, btn) {
    btn.disabled = true;
    var r = await postJSON(base + '/trainer/set/' + setId + '/update', {
      weight_lbs: weightInp.value, reps: repsInp.value, rpe: diff.getRpe(),
    });
    if (!r.ok || (r.data && r.data.error)) {
      btn.disabled = false; btn.textContent = 'Error';
      return;
    }
    editingSetId = null;
    render(r.data); // response is the updated plan
  }

  // ── Per-exercise info ("i") and menu ("...") panels ─────────────────────────

  function panelSlotFor(eid) { return root.querySelector('[data-panel-slot="' + eid + '"]'); }

  function closePanels() {
    root.querySelectorAll('[data-panel-slot]').forEach(function (n) { n.innerHTML = ''; });
    openPanel = null;
  }

  function closeEditors() {
    root.querySelectorAll('[data-editor-slot]').forEach(function (n) { n.innerHTML = ''; });
    editingSetId = null;
  }

  async function toggleInfo(ex) {
    var slot = panelSlotFor(ex.exercise_id);
    if (!slot) return;
    if (openPanel && openPanel.eid === ex.exercise_id && openPanel.kind === 'info') {
      closePanels(); return;
    }
    closePanels(); closeEditors();
    openPanel = { eid: ex.exercise_id, kind: 'info' };
    var card = el('div', 'border-t border-gray-100 pt-3 space-y-2');
    card.appendChild(el('p', 'text-sm text-gray-400', 'Loading…'));
    slot.appendChild(card);
    var info = {};
    try {
      var res = await fetch(base + '/trainer/exercise/' + ex.exercise_id + '/info.json',
        { headers: { 'Accept': 'application/json' } });
      info = await res.json();
    } catch (e) { info = { error: 'Could not load technique notes.' }; }
    // Bail if the user closed/switched the panel while we were fetching.
    if (!(openPanel && openPanel.eid === ex.exercise_id && openPanel.kind === 'info')) return;
    renderInfo(card, ex, info || {});
  }

  function renderInfo(card, ex, info) {
    card.innerHTML = '';
    function block(label, text) {
      if (!text) return;
      card.appendChild(el('p', 'text-[10px] uppercase tracking-widest text-gray-400', label));
      card.appendChild(el('p', 'text-sm text-gray-700 leading-relaxed', text));
    }
    // Muscle emphasis tiers (primary / secondary / tertiary), each only if present.
    var m = info.muscles || {};
    var tiers = [['primary', m.primary], ['secondary', m.secondary], ['tertiary', m.tertiary]]
      .filter(function (t) { return t[1] && t[1].length; });
    if (tiers.length) {
      var mline = el('p', 'text-xs text-gray-500 flex flex-wrap gap-x-3 gap-y-0.5');
      tiers.forEach(function (t) {
        var span = el('span', '');
        span.appendChild(el('span', 'text-gray-400', t[0] + ' '));
        span.appendChild(document.createTextNode(t[1].join(', ')));
        mline.appendChild(span);
      });
      card.appendChild(mline);
    }
    // Saved form photos. free-exercise-db ships a start + finish frame; with both, overlay
    // them and let the shared .rep-loop CSS (base.html) crossfade the two so the rep moves.
    // A lone image (or a self-looping gif) just renders as a still.
    if (info.image_link && info.image_link_end) {
      var wrap = el('div', 'rep-loop relative inline-block rounded-[4px] border border-gray-100 overflow-hidden');
      var start = el('img', 'block max-h-48 w-auto');
      start.src = info.image_link; start.alt = (info.name || ex.name || '') + ' — start of the rep';
      start.loading = 'lazy';
      var finish = el('img', 'rep-loop-end absolute inset-0 h-full w-full object-cover');
      finish.src = info.image_link_end; finish.alt = (info.name || ex.name || '') + ' — finish of the rep';
      finish.loading = 'lazy';
      wrap.appendChild(start); wrap.appendChild(finish);
      card.appendChild(wrap);
    } else if (info.image_link) {
      var img = el('img', 'rounded-[4px] border border-gray-100 max-h-48 w-auto');
      img.src = info.image_link; img.alt = (info.name || ex.name || '') + ' technique';
      img.loading = 'lazy';
      card.appendChild(img);
    }
    block('Technique', info.technique_notes);
    block('Common mistakes', info.common_mistakes);
    block('Cautions', info.cautions);
    if (!info.technique_notes && !info.common_mistakes && !info.cautions) {
      card.appendChild(el('p', 'text-sm text-gray-400',
        'No saved technique notes yet — watch a quick video below, or ask the trainer for cues.'));
    }
    var links = el('div', 'flex flex-wrap gap-4 pt-1 text-sm');
    var yt = el('a', 'text-gray-700 hover:text-black underline transition-colors', 'Watch on YouTube ↗');
    yt.href = info.youtube_search ||
      ('https://www.youtube.com/results?search_query=' + encodeURIComponent((ex.name || '') + ' proper form technique'));
    yt.target = '_blank'; yt.rel = 'noopener noreferrer';
    links.appendChild(yt);
    if (info.video_link) {
      var vl = el('a', 'text-gray-700 hover:text-black underline transition-colors', 'Saved video ↗');
      vl.href = info.video_link; vl.target = '_blank'; vl.rel = 'noopener noreferrer';
      links.appendChild(vl);
    }
    card.appendChild(links);
  }

  function toggleMenu(ex) {
    var slot = panelSlotFor(ex.exercise_id);
    if (!slot) return;
    if (openPanel && openPanel.eid === ex.exercise_id && openPanel.kind === 'menu') {
      closePanels(); return;
    }
    closePanels(); closeEditors();
    openPanel = { eid: ex.exercise_id, kind: 'menu' };
    var card = el('div', 'border-t border-gray-100 pt-3 flex flex-wrap gap-2');
    var replace = el('button', 'set-pill hover:border-black hover:text-black transition-colors',
      'Replace · similar muscles');
    replace.addEventListener('click', function () { doReplace(ex); });
    var del = el('button', 'set-pill text-red-500 !border-red-200 hover:!border-red-500 transition-colors',
      'Delete exercise');
    del.addEventListener('click', function () { doDelete(ex); });
    card.appendChild(replace);
    card.appendChild(del);
    slot.appendChild(card);
  }

  function doReplace(ex) {
    closePanels();
    var msg = 'Replace ' + ex.name + ' in my plan with a different exercise that hits the ' +
      'same muscles — pick the substitute and set the weight and reps from my training history.';
    if (window.TrainerChat && window.TrainerChat.send) window.TrainerChat.send(msg);
    else document.dispatchEvent(new CustomEvent('trainer:open-chat'));
  }

  async function doDelete(ex) {
    if (!window.confirm('Remove ' + ex.name + ' from your plan? Any sets you logged for it will be deleted.')) return;
    closePanels();
    var r = await postJSON(base + '/trainer/exercise/' + ex.exercise_id + '/remove', {});
    if (r.ok && r.data && !r.data.error) render(r.data);
  }

  // ── Finish / empty state ────────────────────────────────────────────────────

  async function onFinish() {
    var p = currentPlan;
    var pr = (p && p.progress) || { done: 0, total: 0 };

    // Only warn about dropping sets when there actually ARE unfinished ones.
    var left = pr.total - pr.done;
    if (pr.total > 0 && left > 0) {
      if (!window.confirm('Finish this workout? ' + left + ' unfinished set' +
        (left === 1 ? '' : 's') + ' will be dropped.')) return;
    }

    // The weigh-in is submitted only now — read what's typed into the box.
    var bw = parseFloat(pendingBodyweight);
    var hasWeight = pendingBodyweight.trim() !== '' && bw > 0;
    var alreadyLogged = p && p.bodyweight != null;

    // Warn if finishing with no bodyweight going in (and none logged earlier today).
    if (!hasWeight && !alreadyLogged) {
      if (!window.confirm('Finish without logging your bodyweight?')) return;
    }
    // Save the typed weight before finishing (the only place it's submitted).
    if (hasWeight) {
      await postJSON(base + '/trainer/bodyweight', { weight_lbs: bw });
    }
    pendingBodyweight = '';
    var r = await postJSON(base + '/trainer/finish', {});
    render({ active: false, justFinished: !(r.data && r.data.deleted_empty) && r.ok });
  }

  function renderEmpty(plan) {
    var box = el('div', 'border border-gray-200 rounded-[4px] px-6 py-10 text-center');
    if (plan && plan.justFinished) {
      box.appendChild(el('p', 'text-sm font-medium mb-1', 'Workout finished ✓'));
      box.appendChild(el('p', 'text-sm text-gray-400 mb-5', 'Nice work. It’s in your training history.'));
    } else {
      box.appendChild(el('p', 'text-sm font-medium mb-1', 'No active plan'));
      box.appendChild(el('p', 'text-sm text-gray-400 mb-5', 'Ask the trainer to build today’s routine.'));
    }
    var cta = el('button',
      'inline-flex items-center gap-2 px-4 h-10 bg-black text-white rounded-[4px] text-sm hover:bg-gray-800 transition-colors',
      'Open trainer chat');
    cta.addEventListener('click', function () {
      document.dispatchEvent(new CustomEvent('trainer:open-chat'));
    });
    box.appendChild(cta);
    root.appendChild(box);
  }

  async function refresh() {
    try {
      var res = await fetch(base + '/trainer/plan.json', { headers: { 'Accept': 'application/json' } });
      if (!res.ok) return;
      render(await res.json());
    } catch (e) {}
  }

  // ── Boot ─────────────────────────────────────────────────────────────────
  window.TrainerPlan = { render: render, refresh: refresh };
  var seed = document.getElementById('plan-data');
  var initial = {};
  try { initial = JSON.parse(seed.textContent); } catch (e) {}
  render(initial);
})();
