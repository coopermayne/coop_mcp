/*
 * Trainer plan card. Renders the active workout plan into #plan-root and handles the
 * two write paths the page owns directly: tap-to-complete a planned set, and finish
 * the session. Everything else (building/swapping the routine) happens in the chat,
 * which calls window.TrainerPlan.refresh() after each write (see _trainer_chat_panel).
 *
 * One render path: the server bootstraps the initial plan as JSON; every update
 * (tap, finish, or chat-driven refresh) re-renders from a fresh plan object.
 */
(function () {
  var root = document.getElementById('plan-root');
  if (!root) return;
  var base = root.dataset.base || '';
  var editingSetId = null; // only one inline set editor open at a time

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

  // Label for a set: weight × reps (+ @rpe), using actuals when done, targets when not.
  function setText(s, done) {
    var w = done ? s.weight_lbs : s.target_weight_lbs;
    var r = done ? s.reps : s.target_reps;
    var parts;
    if (w != null && r != null) parts = num(w) + ' × ' + r;
    else if (r != null) parts = r + ' rep' + (r === 1 ? '' : 's');
    else if (w != null) parts = num(w) + ' lb';
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

  function renderExercise(ex) {
    var box = el('div', 'border border-gray-200 rounded-[4px] px-5 sm:px-6 py-4 mb-3');
    box.appendChild(el('p', 'text-sm font-medium mb-3', ex.name));

    var rowWrap = el('div', 'flex flex-wrap items-center gap-2');
    ex.sets.forEach(function (s) { rowWrap.appendChild(setChip(ex, s)); });
    box.appendChild(rowWrap);

    // Inline editor slot (filled when a pending chip is tapped).
    var slot = el('div', 'mt-3');
    slot.dataset.editorSlot = String(ex.exercise_id);
    box.appendChild(slot);
    return box;
  }

  function setChip(ex, s) {
    if (s.status === 'done') {
      var done = el('span',
        'set-pill !border-black bg-black text-white gap-1', null);
      var check = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      check.setAttribute('viewBox', '0 0 24 24'); check.setAttribute('fill', 'none');
      check.setAttribute('stroke', 'currentColor'); check.setAttribute('stroke-width', '3');
      check.setAttribute('class', 'w-3 h-3');
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M20 6 9 17l-5-5'); check.appendChild(path);
      done.appendChild(check);
      done.appendChild(document.createTextNode(setText(s, true)));
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

  function openEditor(ex, s) {
    var slot = root.querySelector('[data-editor-slot="' + ex.exercise_id + '"]');
    if (!slot) return;
    if (editingSetId === s.set_id) { slot.innerHTML = ''; editingSetId = null; return; }
    editingSetId = s.set_id;
    // close any other open editor
    root.querySelectorAll('[data-editor-slot]').forEach(function (n) {
      if (n !== slot) n.innerHTML = '';
    });
    slot.innerHTML = '';

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
    var weight = field('Weight', s.target_weight_lbs, 'any', 0, null);
    var reps = field('Reps', s.target_reps, '1', 0, null);
    var rpe = field('RPE', null, '0.5', 1, 10);
    rpe.input.placeholder = '1–10';

    var log = el('button', 'h-[34px] px-3 bg-black text-white rounded-[4px] text-sm hover:bg-gray-800 transition-colors', 'Log set');
    var cancel = el('button', 'h-[34px] px-3 text-sm text-gray-400 hover:text-black transition-colors', 'Cancel');
    cancel.addEventListener('click', function () { slot.innerHTML = ''; editingSetId = null; });
    log.addEventListener('click', function () { completeSet(s.set_id, weight.input, reps.input, rpe.input, log); });

    [weight, reps, rpe].forEach(function (f) { form.appendChild(f.wrap); });
    form.appendChild(log);
    form.appendChild(cancel);
    slot.appendChild(form);
    // Enter in any field logs the set.
    form.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); log.click(); }
    });
    weight.input.focus();
  }

  async function completeSet(setId, weightInp, repsInp, rpeInp, btn) {
    btn.disabled = true;
    var r = await postJSON(base + '/trainer/set/' + setId + '/complete', {
      weight_lbs: weightInp.value, reps: repsInp.value, rpe: rpeInp.value,
    });
    if (!r.ok || (r.data && r.data.error)) {
      btn.disabled = false;
      btn.textContent = (r.data && r.data.error) ? 'Error' : 'Error';
      return;
    }
    editingSetId = null;
    render(r.data); // response is the updated plan
  }

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
