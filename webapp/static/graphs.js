/* /graphs — line charts over bodyweight, drinks, and per-exercise strength
   progress. Rendering is uPlot (vendored, static/vendor/); this file is the
   common chart component (theme-aware defaults, synced cursors) plus the page
   logic (range presets, panel toggles, the exercise picker).

   Everything is client-side over the bootstrap JSON the route embeds — the
   whole history is small. Charts are canvas, so dark mode can't be a CSS
   override like the rest of the app: we rebuild the charts with the other
   palette when <html data-theme> flips. */
(function () {
  var root = document.querySelector('[data-graphs]');
  var dataEl = document.getElementById('graph-data');
  if (!root || !dataEl || typeof uPlot === 'undefined') return;

  var DATA = JSON.parse(dataEl.textContent);
  var DAY = 86400;

  /* ---------- palette ----------------------------------------------------
     Series hues: a colorblind-validated 8-slot categorical palette, stepped
     separately for the light and dark surfaces. The slot ORDER is part of the
     validation (adjacent pairs stay distinguishable) — assign in order, never
     cycle past 8 (the picker caps selection instead). Chrome (axis/grid) tones
     match the app's monochrome theme. */
  var SLOTS = {
    light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'],
    dark:  ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  };
  function theme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }
  function palette() {
    var dark = theme() === 'dark';
    return {
      slots: SLOTS[theme()],
      axis:  dark ? '#737373' : '#9ca3af',   // tick labels
      grid:  dark ? '#1f1f1f' : '#f3f4f6',   // hairline gridlines
    };
  }
  var WEIGHT_COLOR = { light: SLOTS.light[0], dark: SLOTS.dark[0] };
  // Drinks read against a guideline rather than as a bare trend: a day at or
  // under DRINK_LIMIT standard drinks is green, over it red (slots 5 and 7 of the
  // validated palette, so the pair stays distinguishable in both themes).
  var DRINK_LIMIT = 2;
  var UNDER_COLOR = { light: SLOTS.light[5], dark: SLOTS.dark[5] };
  var OVER_COLOR  = { light: SLOTS.light[7], dark: SLOTS.dark[7] };

  /* ---------- state (persisted) ----------------------------------------- */
  var STORE_KEY = 'graphs.state';
  // `ex` is deliberately NOT restored from storage: the panel always opens on
  // the lift trained most recently, so a visit starts on what you just did
  // rather than on whatever you happened to be looking at weeks ago. The
  // cycler/dropdown still move freely within the visit.
  var state = { range: 90, panels: { weight: true, drinks: true, exercises: true },
                ex: null, metric: 'top' };
  try {
    var saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      if (typeof saved.range === 'number') state.range = saved.range;
      if (saved.panels) state.panels = Object.assign(state.panels, saved.panels);
      if (saved.metric) state.metric = saved.metric;
    }
  } catch (e) {}
  function persist() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) {}
  }

  var exById = {};
  DATA.exercises.forEach(function (ex) { exById[ex.exercise_id] = ex; });
  // One exercise shows at a time; the cycle order is most-recently-trained
  // first, walking back to the lift touched longest ago (points are
  // date-ascending, so the last point is the latest session).
  var EX_ORDER = DATA.exercises.slice().sort(function (a, b) {
    var la = a.points[a.points.length - 1].date, lb = b.points[b.points.length - 1].date;
    return la < lb ? 1 : la > lb ? -1 : a.name.localeCompare(b.name);
  });
  if (!exById[state.ex]) state.ex = EX_ORDER.length ? EX_ORDER[0].exercise_id : null;
  function exIndex() {
    for (var i = 0; i < EX_ORDER.length; i++) if (EX_ORDER[i].exercise_id === state.ex) return i;
    return 0;
  }

  /* ---------- helpers ---------------------------------------------------- */
  function ts(iso) { return Date.parse(iso + 'T12:00:00') / 1000; }  // local noon, DST-safe
  function isoShift(iso, days) {   // calendar-day arithmetic, immune to DST
    var d = new Date(iso + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }
  var todayTs = ts(DATA.today);
  function minTs() { return state.range ? todayTs - (state.range - 1) * DAY : -Infinity; }
  function minIso() { return state.range ? isoShift(DATA.today, -(state.range - 1)) : ''; }
  function fmtDate(v) {
    var d = new Date(v * 1000);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }
  function fmtDateDow(v) {   // hover readout — day of week up front ("Sun, Aug 2, 2026")
    var d = new Date(v * 1000);
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  }
  function fmtNum(v) {
    if (v == null) return '';
    return (Math.round(v * 10) / 10).toLocaleString();
  }

  /* ---------- the common chart component --------------------------------
     One call = one themed uPlot panel: time x-axis, hairline grid, crosshair
     cursor synced across every panel on the page (the built-in live legend is
     the hover readout), 2px lines with visible points on sparse series. */
  var charts = [];   // [{u, el, height}] for resize/teardown
  function makeChart(el, series, data, opts) {
    opts = opts || {};
    var pal = palette();
    var axisFont = '11px Inter, system-ui, sans-serif';
    var u = new uPlot({
      width: el.clientWidth || 600,
      height: opts.height || 200,
      padding: [10, 12, 0, 0],
      cursor: {
        sync: { key: 'graphs' },
        points: { size: 7 },
        drag: { x: false, y: false },
      },
      legend: { live: true },
      scales: {
        // half-day pad each side so edge points aren't clipped by the plot border
        x: { time: true, range: function (u_, min, max) {
          return min == null ? [0, 1] : [min - DAY / 2, max + DAY / 2];
        } },
        y: {
          range: function (u_, min, max) {
            if (min == null) return [0, 1];
            if (opts.zeroBase) return [0, Math.max(max * 1.1, opts.yMin || 0) || 1];
            var pad = (max - min) * 0.12 || Math.abs(max) * 0.05 || 1;
            return [min - pad, max + pad];
          },
        },
      },
      axes: [
        { stroke: pal.axis, font: axisFont, grid: { show: false },
          ticks: { show: false }, size: 40, gap: 6,
          // data is daily — never let the time axis split below one day
          incrs: [DAY, 2 * DAY, 3 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 90 * DAY, 365 * DAY] },
        { stroke: pal.axis, font: axisFont, size: 44, gap: 4,
          grid: { stroke: pal.grid, width: 1 }, ticks: { show: false },
          values: function (u_, vals) { return vals.map(fmtNum); } },
      ],
      series: [{ label: 'Date', value: function (u_, v) { return v == null ? '—' : fmtDateDow(v); } }]
        .concat(series.map(function (s) {
          var cfg = {
            label: s.label,
            stroke: s.color,
            width: s.width || 2,
            dash: s.dash,
            spanGaps: true,
            points: { show: s.points !== false, size: 5, fill: s.color },
            value: function (u_, v) { return v == null ? '—' : fmtNum(v); },
          };
          // A bar series: one column per day, colored per-bar off s.colors (uPlot's
          // `disp` per-datum values) so a single series can carry a threshold's
          // worth of meaning without splitting into two.
          if (s.bars) {
            var colors = s.colors || [];
            var at = function () { return colors; };
            cfg.paths = uPlot.paths.bars({
              size: [0.72, 18],
              disp: { fill: { unit: 3, values: at }, stroke: { unit: 3, values: at } },
            });
            cfg.fill = s.color;
            cfg.width = 0;
            cfg.points = { show: false };
          }
          return cfg;
        })),
    }, data, el);
    charts.push({ u: u, el: el, height: opts.height || 200 });
    return u;
  }

  function teardown() {
    charts.forEach(function (c) { c.u.destroy(); });
    charts = [];
  }

  function emptyNote(el, msg) {
    var p = document.createElement('p');
    p.className = 'text-sm text-gray-400 py-6';
    p.textContent = msg;
    el.appendChild(p);
  }

  /* ---------- per-panel builders ----------------------------------------- */
  function buildWeight(el) {
    var pts = DATA.weight.filter(function (p) { return ts(p.date) >= minTs(); });
    if (!pts.length) return emptyNote(el, 'No weigh-ins in this range.');
    var col = WEIGHT_COLOR[theme()];
    var pal = palette();
    var goal = DATA.weight_goal;
    var wBy = {}, xsSet = {};
    pts.forEach(function (p) { var t = ts(p.date); wBy[t] = p.lbs; xsSet[t] = true; });

    // Pace line: from the weigh-in the goal was anchored at to (target_date,
    // target_lbs), clipped to the visible window (interpolated at the edges).
    // In the All range the axis extends to the goal date so the flag itself is
    // on screen; in windowed ranges you still see whether you're above or
    // below pace where the line crosses the window.
    var trajBy = null;
    if (goal && goal.target_date && goal.start_date && goal.start_lbs != null) {
      var gTs = ts(goal.target_date), sTs = ts(goal.start_date);
      if (gTs > sTs) {
        var winEnd = state.range ? todayTs : Math.max(todayTs, gTs);
        var t0 = Math.max(sTs, minTs() === -Infinity ? sTs : minTs());
        var t1 = Math.min(gTs, winEnd);
        if (t1 > t0) {
          var lerp = function (t) {
            return goal.start_lbs + (goal.target_lbs - goal.start_lbs) * (t - sTs) / (gTs - sTs);
          };
          trajBy = {};
          trajBy[t0] = lerp(t0);
          trajBy[t1] = lerp(t1);
          xsSet[t0] = xsSet[t1] = true;
        }
      }
    }
    var xs = Object.keys(xsSet).map(Number).sort(function (a, b) { return a - b; });
    var series = [{ label: 'Bodyweight', color: col }];
    var cols = [xs.map(function (t) { return wBy[t] != null ? wBy[t] : null; })];
    if (trajBy) {
      series.push({ label: 'Pace', color: col, dash: [5, 6], points: false, width: 1.5 });
      cols.push(xs.map(function (t) { return trajBy[t] != null ? trajBy[t] : null; }));
    }
    if (goal) {   // horizontal target line across the whole plot
      var gl = {};
      gl[xs[0]] = gl[xs[xs.length - 1]] = goal.target_lbs;
      series.push({ label: 'Goal', color: pal.axis, dash: [3, 5], points: false, width: 1.5 });
      cols.push(xs.map(function (t) { return gl[t] != null ? gl[t] : null; }));
    }
    makeChart(el, series, [xs].concat(cols), { height: 180 });
  }

  function buildDrinks(el) {
    if (!DATA.drinks.length) return emptyNote(el, 'No drinks logged yet.');
    // Gap-fill: a day with no row is sober (0), and the calendar shouldn't lie —
    // fill every day from the range start (or first logged day) through today.
    // Iterate calendar days as ISO strings, not timestamp += 86400 (DST).
    var start = DATA.drinks[0].date;
    if (minIso() && minIso() > start) start = minIso();
    var byDate = {};
    DATA.drinks.forEach(function (p) { byDate[p.date] = p.total; });
    var xs = [], ys = [], any = false;
    for (var d = start; d <= DATA.today; d = isoShift(d, 1)) {
      xs.push(ts(d));
      ys.push(byDate[d] || 0);
      if (byDate[d]) any = true;
    }
    if (!xs.length) return emptyNote(el, 'No days in this range.');
    // One bar per day, read against the DRINK_LIMIT guideline: at or under the
    // line is green, over it red. The limit itself is drawn as a flat dashed
    // series so the eye has the reference, and the y-range is floored just above
    // it so an all-sober stretch still shows the line.
    var t = theme();
    var colors = ys.map(function (v) { return v > DRINK_LIMIT ? OVER_COLOR[t] : UNDER_COLOR[t]; });
    var limit = xs.map(function () { return DRINK_LIMIT; });
    makeChart(el, [
      { label: 'Drinks', color: UNDER_COLOR[t], bars: true, colors: colors },
      { label: 'Limit', color: palette().axis, dash: [3, 5], points: false, width: 1.5 },
    ], [xs, ys, limit], { height: 180, zeroBase: true, yMin: DRINK_LIMIT * 1.4 });
    if (!any) emptyNote(el, 'All sober in this range.');
  }

  function buildExercises(el) {
    var ex = exById[state.ex];
    if (!ex) return emptyNote(el, 'No logged lifts yet.');
    var pts = ex.points.filter(function (p) { return ts(p.date) >= minTs(); });
    var meta = root.querySelector('[data-ex-meta]');
    var last = ex.points[ex.points.length - 1].date;
    meta.textContent = ex.points.length + ' session' + (ex.points.length === 1 ? '' : 's')
      + ' · last done ' + fmtDate(ts(last))
      + ' · ' + (exIndex() + 1) + ' of ' + EX_ORDER.length;
    if (!pts.length) return emptyNote(el, 'No sessions in this range — widen the range to see this lift.');
    makeChart(el, [{ label: ex.name, color: SLOTS[theme()][0] }], [
      pts.map(function (p) { return ts(p.date); }),
      pts.map(function (p) { return p[state.metric]; }),
    ], { height: 240 });
  }

  var BUILDERS = { weight: buildWeight, drinks: buildDrinks, exercises: buildExercises };
  var HAS_DATA = {
    weight: DATA.weight.length > 0,
    drinks: DATA.drinks.length > 0,
    exercises: DATA.exercises.length > 0,
  };

  /* ---------- render ------------------------------------------------------ */
  function render() {
    // Rebuilding empties every chart container, so mid-render the page is
    // briefly short enough that the browser clamps the scroll position — which
    // is why cycling exercises used to throw you back to the top. Charts are
    // rebuilt synchronously, so restoring the offset afterwards is invisible;
    // a genuinely shorter page (a panel switched off) just clamps as normal.
    var scrollY = window.scrollY;
    teardown();
    var anyShown = false;
    ['weight', 'exercises', 'drinks'].forEach(function (key) {
      var section = root.querySelector('[data-panel-el="' + key + '"]');
      var el = section.querySelector('[data-chart]');
      el.innerHTML = '';
      var show = state.panels[key] && HAS_DATA[key];
      section.hidden = !show;
      if (show) { anyShown = true; BUILDERS[key](el); }
    });
    root.querySelector('[data-graphs-empty]').hidden = anyShown ||
      HAS_DATA.weight || HAS_DATA.drinks || HAS_DATA.exercises;
    syncChips();
    if (window.scrollY !== scrollY) window.scrollTo(0, scrollY);
  }

  /* ---------- controls ---------------------------------------------------- */
  function syncChips() {
    root.querySelectorAll('[data-range]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(Number(b.dataset.range) === state.range));
    });
    var panelColor = { weight: WEIGHT_COLOR[theme()], drinks: UNDER_COLOR[theme()], exercises: '' };
    root.querySelectorAll('[data-panel]').forEach(function (b) {
      var key = b.dataset.panel;
      b.hidden = !HAS_DATA[key];
      b.setAttribute('aria-pressed', String(!!state.panels[key]));
      if (panelColor[key]) b.style.setProperty('--dot', panelColor[key]);
      else b.querySelector('.gdot').style.display = 'none';
    });
    if (exPick && state.ex != null) exPick.value = String(state.ex);
  }

  root.querySelector('[data-range-chips]').addEventListener('click', function (e) {
    var b = e.target.closest('[data-range]');
    if (!b) return;
    state.range = Number(b.dataset.range);
    persist();
    render();
  });

  root.querySelector('[data-panel-chips]').addEventListener('click', function (e) {
    var b = e.target.closest('[data-panel]');
    if (!b) return;
    state.panels[b.dataset.panel] = !state.panels[b.dataset.panel];
    persist();
    render();
  });

  var goalToggle = root.querySelector('[data-goal-toggle]');
  var goalForm = root.querySelector('[data-goal-form]');
  if (goalToggle && goalForm) {
    goalToggle.addEventListener('click', function () { goalForm.hidden = !goalForm.hidden; });
  }

  root.querySelector('[data-ex-metric]').value = state.metric;
  root.querySelector('[data-ex-metric]').addEventListener('change', function (e) {
    state.metric = e.target.value;
    persist();
    render();
  });

  // Exercise cycler — the dropdown lists every lift in cycle order (most
  // recently trained first); the arrows (and ←/→ off a text field) walk it,
  // wrapping at the ends.
  var exPick = root.querySelector('[data-ex-pick]');
  EX_ORDER.forEach(function (ex) {
    var o = document.createElement('option');
    o.value = String(ex.exercise_id);
    o.textContent = ex.name;
    exPick.appendChild(o);
  });
  exPick.addEventListener('change', function () {
    state.ex = Number(exPick.value);
    persist();
    render();
  });
  function cycleEx(step) {
    if (!EX_ORDER.length) return;
    var i = (exIndex() + step + EX_ORDER.length) % EX_ORDER.length;
    state.ex = EX_ORDER[i].exercise_id;
    persist();
    render();
  }
  root.querySelector('[data-ex-prev]').addEventListener('click', function () { cycleEx(-1); });
  root.querySelector('[data-ex-next]').addEventListener('click', function () { cycleEx(1); });
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    if (t && (t.isContentEditable || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
    if (root.querySelector('[data-panel-el="exercises"]').hidden) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); cycleEx(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); cycleEx(1); }
  });

  /* ---------- environment reactions --------------------------------------- */
  new MutationObserver(render).observe(document.documentElement,
    { attributes: true, attributeFilter: ['data-theme'] });

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      charts.forEach(function (c) {
        c.u.setSize({ width: c.el.clientWidth || 600, height: c.height });
      });
    }, 120);
  });

  render();
})();
