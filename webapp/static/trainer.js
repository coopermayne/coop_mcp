/*
 * Trainer plan card. Renders the active workout plan into #plan-root and handles the
 * write paths the page owns directly. The between-sets loop is the UP NEXT card at
 * the top: the next pending set with its target, steppers to adjust, and a row of
 * RPE buttons where ONE tap both rates the set and logs it, which then starts a rest
 * timer. Below it the full plan: tap any set chip to log it out of order, edit a logged
 * ('done') set to fix a data-entry error, drop or replace an exercise (the per-exercise
 * "..." menu), and finish the session. Building/swapping the routine happens in the chat, which calls
 * window.TrainerPlan.refresh() after each write (see _trainer_chat_panel).
 *
 * One render path: the server bootstraps the initial plan as JSON; every update
 * (tap, edit, finish, or chat-driven refresh) re-renders from a fresh plan object.
 */
(function () {
  var root = document.getElementById('plan-root');
  if (!root) return;
  var base = root.dataset.base || '';
  // Which session this page is: a whole week can be planned at once, so every write
  // names its workout instead of letting the server pick "the active one". Seeded from
  // the page's data-workout-id and re-read from each plan payload we render.
  var wid = root.dataset.workoutId || '';
  function url(path) { return base + '/trainer/' + wid + path; }
  var editingSetId = null; // only one inline set editor open at a time
  var openPanel = null;    // {eid, kind:'menu'} — at most one menu panel open
  var currentPlan = null;  // last rendered plan (Finish reads its progress)
  var reordering = false;  // reorder mode: arrows to the left of each exercise, header "Done"
  var reorderList = null;  // working copy of the visible exercises while reordering

  // A programmatic focus() pops the mobile soft keyboard, which covers the weight
  // steppers and the RPE buttons — the very controls that let you log a
  // set without typing. So on a touch-primary device we skip auto-focusing the
  // editor's inputs; on a mouse/desktop there's no keyboard to get in the way, so
  // focusing still helps (Enter-to-submit, caret ready for typing).
  var isTouch = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  function maybeFocus(inp) { if (!isTouch) inp.focus(); }

  function num(x) {
    if (x === null || x === undefined || x === '') return '';
    return (+x).toString();
  }

  // Difficulty is RPE, entered on a 6-10 scale with "reps left in the tank" under each
  // number (10 = nothing left, 8 = two more). It used to be Easy/Med/Hard stored as
  // 5/7/9, which the trainer couldn't tell apart from a grind to failure; the trainer
  // picks the next weight from this number, so the buttons give it the resolution.
  var RPE_CHOICES = [6, 7, 8, 9, 10];
  function rirLabel(rpe) { return rpe >= 10 ? 'max' : (10 - rpe) + ' left'; }
  // The button an RPE prefills to. Halves round DOWN (a 9.5 target prefills 9), so a
  // planned near-max never prefills as an actual failure.
  function nearestRpe(rpe) {
    if (rpe === null || rpe === undefined) return null;
    return Math.min(10, Math.max(6, Math.ceil(rpe - 0.5)));
  }
  // A heavy set: the trainer programmed it at RPE 9+ (top sets, PR attempts).
  function isHeavy(s) { return s.target_rpe != null && s.target_rpe >= 9; }

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
  // Weight as shown. On a BODYWEIGHT-BASED exercise (pull-ups, dips: see isBodyweight)
  // the number is load relative to bodyweight under the signed-weight convention, so it
  // reads signed: "−40" is 40 lb of assistance, "+25" is 25 added, "BW" is neither.
  // Everything else is a plain load ("135").
  function wLabel(w, bw) {
    if (!bw) return num(w);
    if (+w === 0) return 'BW';
    return (+w < 0 ? '−' + num(-w) : '+' + num(w));
  }

  // An exercise is bodyweight-based when ANY weight it has carried — planned, logged
  // this session, or in its history — is 0 or below. No barbell lift is ever loaded
  // with ≤0, so one assisted or bodyweight set marks the movement for good, which is
  // what keeps a pull-up reading "+25" (not a bare "25") once assistance gives way
  // to added weight.
  function isBodyweight(ex) {
    var ws = [];
    ex.sets.forEach(function (s) { ws.push(s.weight_lbs, s.target_weight_lbs); });
    var h = hist(ex);
    if (h.last) h.last.sets.forEach(function (s) { ws.push(s.weight_lbs); });
    if (h.best) ws.push(h.best.weight_lbs);
    return ws.some(function (w) { return w != null && +w <= 0; });
  }

  function setText(s, done, bw) {
    var w = done ? s.weight_lbs : s.target_weight_lbs;
    var r = done ? s.reps : s.target_reps;
    var dur = s.duration_seconds, dist = s.distance_miles;
    var parts;
    if (w != null && r != null) parts = wLabel(w, bw) + ' × ' + r;
    else if (r != null) parts = r + ' rep' + (r === 1 ? '' : 's');
    else if (w != null) parts = num(w) + ' lb';
    else if (dist != null || dur != null) {
      var cardio = [];
      if (dist != null) cardio.push(num(dist) + ' mi');
      if (dur != null) cardio.push(durLabel(dur));
      parts = cardio.join(' · ');
    }
    else parts = '—';
    if (done && s.rpe != null) parts += ' @' + num(s.rpe);
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
    if (plan && plan.workout_id) wid = String(plan.workout_id);
    if (!plan || !plan.active) { reordering = false; reorderList = null; renderEmpty(plan); return; }

    var pr = plan.progress || { done: 0, total: 0 };
    var visible = plan.exercises.filter(function (ex) { return liveSets(ex).length; });

    // Header: focus + progress, with a reorder toggle on the right (Done while reordering).
    var head = el('div', 'flex items-end justify-between mb-4');
    var left = el('div');
    // Just "Plan" — the page header above already carries this session's focus and day.
    left.appendChild(el('p', 'text-[10px] uppercase tracking-widest text-gray-400 mb-1', 'Plan'));
    left.appendChild(el('p', 'text-lg font-semibold tracking-tight',
      pr.done + ' / ' + pr.total + ' sets'));
    head.appendChild(left);
    left.className = 'flex-1 min-w-0 mr-4';
    var bar = el('div', 'mt-2 h-1 rounded-full bg-gray-100 overflow-hidden');
    var fill = el('div', 'h-full bg-yellow-400 transition-all duration-500');
    fill.style.width = (pr.total ? Math.round(100 * pr.done / pr.total) : 0) + '%';
    bar.appendChild(fill);
    left.appendChild(bar);
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

    // Reorder mode: just the exercises with ↑/↓ arrows; Finish is hidden.
    if (reordering) {
      reorderList.forEach(function (ex, i) { root.appendChild(renderReorderRow(ex, i)); });
      root.appendChild(el('p', 'text-[11px] text-gray-400 mt-3 mb-1',
        'Reorder with the arrows, then tap Done.'));
      return;
    }

    // The trainer's notes for the session (the PR targets, a cue, why it's light) — the
    // intent behind the numbers, which the model writes when it builds the plan.
    if (plan.notes) {
      var notes = el('div', 'mb-4 border-l-2 border-yellow-400 pl-3 py-0.5');
      notes.appendChild(el('p', 'text-[13px] text-gray-600 leading-relaxed whitespace-pre-line', plan.notes));
      root.appendChild(notes);
    }

    var next = nextPending(visible);
    if (next) root.appendChild(renderUpNext(next.ex, next.set, plan));
    else if (pr.done) root.appendChild(renderAllDone());

    // Exercises (fully swapped-out ones are hidden).
    visible.forEach(function (ex) {
      root.appendChild(renderExercise(ex, next && next.ex.exercise_id === ex.exercise_id));
    });

    paintRest();
    ensureTicker();
    holdWake();

    // The big full-width Finish ("Done") button at the bottom. No weigh-in box: a
    // bodyweight is a MORNING reading, not a gym artifact, so it's entered on /graphs
    // next to the line it moves. This card is about sets.
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
    var r = await postJSON(url('/discard'), {});
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
    var r = await postJSON(url('/reorder'), { order: order });
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
  // Finish is quiet while sets remain (one stray tap there drops the rest of the
  // session) and becomes the big yellow call to action once everything is logged.
  function renderFinish() {
    var pr = (currentPlan && currentPlan.progress) || { done: 0, total: 0 };
    var complete = pr.total > 0 && pr.done >= pr.total;
    var b = el('button', complete
      ? 'w-full py-5 mt-1 rounded-[4px] bg-yellow-400 hover:bg-yellow-500 ' +
        'text-white font-bold text-lg uppercase tracking-widest transition-colors'
      : 'w-full py-3 mt-1 rounded-[4px] border border-gray-200 text-gray-400 ' +
        'text-xs uppercase tracking-widest hover:border-black hover:text-black transition-colors',
      'Finish workout');
    b.type = 'button';
    b.addEventListener('click', onFinish);
    return b;
  }

  // A small round icon button for the per-exercise "..." menu.
  function iconBtn(label) {
    var b = el('button', 'w-7 h-7 flex items-center justify-center rounded-full ' +
      'text-gray-300 hover:text-black hover:bg-gray-100 transition-colors');
    b.type = 'button';
    b.setAttribute('aria-label', label);
    b.title = label;
    b.appendChild(el('span', 'text-lg leading-none', '⋯'));
    return b;
  }

  function renderExercise(ex, isCurrent) {
    var box = el('div', 'border rounded-[4px] px-5 sm:px-6 py-4 mb-3 ' +
      (isCurrent ? 'border-black' : 'border-gray-200'));

    var head = el('div', 'flex items-start justify-between mb-3 gap-2');
    var title = el('div', 'min-w-0');
    title.appendChild(el('p', 'text-sm font-medium', ex.name));
    var hl = historyLine(ex);
    if (hl) title.appendChild(el('p', 'text-[11px] text-gray-400 mt-0.5 leading-snug', hl));
    head.appendChild(title);
    var ctrls = el('div', 'flex items-center gap-1 shrink-0');
    var menu = iconBtn('More options for ' + ex.name);
    menu.addEventListener('click', function () { toggleMenu(ex); });
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

    // Panel slot for the "..." menu.
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
      done.appendChild(document.createTextNode(setText(s, true, isBodyweight(ex))));
      done.dataset.setId = String(s.set_id);  // where a PR burst aims after the re-render
      done.addEventListener('click', function () { openEditor(ex, s); });
      return done;
    }
    if (s.status === 'skipped') {
      return el('span', 'set-pill text-gray-300 line-through', setText(s, false, isBodyweight(ex)));
    }
    // pending → tappable. A heavy set (target RPE 9+) gets the accent border and its
    // target RPE, so the top set and a PR attempt don't look like warm-ups.
    var heavy = isHeavy(s);
    var chip = el('button', 'set-pill hover:border-black hover:text-black transition-colors' +
      (heavy ? ' !border-yellow-400' : ''),
      setText(s, false, isBodyweight(ex)) + (heavy ? ' @' + num(s.target_rpe) : ''));
    if (s.note) chip.title = s.note;
    chip.dataset.setId = String(s.set_id);
    chip.addEventListener('click', function () { openEditor(ex, s); });
    return chip;
  }

  // ── Set editor (log a pending set, or correct a done one) ───────────────────

  // Nudge the weight input by a signed delta (weight itself can be negative, for assisted
  // work). Rounds to kill float drift; an empty field counts as 0.
  function nudgeWeight(inp, delta) {
    var cur = parseFloat(inp.value);
    if (isNaN(cur)) cur = 0;
    inp.value = num(Math.round((cur + delta) * 100) / 100);
    maybeFocus(inp);
  }

  // Nudge the reps input by ±1, clamped at 0 (no negative reps); an empty field counts as 0.
  function nudgeReps(inp, delta) {
    var cur = parseInt(inp.value, 10);
    if (isNaN(cur)) cur = 0;
    inp.value = String(Math.max(0, cur + delta));
    maybeFocus(inp);
  }

  // A graduated stepper button; `nudge` defaults to the weight stepper.
  function stepBtn(label, delta, inp, nudge) {
    var b = el('button', 'shrink-0 w-9 h-9 flex items-center justify-center rounded-[4px] ' +
      'border border-gray-200 text-xs text-gray-600 hover:border-black hover:text-black transition-colors',
      label);
    b.type = 'button';
    b.addEventListener('click', function () { (nudge || nudgeWeight)(inp, delta); });
    return b;
  }

  // Weight as a centered editable number flanked by graduated steppers — −5/−1/−.5 on the
  // left, +.5/+1/+5 on the right — so a working weight is a few taps, not a keyboard entry,
  // while the field itself stays editable for anything the buttons don't cover.
  // On a bodyweight-based exercise the label spells out the sign, since "−" on the
  // stepper means MORE assistance (easier), not less weight on the bar.
  function weightField(value, bw) {
    var w = el('div', 'flex flex-col gap-1');
    w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400',
      bw ? 'Weight (− assist · + added)' : 'Weight'));
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

  // Reps as a centered editable number flanked by −1 / +1 steppers — the usual nudge when a
  // set lands a rep or two off plan, without popping the keyboard. Field stays editable for
  // bigger jumps.
  function repsField(value) {
    var w = el('div', 'flex flex-col gap-1');
    w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', 'Reps'));
    var row = el('div', 'flex items-center gap-1.5');
    var inp = el('input', 'flex-1 min-w-0 text-center border border-gray-200 rounded-[4px] ' +
      'px-2 py-1.5 text-sm focus:outline-none focus:border-black transition-colors');
    inp.type = 'number'; inp.inputMode = 'numeric'; inp.step = '1'; inp.min = '0';
    if (value != null) inp.value = num(value);
    row.appendChild(stepBtn('−1', -1, inp, nudgeReps));
    row.appendChild(inp);
    row.appendChild(stepBtn('+1', 1, inp, nudgeReps));
    w.appendChild(row);
    return { wrap: w, input: inp };
  }

  // RPE as a 6-10 toggle with "reps left" under each number. Prefilled from the set's
  // RPE (done) or the trainer's target (pending); tapping the active choice clears it.
  // getRpe() yields the number, or null when none is picked.
  function difficultyField(initialRpe) {
    var w = el('div', 'flex flex-col gap-1');
    w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', 'RPE'));
    var row = el('div', 'flex gap-1.5');
    var selected = nearestRpe(initialRpe);
    var BASE = 'flex-1 h-11 rounded-[4px] flex flex-col items-center justify-center leading-none transition-colors ';
    var ON = 'bg-black text-white';
    var OFF = 'border border-gray-200 text-gray-500 hover:border-black hover:text-black';
    var btns = [];
    function paint() {
      btns.forEach(function (o) { o.btn.className = BASE + (o.rpe === selected ? ON : OFF); });
    }
    RPE_CHOICES.forEach(function (rpe) {
      var b = el('button', '');
      b.type = 'button';
      b.appendChild(el('span', 'text-sm font-medium', String(rpe)));
      b.appendChild(el('span', 'text-[9px] mt-1 opacity-60', rirLabel(rpe)));
      b.addEventListener('click', function () {
        selected = (selected === rpe) ? null : rpe;
        paint();
      });
      btns.push({ btn: b, rpe: rpe });
      row.appendChild(b);
    });
    paint();
    w.appendChild(row);
    return { wrap: w, getRpe: function () { return selected; } };
  }

  // ── Up next ─────────────────────────────────────────────────────────────────

  // The first pending set, in the plan's exercise order then set order.
  function nextPending(visible) {
    for (var i = 0; i < visible.length; i++) {
      var sets = visible[i].sets;
      for (var j = 0; j < sets.length; j++) {
        if (sets[j].status === 'pending') return { ex: visible[i], set: sets[j] };
      }
    }
    return null;
  }

  function hist(ex) {
    return (currentPlan && currentPlan.history && currentPlan.history[String(ex.exercise_id)]) || {};
  }

  function shortDate(iso) {
    if (!iso) return '';
    var d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  // "Last Sep 28: 160×8 @8, 160×7 @9 · Best 170×5" — the reference you want mid-set.
  function historyLine(ex) {
    var h = hist(ex), parts = [], bw = isBodyweight(ex);
    if (h.last && h.last.sets && h.last.sets.length) {
      parts.push('Last ' + shortDate(h.last.date) + ': ' + h.last.sets.map(function (s) {
        return setText(s, true, bw).replace(' × ', '×');
      }).join(', '));
    }
    if (h.best) parts.push('Best ' + wLabel(h.best.weight_lbs, bw) + '×' + h.best.reps);
    return parts.join(' · ');
  }

  // Would this target beat the best before this session? Same rule as the server's
  // personal-best check: heavier, or the same weight for more reps.
  function isPrAttempt(ex, s) {
    var b = hist(ex).best;
    var w = s.target_weight_lbs, r = s.target_reps;
    if (!b || w == null || r == null) return false;
    return w > b.weight_lbs || (w === b.weight_lbs && r > b.reps);
  }

  function renderUpNext(ex, s, plan) {
    var card = el('div', 'border-2 border-black rounded-[6px] px-4 sm:px-5 pt-4 pb-5 mb-5');
    var timerSlot = el('div');
    timerSlot.dataset.restSlot = '1';
    card.appendChild(timerSlot);

    var top = el('div', 'flex items-center gap-2 mb-1');
    var pos = ex.sets.filter(function (x) { return x.status !== 'skipped'; });
    var idx = pos.indexOf(s) + 1;
    top.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400',
      'Up next · set ' + idx + ' of ' + pos.length));
    if (isPrAttempt(ex, s)) {
      top.appendChild(el('span', 'text-[10px] uppercase tracking-widest font-semibold px-1.5 py-0.5 rounded bg-yellow-400 text-black', 'PR attempt'));
    } else if (isHeavy(s)) {
      top.appendChild(el('span', 'text-[10px] uppercase tracking-widest font-semibold px-1.5 py-0.5 rounded border border-yellow-400 text-yellow-600 dark:text-yellow-300', 'Top set'));
    }
    card.appendChild(top);
    card.appendChild(el('p', 'text-xl font-bold tracking-tight', ex.name));

    var target = setText(s, false, isBodyweight(ex));
    if (s.target_rpe != null) target += ' @' + num(s.target_rpe);
    card.appendChild(el('p', 'text-sm text-gray-500 mt-0.5', 'Target ' + target));
    if (s.note) card.appendChild(el('p', 'text-[13px] text-gray-600 mt-2 leading-snug', s.note));
    var hl = historyLine(ex);
    if (hl) card.appendChild(el('p', 'text-[11px] text-gray-400 mt-1 leading-snug', hl));

    var isCardio = s.target_weight_lbs == null && s.target_reps == null;
    if (isCardio) {
      // Cardio has no targets to adjust; log it from its chip (the editor) instead.
      var open = el('button', 'mt-4 w-full h-11 rounded-[4px] bg-black text-white text-sm', 'Log it');
      open.type = 'button';
      open.addEventListener('click', function () { openEditor(ex, s); });
      card.appendChild(open);
      return card;
    }

    var form = el('div', 'flex flex-col gap-3 mt-4');
    var weight = weightField(s.target_weight_lbs, isBodyweight(ex));
    var reps = repsField(s.target_reps);
    form.appendChild(weight.wrap);
    form.appendChild(reps.wrap);

    // The log row: ONE tap rates the set and logs it with the numbers above.
    var lw = el('div', 'flex flex-col gap-1');
    lw.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400',
      'Done — how hard? (tap to log)'));
    var row = el('div', 'flex gap-1.5');
    RPE_CHOICES.forEach(function (rpe) {
      var b = el('button', 'flex-1 h-14 rounded-[4px] flex flex-col items-center justify-center leading-none ' +
        'bg-black text-white hover:bg-gray-800 transition-colors disabled:opacity-40');
      b.type = 'button';
      b.appendChild(el('span', 'text-lg font-semibold', String(rpe)));
      b.appendChild(el('span', 'text-[9px] mt-1 opacity-70', rirLabel(rpe)));
      b.addEventListener('click', function () {
        row.querySelectorAll('button').forEach(function (x) { x.disabled = true; });
        logSet(s, weight.input.value, reps.input.value, rpe, function () {
          row.querySelectorAll('button').forEach(function (x) { x.disabled = false; });
        });
      });
      row.appendChild(b);
    });
    lw.appendChild(row);
    form.appendChild(lw);
    card.appendChild(form);
    return card;
  }

  function renderAllDone() {
    var box = el('div', 'border-2 border-yellow-400 rounded-[6px] px-5 py-4 mb-5');
    var slot = el('div'); slot.dataset.restSlot = '1';
    box.appendChild(slot);
    box.appendChild(el('p', 'text-base font-semibold', 'Every set is logged.'));
    box.appendChild(el('p', 'text-sm text-gray-500 mt-0.5', 'Finish the workout to save it to your history.'));
    return box;
  }

  // ── Rest timer ──────────────────────────────────────────────────────────────
  // Starts when a set is logged, sized by how hard the set was (RPE 9+ → 3:00, 8 → 2:30,
  // else 1:30). It's an end TIMESTAMP kept in localStorage, not a ticking counter, so it
  // survives a re-render, a reload and a locked phone. Page-only state; the server
  // never hears about it.
  var REST_KEY = 'trainer-rest';
  var restTick = null, audioCtx = null;

  function restFor(rpe) { return rpe >= 9 ? 180 : rpe >= 8 ? 150 : 90; }
  function readRest() {
    try { return JSON.parse(localStorage.getItem(REST_KEY) || 'null'); } catch (e) { return null; }
  }
  function writeRest(v) {
    try { if (v) localStorage.setItem(REST_KEY, JSON.stringify(v)); else localStorage.removeItem(REST_KEY); } catch (e) {}
  }
  function startRest(seconds) {
    // The tap that logged the set is a user gesture, the one moment iOS lets a page
    // unlock audio for the end-of-rest beep.
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume();
    } catch (e) {}
    writeRest({ wid: wid, ends: Date.now() + seconds * 1000, total: seconds, beeped: false });
    paintRest();
  }
  function beep() {
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
    if (!audioCtx) return;
    try {
      [0, 0.25].forEach(function (t) {
        var o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.frequency.value = 880; o.connect(g); g.connect(audioCtx.destination);
        g.gain.setValueAtTime(0.25, audioCtx.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + t + 0.18);
        o.start(audioCtx.currentTime + t); o.stop(audioCtx.currentTime + t + 0.2);
      });
    } catch (e) {}
  }
  function mmss(sec) {
    sec = Math.max(0, Math.round(sec));
    return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
  }
  function paintRest() {
    var slot = root.querySelector('[data-rest-slot]');
    var r = readRest();
    if (!slot) return;
    if (!r || String(r.wid) !== String(wid)) { slot.innerHTML = ''; return; }
    var left = (r.ends - Date.now()) / 1000;
    if (left <= -60) { writeRest(null); slot.innerHTML = ''; return; }  // stale: gone after a minute
    if (left <= 0 && !r.beeped) { r.beeped = true; writeRest(r); beep(); }
    if (!slot.firstChild) {
      var wrap = el('div', 'flex items-center gap-3 mb-4 pb-4 border-b border-gray-100');
      var t = el('span', 'text-3xl font-bold tabular-nums tracking-tight');
      t.dataset.restTime = '1';
      var lab = el('span', 'text-[10px] uppercase tracking-widest text-gray-400 flex-1');
      lab.dataset.restLabel = '1';
      var plus = el('button', 'h-8 px-2.5 rounded-[4px] border border-gray-200 text-xs text-gray-500 hover:border-black hover:text-black transition-colors', '+30s');
      plus.type = 'button';
      plus.addEventListener('click', function () {
        var c = readRest(); if (!c) return;
        c.ends = Math.max(c.ends, Date.now()) + 30000; c.beeped = false; writeRest(c); paintRest();
      });
      var skip = el('button', 'h-8 px-2.5 rounded-[4px] border border-gray-200 text-xs text-gray-500 hover:border-black hover:text-black transition-colors', 'Skip');
      skip.type = 'button';
      skip.addEventListener('click', function () { writeRest(null); paintRest(); });
      wrap.appendChild(t); wrap.appendChild(lab); wrap.appendChild(plus); wrap.appendChild(skip);
      slot.appendChild(wrap);
    }
    var time = slot.querySelector('[data-rest-time]'), label = slot.querySelector('[data-rest-label]');
    if (left > 0) {
      time.textContent = mmss(left);
      time.className = 'text-3xl font-bold tabular-nums tracking-tight';
      label.textContent = 'Rest';
    } else {
      time.textContent = 'Go';
      time.className = 'text-3xl font-bold tracking-tight text-yellow-500';
      label.textContent = 'Rest’s up';
    }
  }
  function ensureTicker() {
    if (restTick) return;
    restTick = setInterval(paintRest, 500);
  }

  // ── Keep the screen awake ───────────────────────────────────────────────────
  // A phone that locks between sets costs you an unlock on every log. Hold a screen
  // wake lock while a session is open; the browser drops it when the tab hides, so
  // re-take it on return. Silently absent where the API isn't supported.
  var wakeLock = null;
  async function holdWake() {
    if (!('wakeLock' in navigator) || !currentPlan || !currentPlan.active) return;
    if (wakeLock && !wakeLock.released) return;
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { holdWake(); paintRest(); }
  });

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
    var weight = weightField(done ? s.weight_lbs : s.target_weight_lbs, isBodyweight(ex));
    var reps = repsField(done ? s.reps : s.target_reps);
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

  // Confetti when the server says the set just logged is a personal best. The server
  // states the fact (server.pr_for_set, relayed by the route as plan.celebrate); the page
  // decides whether to throw anything — and only ONCE per set at a given weight x reps,
  // so re-saving a corrected record doesn't fire over and over. Deduping belongs here
  // rather than in the server, which should keep answering the question honestly: a
  // corrected set that is still the heaviest ever IS still a best. Raising 135x8 to
  // 145x8 fires again, which is right — it's a bigger record. A reload forgets, so a
  // correction after one can fire a second time; two spare seconds, against a column.
  var celebrated = {};
  function celebratePR(plan) {
    var c = plan && plan.celebrate;
    if (!c || c.kind !== 'pr' || !window.Confetti) return;
    var key = c.set_id + '@' + c.weight_lbs + 'x' + c.reps;
    if (celebrated[key]) return;
    celebrated[key] = 1;
    // render() has already wiped root, so find the chip fresh rather than holding a node.
    window.Confetti.burst(root.querySelector('[data-set-id="' + c.set_id + '"]') || root);
  }

  async function completeSet(setId, weightInp, repsInp, diff, btn) {
    btn.disabled = true;
    var s = { set_id: setId };
    logSet(s, weightInp.value, repsInp.value, diff.getRpe(), function () {
      btn.disabled = false; btn.textContent = 'Error';
    });
  }

  // Log a pending set (Up next or the chip editor), start the rest timer off its RPE,
  // re-render, and throw confetti if it was a personal best.
  async function logSet(s, weight, reps, rpe, onError) {
    var r = await postJSON(base + '/trainer/set/' + s.set_id + '/complete', {
      weight_lbs: weight, reps: reps, rpe: rpe,
    });
    if (!r.ok || (r.data && r.data.error)) { if (onError) onError(); return; }
    editingSetId = null;
    var p = r.data.progress || {};
    if (p.total && p.done < p.total) startRest(restFor(rpe || 7));
    else writeRest(null);
    render(r.data); // response is the updated plan
    celebratePR(r.data);
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
    celebratePR(r.data);
  }

  // ── Per-exercise menu ("...") panel ─────────────────────────────────────────

  function panelSlotFor(eid) { return root.querySelector('[data-panel-slot="' + eid + '"]'); }

  function closePanels() {
    root.querySelectorAll('[data-panel-slot]').forEach(function (n) { n.innerHTML = ''; });
    openPanel = null;
  }

  function closeEditors() {
    root.querySelectorAll('[data-editor-slot]').forEach(function (n) { n.innerHTML = ''; });
    editingSetId = null;
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
    var r = await postJSON(url('/exercise/' + ex.exercise_id + '/remove'), {});
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

    // Nothing to ask about but the sets: finishing no longer prompts for a weigh-in,
    // because weighing isn't part of training any more (it's a morning reading, entered
    // on /graphs).
    var r = await postJSON(url('/finish'), {});
    render({ active: false, justFinished: !(r.data && r.data.deleted_empty) && r.ok });
    // This page belonged to one session and that session is over — head back to
    // Training, where it's now at the top of the history and the rest of the week is
    // still upcoming. The finished state shows for a beat first so the tap lands.
    setTimeout(function () { window.location.href = base + '/workouts'; }, 1200);
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
    var back = el('p', 'mt-4');
    var link = el('a', 'text-xs uppercase tracking-widest text-gray-400 hover:text-black transition-colors',
      'Back to training');
    link.href = base + '/workouts';
    back.appendChild(link);
    box.appendChild(back);
    root.appendChild(box);
  }

  async function refresh() {
    try {
      var res = await fetch(url('/plan.json'), { headers: { 'Accept': 'application/json' } });
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
