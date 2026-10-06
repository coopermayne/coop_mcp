/*
 * Workout mode: the /trainer/{id} session page as ONE fixed screen, never a scrolling
 * page. You use it standing at a rack between sets, phone in one hand, so the layout
 * is built around that loop and nothing moves under your thumb:
 *
 *   top bar   back · session name · progress · whole plan · trainer chat · ⋯ menu
 *   strip     every exercise as a pill with its done count; tap to jump (machine busy)
 *   stage     rest clock, which set this is, the exercise, its target, the coach's cue,
 *             last time / best, and this exercise's sets as chips (tap a done one to
 *             correct it, a pending one to do it next)
 *   dock      pinned to the bottom, in the thumb zone: weight and reps steppers and the
 *             RPE 6-10 row, where ONE tap rates the set and logs it
 *
 * Everything that isn't the next set lives in bottom SHEETS over the screen: the whole
 * plan (reorder, remove, replace), editing one set, the trainer's notes, the menu.
 *
 * One render path: the server bootstraps the plan as JSON; every write (tap, edit,
 * finish, or a chat-driven refresh via window.TrainerPlan.refresh) returns or fetches
 * a fresh plan object and the screen re-renders from it.
 */
(function () {
  var root = document.getElementById('plan-root');
  if (!root) return;
  var base = root.dataset.base || '';
  // Which session this page is: a whole week can be planned at once, so every write
  // names its workout instead of letting the server pick "the active one".
  var wid = root.dataset.workoutId || '';
  var chatEnabled = root.dataset.chat === '1';
  function url(path) { return base + '/trainer/' + wid + path; }

  var currentPlan = null;  // last rendered plan
  var selEid = null;       // the exercise on stage
  var selSetId = null;     // a pending set the user picked out of order (else the next one)
  var sheet = null;        // {kind, ...}: at most one sheet open
  var reordering = false;  // plan sheet in reorder mode
  var reorderList = null;  // working copy of the exercise order while reordering

  // A programmatic focus() pops the mobile keyboard over the steppers and RPE buttons,
  // the very controls that let you log without typing, so only focus on a mouse device.
  var isTouch = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  function maybeFocus(inp) { if (!isTouch) inp.focus(); }

  // ── Small helpers ─────────────────────────────────────────────────────────────

  function num(x) {
    if (x === null || x === undefined || x === '') return '';
    return (+x).toString();
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function btn(cls, text, onClick, label) {
    var b = el('button', cls, text);
    b.type = 'button';
    if (label) { b.setAttribute('aria-label', label); b.title = label; }
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  function svgIcon(paths, cls) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('fill', 'none');
    s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '2');
    s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
    s.setAttribute('class', cls || 'w-5 h-5');
    paths.forEach(function (d) {
      var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      p.setAttribute('d', d); s.appendChild(p);
    });
    return s;
  }
  var ICON = {
    back: ['M15 18l-6-6 6-6'],
    more: ['M5 12h.01', 'M12 12h.01', 'M19 12h.01'],
    chat: ['M7.9 20A9 9 0 1 0 4 16.1L2 22z'],
    list: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3 6h.01', 'M3 12h.01', 'M3 18h.01'],
    check: ['M20 6 9 17l-5-5'],
    up: ['M18 15l-6-6-6 6'],
    down: ['M6 9l6 6 6-6'],
    x: ['M18 6 6 18', 'M6 6l12 12'],
  };

  // Compact duration: '45m', '1h05m', '30s'.
  function durLabel(sec) {
    sec = Math.round(sec);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return h + 'h' + String(m).padStart(2, '0') + 'm';
    if (m) return m + 'm';
    return s + 's';
  }

  // Difficulty is RPE on a 6-10 scale with "reps left in the tank" under each number.
  // The trainer picks the next weight from it, so the buttons give it the resolution
  // (the old Easy/Med/Hard was stored as 5/7/9 and couldn't tell an 8 from failure).
  var RPE_CHOICES = [6, 7, 8, 9, 10];
  function rirLabel(rpe) { return rpe >= 10 ? 'max' : (10 - rpe) + ' left'; }
  // Halves round DOWN when prefilling (a 9.5 target prefills 9, never a failure).
  function nearestRpe(rpe) {
    if (rpe === null || rpe === undefined) return null;
    return Math.min(10, Math.max(6, Math.ceil(rpe - 0.5)));
  }
  function isHeavy(s) { return s.target_rpe != null && s.target_rpe >= 9; }

  // Weight as shown. On a BODYWEIGHT-BASED exercise (see isBodyweight) the number is
  // load relative to bodyweight under the signed-weight convention: "−40" is 40 lb of
  // assistance, "+25" is 25 added, "BW" is neither. Everything else is a plain load.
  function wLabel(w, bw) {
    if (!bw) return num(w);
    if (+w === 0) return 'BW';
    return (+w < 0 ? '−' + num(-w) : '+' + num(w));
  }

  // Bodyweight-based when ANY weight the exercise has carried (planned, logged this
  // session, or in its history) is 0 or below. No barbell lift is loaded with ≤0, so
  // one assisted set marks the movement for good, which keeps a pull-up reading "+25"
  // rather than a bare "25" once assistance gives way to added weight.
  function isBodyweight(ex) {
    var ws = [];
    ex.sets.forEach(function (s) { ws.push(s.weight_lbs, s.target_weight_lbs); });
    var h = hist(ex);
    if (h.last) h.last.sets.forEach(function (s) { ws.push(s.weight_lbs); });
    if (h.best) ws.push(h.best.weight_lbs);
    return ws.some(function (w) { return w != null && +w <= 0; });
  }

  // Label for a set: weight × reps for lifts, distance · time for cardio (+ @rpe when
  // done), using actuals when done and targets when not.
  function setText(s, done, bw) {
    var w = done ? s.weight_lbs : s.target_weight_lbs;
    var r = done ? s.reps : s.target_reps;
    var dur = s.duration_seconds, dist = s.distance_miles;
    var parts;
    if (w != null && r != null) parts = wLabel(w, bw) + ' × ' + r;
    else if (r != null) parts = r + ' rep' + (r === 1 ? '' : 's');
    else if (w != null) parts = wLabel(w, bw) + ' lb';
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

  async function postJSON(u, body) {
    var res = await fetch(u, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    var data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, data: data };
  }

  // ── Plan helpers ──────────────────────────────────────────────────────────────

  function liveSets(ex) { return ex.sets.filter(function (s) { return s.status !== 'skipped'; }); }
  function visibleExercises() {
    return ((currentPlan && currentPlan.exercises) || []).filter(function (ex) { return liveSets(ex).length; });
  }
  function pendingOf(ex) { return ex.sets.filter(function (s) { return s.status === 'pending'; }); }
  function exById(eid) {
    return visibleExercises().filter(function (ex) { return ex.exercise_id === eid; })[0] || null;
  }
  function hist(ex) {
    return (currentPlan && currentPlan.history && currentPlan.history[String(ex.exercise_id)]) || {};
  }

  // The exercise on stage: the user's pick while it still exists, else the first one
  // with a set left to do.
  function resolveSelection() {
    var vis = visibleExercises();
    if (selEid != null && exById(selEid)) return;
    var firstPending = vis.filter(function (ex) { return pendingOf(ex).length; })[0];
    selEid = firstPending ? firstPending.exercise_id : (vis[0] ? vis[0].exercise_id : null);
  }

  // After a log: stay on this exercise while it has sets left, else move to the next
  // exercise (in plan order, wrapping) that does.
  function advanceSelection() {
    var vis = visibleExercises();
    var cur = exById(selEid);
    if (cur && pendingOf(cur).length) return;
    var i = vis.indexOf(cur);
    for (var k = 1; k <= vis.length; k++) {
      var ex = vis[(i + k) % vis.length];
      if (ex && pendingOf(ex).length) { selEid = ex.exercise_id; return; }
    }
  }

  // The set the dock logs: one picked out of order, else the exercise's next pending.
  function currentSet(ex) {
    if (!ex) return null;
    if (selSetId != null) {
      var picked = ex.sets.filter(function (s) { return s.set_id === selSetId && s.status === 'pending'; })[0];
      if (picked) return picked;
      selSetId = null;
    }
    return pendingOf(ex)[0] || null;
  }

  // The dock's starting weight: the set's target, unless the set before it was planned
  // at the same weight and actually done at a different one (you went 140 where 135
  // was planned), in which case start from what you really lifted.
  function startingWeight(ex, s) {
    var sets = liveSets(ex), i = sets.indexOf(s);
    var prev = i > 0 ? sets[i - 1] : null;
    if (prev && prev.status === 'done' && prev.weight_lbs != null &&
        prev.target_weight_lbs === s.target_weight_lbs) return prev.weight_lbs;
    return s.target_weight_lbs;
  }

  function shortDate(iso) {
    if (!iso) return '';
    var d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  // "Last Sep 28: 160×8 @8, 160×7 @9 · Best 170×5": the reference you want mid-set.
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

  // Would this target beat the best before this session? The server's personal-best
  // rule: heavier, or the same weight for more reps.
  function isPrAttempt(ex, s) {
    var b = hist(ex).best;
    var w = s.target_weight_lbs, r = s.target_reps;
    if (!b || w == null || r == null) return false;
    return w > b.weight_lbs || (w === b.weight_lbs && r > b.reps);
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  function render(plan) {
    currentPlan = plan;
    if (plan && plan.workout_id) wid = String(plan.workout_id);
    root.innerHTML = '';
    var col = el('div', 'h-full max-w-md mx-auto flex flex-col sm:border-x border-gray-100');
    root.appendChild(col);
    if (!plan || !plan.active) { sheet = null; renderEmpty(col, plan); return; }

    resolveSelection();
    col.appendChild(renderTopBar());
    col.appendChild(renderStrip());
    if (plan.notes) col.appendChild(renderNotesLine());

    var ex = exById(selEid);
    var pr = plan.progress || { done: 0, total: 0 };
    var allDone = pr.total > 0 && pr.done >= pr.total;
    var s = currentSet(ex);

    var stage = el('div', 'flex-1 min-h-0 overflow-y-auto px-5 pt-4 pb-3 flex flex-col');
    var slot = el('div'); slot.dataset.restSlot = '1';
    stage.appendChild(slot);
    col.appendChild(stage);

    if (allDone) {
      renderAllDone(stage);
    } else if (ex && s) {
      renderStage(stage, ex, s);
      col.appendChild(renderDock(ex, s));
    } else if (ex) {
      renderExerciseDone(stage, ex);
    }

    if (sheet) renderSheet();
    paintRest();
    ensureTicker();
    holdWake();
    var cur = root.querySelector('[data-strip-current]');
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  function renderTopBar() {
    var pr = currentPlan.progress || { done: 0, total: 0 };
    var wrap = el('div', 'shrink-0');
    var bar = el('div', 'h-12 px-2 flex items-center gap-1');
    var back = el('a', 'w-10 h-10 flex items-center justify-center rounded-full text-gray-500 hover:text-black hover:bg-gray-100 transition-colors');
    back.href = base + '/workouts';
    back.setAttribute('aria-label', 'Back to training');
    back.appendChild(svgIcon(ICON.back));
    bar.appendChild(back);

    var title = el('div', 'flex-1 min-w-0');
    title.appendChild(el('p', 'text-sm font-semibold truncate', currentPlan.focus || 'Workout'));
    title.appendChild(el('p', 'text-[10px] uppercase tracking-widest text-gray-400 tabular-nums',
      pr.done + ' / ' + pr.total + ' sets'));
    bar.appendChild(title);

    var iconBtn = 'w-10 h-10 flex items-center justify-center rounded-full text-gray-500 hover:text-black hover:bg-gray-100 transition-colors';
    bar.appendChild(btn(iconBtn, null, function () { openSheet({ kind: 'plan' }); }, 'Whole plan'))
      .appendChild(svgIcon(ICON.list));
    if (chatEnabled) {
      bar.appendChild(btn(iconBtn, null, function () {
        if (window.TrainerChat) window.TrainerChat.open();
      }, 'Ask the trainer')).appendChild(svgIcon(ICON.chat));
    }
    bar.appendChild(btn(iconBtn, null, function () { openSheet({ kind: 'menu' }); }, 'Menu'))
      .appendChild(svgIcon(ICON.more));
    wrap.appendChild(bar);

    var track = el('div', 'h-0.5 bg-gray-100');
    var fill = el('div', 'h-full bg-yellow-400 transition-all duration-500');
    fill.style.width = (pr.total ? Math.round(100 * pr.done / pr.total) : 0) + '%';
    track.appendChild(fill);
    wrap.appendChild(track);
    return wrap;
  }

  // Every exercise as a pill: tap to put it on stage. Done ones carry a check.
  function renderStrip() {
    var strip = el('div', 'shrink-0 flex gap-2 overflow-x-auto px-4 py-3 border-b border-gray-100 no-scrollbar');
    visibleExercises().forEach(function (ex) {
      var live = liveSets(ex);
      var done = live.filter(function (s) { return s.status === 'done'; }).length;
      var complete = done === live.length;
      var on = ex.exercise_id === selEid;
      var p = btn('shrink-0 h-9 pl-3 pr-2.5 rounded-full flex items-center gap-2 text-sm whitespace-nowrap transition-colors ' +
        (on ? 'bg-black text-white' : complete ? 'border border-gray-200 text-gray-400'
          : 'border border-gray-200 text-gray-700 hover:border-black'), null, function () {
        selEid = ex.exercise_id; selSetId = null; render(currentPlan);
      });
      if (on) p.dataset.stripCurrent = '1';
      p.appendChild(el('span', '', ex.name));
      if (complete) p.appendChild(svgIcon(ICON.check, 'w-3.5 h-3.5'));
      else p.appendChild(el('span', 'text-[11px] tabular-nums opacity-60', done + '/' + live.length));
      strip.appendChild(p);
    });
    return strip;
  }

  // The trainer's notes for the session, one line; tap for the whole thing.
  function renderNotesLine() {
    var b = btn('shrink-0 w-full text-left px-5 py-2 border-b border-gray-100 flex items-center gap-2', null,
      function () { openSheet({ kind: 'notes' }); });
    b.appendChild(el('span', 'shrink-0 w-1 self-stretch rounded-full bg-yellow-400'));
    b.appendChild(el('span', 'text-[13px] text-gray-600 truncate', currentPlan.notes));
    return b;
  }

  function renderStage(stage, ex, s) {
    var bw = isBodyweight(ex);
    var live = liveSets(ex);
    var top = el('div', 'flex items-center gap-2');
    top.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400',
      'Set ' + (live.indexOf(s) + 1) + ' of ' + live.length));
    if (isPrAttempt(ex, s)) {
      top.appendChild(el('span', 'text-[10px] uppercase tracking-widest font-semibold px-1.5 py-0.5 rounded bg-yellow-400 text-black', 'PR attempt'));
    } else if (isHeavy(s)) {
      top.appendChild(el('span', 'text-[10px] uppercase tracking-widest font-semibold px-1.5 py-0.5 rounded border border-yellow-400 text-yellow-600 dark:text-yellow-300', 'Top set'));
    }
    stage.appendChild(top);
    stage.appendChild(el('h1', 'text-2xl font-bold tracking-tight mt-1 leading-tight', ex.name));

    var target = setText(s, false, bw);
    if (s.target_rpe != null) target += ' @' + num(s.target_rpe);
    stage.appendChild(el('p', 'text-base text-gray-500 mt-1', 'Target ' + target));
    if (s.note) stage.appendChild(el('p', 'text-[13px] text-gray-700 mt-2 leading-snug border-l-2 border-yellow-400 pl-2', s.note));
    var hl = historyLine(ex);
    if (hl) stage.appendChild(el('p', 'text-[11px] text-gray-400 mt-2 leading-snug', hl));

    stage.appendChild(renderSetChips(ex, s));

    // What comes after this set, at the foot of the stage: the next set of this
    // exercise, else the next exercise with work left. Lets you set up during rest.
    var after = afterThis(ex, s);
    if (after) {
      var then = el('p', 'mt-auto pt-4 text-[12px] text-gray-400 truncate');
      then.textContent = 'Then: ' + (after.ex === ex ? '' : after.ex.name + ' · ') +
        setText(after.set, false, isBodyweight(after.ex));
      stage.appendChild(then);
    }
  }

  function afterThis(ex, s) {
    var rest = pendingOf(ex).filter(function (x) { return x !== s; });
    if (rest.length) return { ex: ex, set: rest[0] };
    var vis = visibleExercises(), i = vis.indexOf(ex);
    for (var k = 1; k < vis.length; k++) {
      var c = vis[(i + k) % vis.length], p = c && pendingOf(c);
      if (p && p.length) return { ex: c, set: p[0] };
    }
    return null;
  }

  // This exercise's sets: done (tap to correct), the current one (outlined), pending
  // (tap to do it next), skipped (struck).
  function renderSetChips(ex, cur) {
    var bw = isBodyweight(ex);
    var row = el('div', 'flex flex-wrap gap-2 mt-4');
    ex.sets.forEach(function (s) {
      if (s.status === 'done') {
        var d = btn('set-pill !border-black bg-black text-white gap-1 hover:bg-gray-800 transition-colors', null,
          function () { openSheet({ kind: 'edit', eid: ex.exercise_id, setId: s.set_id }); });
        d.appendChild(svgIcon(ICON.check, 'w-3 h-3'));
        d.appendChild(document.createTextNode(setText(s, true, bw)));
        d.dataset.setId = String(s.set_id);
        row.appendChild(d);
      } else if (s.status === 'skipped') {
        row.appendChild(el('span', 'set-pill text-gray-300 line-through', setText(s, false, bw)));
      } else {
        var isCur = cur && s.set_id === cur.set_id;
        var p = btn('set-pill transition-colors ' + (isCur ? '!border-black !border-2 font-medium'
          : 'text-gray-500 hover:border-black hover:text-black') + (isHeavy(s) && !isCur ? ' !border-yellow-400' : ''),
          setText(s, false, bw), function () { selSetId = s.set_id; render(currentPlan); });
        p.dataset.setId = String(s.set_id);
        if (s.note) p.title = s.note;
        row.appendChild(p);
      }
    });
    return row;
  }

  // ── The dock: steppers + log row, pinned to the bottom ───────────────────────

  function stepBtn(label, onClick) {
    return btn('shrink-0 w-11 h-11 flex items-center justify-center rounded-[6px] border border-gray-200 text-sm text-gray-600 hover:border-black hover:text-black transition-colors active:bg-gray-100',
      label, onClick);
  }

  function numberInput(value, mode) {
    var inp = el('input', 'flex-1 min-w-0 h-11 text-center border border-gray-200 rounded-[6px] text-xl font-semibold tabular-nums focus:outline-none focus:border-black transition-colors');
    inp.type = 'number'; inp.inputMode = mode; inp.step = 'any';
    if (value != null) inp.value = num(value);
    return inp;
  }

  function nudge(inp, delta, minZero) {
    var cur = parseFloat(inp.value);
    if (isNaN(cur)) cur = 0;
    var v = Math.round((cur + delta) * 100) / 100;
    if (minZero) v = Math.max(0, v);
    inp.value = num(v);
    maybeFocus(inp);
  }

  // Weight: −5 −2.5 [n] +2.5 +5 (2.5 is the smallest plate/dumbbell jump; type for
  // anything else). Reps: −1 [n] +1.
  function weightRow(value, bw) {
    var wrap = el('div', 'flex flex-col gap-1');
    wrap.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400',
      bw ? 'Weight (− assist · + added)' : 'Weight'));
    var row = el('div', 'flex items-center gap-1.5');
    var inp = numberInput(value, 'decimal');
    row.appendChild(stepBtn('−5', function () { nudge(inp, -5); }));
    row.appendChild(stepBtn('−2.5', function () { nudge(inp, -2.5); }));
    row.appendChild(inp);
    row.appendChild(stepBtn('+2.5', function () { nudge(inp, 2.5); }));
    row.appendChild(stepBtn('+5', function () { nudge(inp, 5); }));
    wrap.appendChild(row);
    return { wrap: wrap, input: inp };
  }

  function repsRow(value) {
    var wrap = el('div', 'flex flex-col gap-1');
    wrap.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', 'Reps'));
    var row = el('div', 'flex items-center gap-1.5');
    var inp = numberInput(value, 'numeric');
    inp.step = '1'; inp.min = '0';
    row.appendChild(stepBtn('−1', function () { nudge(inp, -1, true); }));
    row.appendChild(inp);
    row.appendChild(stepBtn('+1', function () { nudge(inp, 1, true); }));
    wrap.appendChild(row);
    return { wrap: wrap, input: inp };
  }

  function renderDock(ex, s) {
    var dock = el('div', 'shrink-0 border-t border-gray-100 px-4 pt-3 flex flex-col gap-3 bg-white');
    dock.style.paddingBottom = 'calc(0.75rem + env(safe-area-inset-bottom))';
    var isCardio = s.target_weight_lbs == null && s.target_reps == null;
    var weight = null, reps = null;
    if (!isCardio) {
      weight = weightRow(startingWeight(ex, s), isBodyweight(ex));
      reps = repsRow(s.target_reps);
      dock.appendChild(weight.wrap);
      dock.appendChild(reps.wrap);
    }
    var lw = el('div', 'flex flex-col gap-1');
    lw.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400',
      'Log it · how hard was it?'));
    var row = el('div', 'flex gap-1.5');
    RPE_CHOICES.forEach(function (rpe) {
      var b = btn('flex-1 h-16 rounded-[6px] flex flex-col items-center justify-center leading-none ' +
        'bg-black text-white hover:bg-gray-800 transition-colors disabled:opacity-40', null, function () {
        row.querySelectorAll('button').forEach(function (x) { x.disabled = true; });
        logSet(s.set_id, weight ? weight.input.value : null, reps ? reps.input.value : null, rpe,
          function () { row.querySelectorAll('button').forEach(function (x) { x.disabled = false; }); });
      });
      b.appendChild(el('span', 'text-xl font-semibold', String(rpe)));
      b.appendChild(el('span', 'text-[9px] mt-1 opacity-70', rirLabel(rpe)));
      row.appendChild(b);
    });
    lw.appendChild(row);
    dock.appendChild(lw);
    return dock;
  }

  // ── Stage end states ──────────────────────────────────────────────────────────

  function renderExerciseDone(stage, ex) {
    stage.appendChild(el('p', 'text-[10px] uppercase tracking-widest text-gray-400', 'Done'));
    stage.appendChild(el('h1', 'text-2xl font-bold tracking-tight mt-1', ex.name));
    stage.appendChild(renderSetChips(ex, null));
    var vis = visibleExercises(), i = vis.indexOf(ex), next = null;
    for (var k = 1; k <= vis.length; k++) {
      var c = vis[(i + k) % vis.length];
      if (c && pendingOf(c).length) { next = c; break; }
    }
    if (next) {
      stage.appendChild(btn('mt-6 w-full h-14 rounded-[6px] bg-black text-white text-base font-medium hover:bg-gray-800 transition-colors',
        'Next: ' + next.name, function () { selEid = next.exercise_id; selSetId = null; render(currentPlan); }));
    }
  }

  function renderAllDone(stage) {
    var box = el('div', 'flex-1 flex flex-col items-center justify-center text-center gap-2 pb-8');
    box.appendChild(el('p', 'text-2xl font-bold tracking-tight', 'Every set is logged.'));
    box.appendChild(el('p', 'text-sm text-gray-500', 'Finish to save it to your history.'));
    box.appendChild(btn('mt-6 w-full h-16 rounded-[6px] bg-yellow-400 hover:bg-yellow-500 text-white font-bold text-lg uppercase tracking-widest transition-colors',
      'Finish workout', onFinish));
    stage.appendChild(box);
  }

  function renderEmpty(col, plan) {
    var box = el('div', 'flex-1 flex flex-col items-center justify-center text-center px-6 gap-1');
    if (plan && plan.justFinished) {
      box.appendChild(el('p', 'text-xl font-bold', 'Workout finished ✓'));
      box.appendChild(el('p', 'text-sm text-gray-400 mb-5', 'Nice work. It’s in your training history.'));
    } else {
      box.appendChild(el('p', 'text-base font-medium', 'No active plan'));
      box.appendChild(el('p', 'text-sm text-gray-400 mb-5', 'Ask the trainer to build today’s routine.'));
      if (chatEnabled) {
        box.appendChild(btn('inline-flex items-center gap-2 px-4 h-10 bg-black text-white rounded-[4px] text-sm hover:bg-gray-800 transition-colors',
          'Open trainer chat', function () { document.dispatchEvent(new CustomEvent('trainer:open-chat')); }));
      }
    }
    var link = el('a', 'mt-4 text-xs uppercase tracking-widest text-gray-400 hover:text-black transition-colors', 'Back to training');
    link.href = base + '/workouts';
    box.appendChild(link);
    col.appendChild(box);
  }

  // ── Sheets ────────────────────────────────────────────────────────────────────

  function openSheet(s) { sheet = s; reordering = false; reorderList = null; render(currentPlan); }
  function closeSheet() { sheet = null; reordering = false; reorderList = null; render(currentPlan); }

  function renderSheet() {
    var body;
    if (sheet.kind === 'menu') body = menuSheet();
    else if (sheet.kind === 'notes') body = notesSheet();
    else if (sheet.kind === 'plan') body = planSheet();
    else if (sheet.kind === 'edit') body = editSheet();
    if (!body) { sheet = null; return; }
    var overlay = el('div', 'fixed inset-0 z-40 flex items-end justify-center bg-black/40');
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeSheet(); });
    var card = el('div', 'w-full max-w-md bg-white rounded-t-2xl flex flex-col max-h-[85vh]');
    card.style.paddingBottom = 'env(safe-area-inset-bottom)';
    card.appendChild(sheetHead(body.title, body.action));
    var scroll = el('div', 'overflow-y-auto px-5 pb-5');
    scroll.appendChild(body.node);
    card.appendChild(scroll);
    overlay.appendChild(card);
    root.appendChild(overlay);
  }

  function sheetHead(title, action) {
    var h = el('div', 'shrink-0 flex items-center justify-between gap-3 px-5 h-14');
    h.appendChild(el('p', 'text-base font-semibold truncate', title));
    var right = el('div', 'flex items-center gap-2 shrink-0');
    if (action) right.appendChild(action);
    right.appendChild(btn('w-9 h-9 flex items-center justify-center rounded-full text-gray-400 hover:text-black hover:bg-gray-100 transition-colors',
      null, closeSheet, 'Close')).appendChild(svgIcon(ICON.x));
    h.appendChild(right);
    return h;
  }

  function menuItem(text, onClick, danger) {
    return btn('w-full text-left px-1 py-3.5 text-base border-b border-gray-100 transition-colors ' +
      (danger ? 'text-red-500' : 'hover:text-black'), text, onClick);
  }

  function menuSheet() {
    var n = el('div');
    n.appendChild(menuItem('Whole plan', function () { openSheet({ kind: 'plan' }); }));
    if (currentPlan.notes) n.appendChild(menuItem('Trainer’s notes', function () { openSheet({ kind: 'notes' }); }));
    if (window.TrainerCoaching) {
      n.appendChild(menuItem('Coaching preferences', function () { closeSheet(); window.TrainerCoaching.open(); }));
    }
    n.appendChild(menuItem('Finish workout', function () { closeSheet(); onFinish(); }));
    n.appendChild(menuItem('Delete plan', function () { closeSheet(); confirmDiscard(); }, true));
    return { title: currentPlan.focus || 'Workout', node: n };
  }

  function notesSheet() {
    return { title: 'Trainer’s notes',
      node: el('p', 'text-[15px] text-gray-700 leading-relaxed whitespace-pre-line', currentPlan.notes || '') };
  }

  // The whole session: each exercise with its sets. Tap an exercise to put it on stage,
  // a done set to correct it, a pending one to do it next; per-exercise Replace /
  // Remove; reorder mode.
  function planSheet() {
    var n = el('div', 'flex flex-col gap-2');
    var vis = visibleExercises();
    var action = null;
    if (vis.length > 1) {
      action = btn('h-8 px-3 rounded-full text-xs uppercase tracking-widest ' +
        (reordering ? 'bg-black text-white' : 'border border-gray-200 text-gray-500 hover:border-black hover:text-black'),
        reordering ? 'Save order' : 'Reorder', function () {
          if (reordering) { submitReorder(); return; }
          reordering = true; reorderList = vis.slice(); render(currentPlan);
        });
    }
    if (reordering) {
      reorderList.forEach(function (ex, i) {
        var r = el('div', 'flex items-center gap-3 border border-gray-200 rounded-[6px] pl-2 pr-4 py-2');
        var arrows = el('div', 'flex flex-col');
        var up = btn('w-8 h-7 flex items-center justify-center text-gray-400 hover:text-black disabled:opacity-20', null,
          function () { moveReorder(i, -1); }, 'Move up');
        up.appendChild(svgIcon(ICON.up, 'w-4 h-4')); up.disabled = i === 0;
        var dn = btn('w-8 h-7 flex items-center justify-center text-gray-400 hover:text-black disabled:opacity-20', null,
          function () { moveReorder(i, 1); }, 'Move down');
        dn.appendChild(svgIcon(ICON.down, 'w-4 h-4')); dn.disabled = i === reorderList.length - 1;
        arrows.appendChild(up); arrows.appendChild(dn);
        r.appendChild(arrows);
        r.appendChild(el('p', 'text-sm font-medium', ex.name));
        n.appendChild(r);
      });
      return { title: 'Reorder', node: n, action: action };
    }
    vis.forEach(function (ex) {
      var bw = isBodyweight(ex);
      var box = el('div', 'border rounded-[6px] px-4 py-3 ' + (ex.exercise_id === selEid ? 'border-black' : 'border-gray-200'));
      var head = el('div', 'flex items-start justify-between gap-2');
      var t = btn('text-left min-w-0', null, function () {
        selEid = ex.exercise_id; selSetId = null; closeSheet();
      });
      t.appendChild(el('p', 'text-sm font-medium', ex.name));
      var hl = historyLine(ex);
      if (hl) t.appendChild(el('p', 'text-[11px] text-gray-400 mt-0.5 leading-snug', hl));
      head.appendChild(t);
      var acts = el('div', 'flex items-center gap-1 shrink-0');
      if (chatEnabled) {
        acts.appendChild(btn('h-7 px-2 rounded-full border border-gray-200 text-[11px] text-gray-500 hover:border-black hover:text-black', 'Replace',
          function () { closeSheet(); doReplace(ex); }));
      }
      acts.appendChild(btn('h-7 px-2 rounded-full border border-red-200 text-[11px] text-red-500 hover:border-red-500', 'Remove',
        function () { doDelete(ex); }));
      head.appendChild(acts);
      box.appendChild(head);
      var chips = el('div', 'flex flex-wrap gap-1.5 mt-2');
      ex.sets.forEach(function (s) {
        if (s.status === 'done') {
          var d = btn('set-pill !border-black bg-black text-white gap-1', null,
            function () { openSheet({ kind: 'edit', eid: ex.exercise_id, setId: s.set_id }); });
          d.appendChild(svgIcon(ICON.check, 'w-3 h-3'));
          d.appendChild(document.createTextNode(setText(s, true, bw)));
          chips.appendChild(d);
        } else if (s.status === 'skipped') {
          chips.appendChild(el('span', 'set-pill text-gray-300 line-through', setText(s, false, bw)));
        } else {
          chips.appendChild(btn('set-pill text-gray-500 hover:border-black hover:text-black' + (isHeavy(s) ? ' !border-yellow-400' : ''),
            setText(s, false, bw), function () {
              selEid = ex.exercise_id; selSetId = s.set_id; closeSheet();
            }));
        }
      });
      box.appendChild(chips);
      n.appendChild(box);
    });
    return { title: 'Whole plan', node: n, action: action };
  }

  // Correct a logged set. Clearing it hands a planned set back to pending (and drops
  // an ad-hoc one), the server's clear_plan_set.
  function editSheet() {
    var ex = exById(sheet.eid);
    var s = ex && ex.sets.filter(function (x) { return x.set_id === sheet.setId; })[0];
    if (!s) return null;
    var n = el('div', 'flex flex-col gap-3');
    var weight = weightRow(s.weight_lbs, isBodyweight(ex));
    var reps = repsRow(s.reps);
    n.appendChild(weight.wrap);
    n.appendChild(reps.wrap);

    var rw = el('div', 'flex flex-col gap-1');
    rw.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', 'RPE'));
    var row = el('div', 'flex gap-1.5');
    var selected = nearestRpe(s.rpe);
    var rbtns = [];
    function paint() {
      rbtns.forEach(function (o) {
        o.b.className = 'flex-1 h-12 rounded-[6px] flex flex-col items-center justify-center leading-none transition-colors ' +
          (o.rpe === selected ? 'bg-black text-white' : 'border border-gray-200 text-gray-500 hover:border-black hover:text-black');
      });
    }
    RPE_CHOICES.forEach(function (rpe) {
      var b = btn('', null, function () { selected = (selected === rpe) ? null : rpe; paint(); });
      b.appendChild(el('span', 'text-sm font-medium', String(rpe)));
      b.appendChild(el('span', 'text-[9px] mt-1 opacity-60', rirLabel(rpe)));
      rbtns.push({ b: b, rpe: rpe });
      row.appendChild(b);
    });
    paint();
    rw.appendChild(row);
    n.appendChild(rw);

    var actions = el('div', 'flex items-center gap-3 pt-2');
    var save = btn('flex-1 h-12 rounded-[6px] bg-black text-white text-base hover:bg-gray-800 transition-colors disabled:opacity-40',
      'Save', async function () {
        save.disabled = true;
        // A blank reps box would CLEAR the set server-side; that's what the Clear
        // button is for, so Save refuses it rather than deleting by accident.
        if (reps.input.value === '') { save.disabled = false; return; }
        var r = await postJSON(base + '/trainer/set/' + s.set_id + '/update',
          { weight_lbs: weight.input.value, reps: reps.input.value, rpe: selected });
        if (!r.ok || (r.data && r.data.error)) { save.disabled = false; save.textContent = 'Error'; return; }
        sheet = null; render(r.data); celebratePR(r.data);
      });
    actions.appendChild(save);
    actions.appendChild(btn('h-12 px-4 rounded-[6px] border border-red-200 text-red-500 text-sm hover:border-red-500 transition-colors',
      'Clear set', async function () {
        var r = await postJSON(base + '/trainer/set/' + s.set_id + '/update', { reps: '' });
        if (r.ok && r.data && !r.data.error) { sheet = null; render(r.data); }
      }));
    n.appendChild(actions);
    return { title: ex.name + ' · set ' + (liveSets(ex).indexOf(s) + 1), node: n };
  }

  // ── Writes ────────────────────────────────────────────────────────────────────

  // Log a pending set, start the rest clock, advance the stage, and throw confetti at
  // the chip if it was a personal best.
  async function logSet(setId, weight, reps, rpe, onError) {
    var r = await postJSON(base + '/trainer/set/' + setId + '/complete', {
      weight_lbs: weight, reps: reps, rpe: rpe,
    });
    if (!r.ok || (r.data && r.data.error)) { if (onError) onError(); return; }
    var p = r.data.progress || {};
    if (p.total && p.done < p.total) startRest(restFor(rpe || 7));
    else writeRest(null);
    selSetId = null;
    currentPlan = r.data;
    resolveSelection();
    advanceSelection();
    render(r.data);
    celebratePR(r.data);
  }

  var celebrated = {};
  function celebratePR(plan) {
    var c = plan && plan.celebrate;
    if (!c || c.kind !== 'pr' || !window.Confetti) return;
    var key = c.set_id + '@' + c.weight_lbs + 'x' + c.reps;
    if (celebrated[key]) return;
    celebrated[key] = 1;
    window.Confetti.burst(root.querySelector('[data-set-id="' + c.set_id + '"]') || root);
  }

  function doReplace(ex) {
    var msg = 'Replace ' + ex.name + ' in my plan with a different exercise that hits the ' +
      'same muscles — pick the substitute and set the weight and reps from my training history.';
    if (window.TrainerChat && window.TrainerChat.send) window.TrainerChat.send(msg);
  }

  async function doDelete(ex) {
    if (!window.confirm('Remove ' + ex.name + ' from your plan? Any sets you logged for it will be deleted.')) return;
    var r = await postJSON(url('/exercise/' + ex.exercise_id + '/remove'), {});
    if (r.ok && r.data && !r.data.error) render(r.data);
  }

  function moveReorder(i, dir) {
    var j = i + dir;
    if (j < 0 || j >= reorderList.length) return;
    var tmp = reorderList[i]; reorderList[i] = reorderList[j]; reorderList[j] = tmp;
    render(currentPlan);
  }

  async function submitReorder() {
    var order = (reorderList || []).map(function (ex) { return ex.exercise_id; });
    reordering = false; reorderList = null;
    var r = await postJSON(url('/reorder'), { order: order });
    if (r.ok && r.data && !r.data.error) render(r.data);
    else refresh();
  }

  // A centered confirmation modal; calls opts.onConfirm() on commit.
  function confirmModal(opts) {
    var overlay = el('div', 'fixed inset-0 z-50 flex items-center justify-center px-4 bg-black/40');
    var card = el('div', 'bg-white rounded-[8px] shadow-xl max-w-sm w-full p-6');
    card.appendChild(el('p', 'text-base font-semibold mb-2', opts.title));
    card.appendChild(el('p', 'text-sm text-gray-500 leading-relaxed mb-6', opts.body));
    var rowBtns = el('div', 'flex justify-end gap-2');
    function close() { document.removeEventListener('keydown', onKey); overlay.remove(); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    rowBtns.appendChild(btn('px-4 h-11 rounded-[4px] text-sm text-gray-500 hover:text-black hover:bg-gray-100 transition-colors', 'Cancel', close));
    rowBtns.appendChild(btn('px-4 h-11 rounded-[4px] text-white text-sm font-medium transition-colors ' +
      (opts.danger === false ? 'bg-black hover:bg-gray-800' : 'bg-red-500 hover:bg-red-600'),
      opts.confirmText || 'Delete', function () { close(); opts.onConfirm(); }));
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.addEventListener('keydown', onKey);
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
      onConfirm: async function () {
        var r = await postJSON(url('/discard'), {});
        if (r.ok && r.data && !r.data.error) render(r.data); else refresh();
      },
    });
  }

  function onFinish() {
    var pr = (currentPlan && currentPlan.progress) || { done: 0, total: 0 };
    var left = pr.total - pr.done;
    if (pr.total > 0 && left > 0) {
      confirmModal({
        title: 'Finish this workout?',
        body: left + ' unfinished set' + (left === 1 ? '' : 's') + ' will be dropped.',
        confirmText: 'Finish', danger: false, onConfirm: finish,
      });
    } else finish();
  }

  async function finish() {
    var r = await postJSON(url('/finish'), {});
    writeRest(null);
    render({ active: false, justFinished: !(r.data && r.data.deleted_empty) && r.ok });
    // This page belonged to one session and it's over: head back to Training.
    setTimeout(function () { window.location.href = base + '/workouts'; }, 1200);
  }

  // ── Rest clock ────────────────────────────────────────────────────────────────
  // Counts UP from the moment a set is logged. The RPE sets a target (9+ → 3:00,
  // 8 → 2:30, else 1:30), shown as a quiet "/ 3:00" beside the clock; passing it turns
  // the clock yellow, and it keeps counting. No sound: the color is the cue. Stored as
  // a START timestamp in localStorage so it survives a re-render, a reload and a locked
  // phone. Page-only state; the server never hears about it.
  var REST_KEY = 'trainer-rest';
  var REST_MAX = 20 * 60;  // a clock still running after 20 min is a forgotten one
  var restTick = null;

  function restFor(rpe) { return rpe >= 9 ? 180 : rpe >= 8 ? 150 : 90; }
  function readRest() {
    var r = null;
    try { r = JSON.parse(localStorage.getItem(REST_KEY) || 'null'); } catch (e) {}
    if (r && r.started == null && r.ends != null) {  // a countdown from an older version
      r = { wid: r.wid, started: r.ends - (r.total || 0) * 1000, target: r.total };
    }
    return r;
  }
  function writeRest(v) {
    try { if (v) localStorage.setItem(REST_KEY, JSON.stringify(v)); else localStorage.removeItem(REST_KEY); } catch (e) {}
  }
  function startRest(target) { writeRest({ wid: wid, started: Date.now(), target: target }); }
  function mmss(sec) {
    sec = Math.max(0, Math.floor(sec));
    return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
  }
  function paintRest() {
    var slot = root.querySelector('[data-rest-slot]');
    if (!slot) return;
    var r = readRest();
    if (!r || String(r.wid) !== String(wid)) { slot.innerHTML = ''; return; }
    var elapsed = (Date.now() - r.started) / 1000;
    if (elapsed >= REST_MAX) { writeRest(null); slot.innerHTML = ''; return; }
    var over = r.target && elapsed >= r.target;
    if (!slot.firstChild) {
      var wrap = el('div', 'flex items-center gap-3 mb-3 pb-3 sm:mb-4 sm:pb-4 border-b border-gray-100');
      var clock = el('div', 'flex items-baseline gap-1.5 flex-1');
      var t = el('span', 'text-3xl sm:text-4xl font-bold tabular-nums tracking-tight');
      t.dataset.restTime = '1';
      var hint = el('span', 'text-sm text-gray-400 tabular-nums');
      hint.dataset.restHint = '1';
      clock.appendChild(t); clock.appendChild(hint);
      wrap.appendChild(clock);
      wrap.appendChild(btn('h-8 px-2.5 rounded-[4px] border border-gray-200 text-xs text-gray-500 hover:border-black hover:text-black transition-colors',
        'Hide', function () { writeRest(null); paintRest(); }));
      slot.appendChild(wrap);
    }
    var time = slot.querySelector('[data-rest-time]'), hintEl = slot.querySelector('[data-rest-hint]');
    time.textContent = mmss(elapsed);
    time.className = 'text-3xl sm:text-4xl font-bold tabular-nums tracking-tight' + (over ? ' text-yellow-500' : '');
    hintEl.textContent = r.target ? '/ ' + mmss(r.target) + ' rest' : 'rest';
  }
  function ensureTicker() { if (!restTick) restTick = setInterval(paintRest, 500); }

  // ── Keep the screen awake ─────────────────────────────────────────────────────
  // A phone that locks between sets costs an unlock on every log. Hold a wake lock
  // while a session is open; the browser drops it when the tab hides, so re-take it.
  var wakeLock = null;
  async function holdWake() {
    if (!('wakeLock' in navigator) || !currentPlan || !currentPlan.active) return;
    if (wakeLock && !wakeLock.released) return;
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { holdWake(); paintRest(); }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && sheet) closeSheet();
  });

  async function refresh() {
    try {
      var res = await fetch(url('/plan.json'), { headers: { 'Accept': 'application/json' } });
      if (!res.ok) return;
      render(await res.json());
    } catch (e) {}
  }

  // ── Boot ──────────────────────────────────────────────────────────────────────
  window.TrainerPlan = { render: render, refresh: refresh };
  var seed = document.getElementById('plan-data');
  var initial = {};
  try { initial = JSON.parse(seed.textContent); } catch (e) {}
  render(initial);
})();
