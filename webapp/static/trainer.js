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
    if (done && s.rpe != null) parts += '  @' + num(s.rpe);
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

  function render(plan) {
    root.innerHTML = '';
    editingSetId = null;
    openPanel = null;
    if (!plan || !plan.active) { renderEmpty(plan); return; }

    // Header: focus + progress + finish.
    var head = el('div', 'flex items-end justify-between mb-4');
    var left = el('div');
    left.appendChild(el('p', 'text-[10px] uppercase tracking-widest text-gray-400 mb-1',
      'Today’s plan' + (plan.focus ? ' · ' + plan.focus : '')));
    var pr = plan.progress || { done: 0, total: 0 };
    left.appendChild(el('p', 'text-lg font-semibold tracking-tight',
      pr.done + ' / ' + pr.total + ' sets'));
    head.appendChild(left);
    var finish = el('button',
      'text-xs uppercase tracking-widest text-gray-400 hover:text-black transition-colors', 'Finish');
    finish.addEventListener('click', onFinish);
    head.appendChild(finish);
    root.appendChild(head);

    // Exercises (fully swapped-out ones are hidden).
    plan.exercises.forEach(function (ex) {
      var live = ex.sets.filter(function (s) { return s.status !== 'skipped'; });
      if (!live.length) return;
      root.appendChild(renderExercise(ex));
    });
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

    var form = el('div', 'flex flex-wrap items-end gap-3 border-t border-gray-100 pt-3');
    function field(label, value, step, min, max) {
      var w = el('label', 'flex flex-col gap-1');
      w.appendChild(el('span', 'text-[10px] uppercase tracking-widest text-gray-400', label));
      var inp = el('input', 'w-20 border border-gray-200 rounded-[4px] px-2.5 py-1.5 text-sm focus:outline-none focus:border-black transition-colors');
      inp.type = 'number'; inp.inputMode = 'decimal';
      if (step != null) inp.step = step;
      if (min != null) inp.min = min;
      if (max != null) inp.max = max;
      if (value != null) inp.value = num(value);
      w.appendChild(inp);
      return { wrap: w, input: inp };
    }
    // Done sets prefill their actuals (you're correcting them); pending prefill targets.
    var weight = field('Weight', done ? s.weight_lbs : s.target_weight_lbs, 'any', 0, null);
    var reps = field('Reps', done ? s.reps : s.target_reps, '1', 0, null);
    var rpe = field('RPE', done ? s.rpe : null, '0.5', 1, 10);
    rpe.input.placeholder = '1–10';

    var save = el('button', 'h-[34px] px-3 bg-black text-white rounded-[4px] text-sm hover:bg-gray-800 transition-colors',
      done ? 'Save' : 'Log set');
    var cancel = el('button', 'h-[34px] px-3 text-sm text-gray-400 hover:text-black transition-colors', 'Cancel');
    cancel.addEventListener('click', function () { slot.innerHTML = ''; editingSetId = null; });
    save.addEventListener('click', function () {
      if (done) saveSet(s.set_id, weight.input, reps.input, rpe.input, save);
      else completeSet(s.set_id, weight.input, reps.input, rpe.input, save);
    });

    [weight, reps, rpe].forEach(function (f) { form.appendChild(f.wrap); });
    form.appendChild(save);
    form.appendChild(cancel);
    slot.appendChild(form);
    // For a logged set, blanking reps clears it — surface that gesture.
    if (done) {
      slot.appendChild(el('p', 'text-[11px] text-gray-400 mt-2',
        'Clear reps and save to remove this set.'));
    }
    // Enter in any field submits.
    form.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); save.click(); }
    });
    weight.input.focus();
  }

  async function completeSet(setId, weightInp, repsInp, rpeInp, btn) {
    btn.disabled = true;
    var r = await postJSON(base + '/trainer/set/' + setId + '/complete', {
      weight_lbs: weightInp.value, reps: repsInp.value, rpe: rpeInp.value,
    });
    if (!r.ok || (r.data && r.data.error)) {
      btn.disabled = false; btn.textContent = 'Error';
      return;
    }
    editingSetId = null;
    render(r.data); // response is the updated plan
  }

  async function saveSet(setId, weightInp, repsInp, rpeInp, btn) {
    btn.disabled = true;
    var r = await postJSON(base + '/trainer/set/' + setId + '/update', {
      weight_lbs: weightInp.value, reps: repsInp.value, rpe: rpeInp.value,
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
    // A saved gif/still of proper form, if any (a gif loops on its own).
    if (info.image_link) {
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
    if (!window.confirm('Remove ' + ex.name + ' from today’s plan? Any sets you logged for it will be deleted.')) return;
    closePanels();
    var r = await postJSON(base + '/trainer/exercise/' + ex.exercise_id + '/remove', {});
    if (r.ok && r.data && !r.data.error) render(r.data);
  }

  // ── Finish / empty state ────────────────────────────────────────────────────

  async function onFinish() {
    if (!window.confirm('Finish this workout? Unfinished sets will be dropped.')) return;
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
