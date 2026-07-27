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
  var DRINKS_COLOR = { light: SLOTS.light[1], dark: SLOTS.dark[1] };

  /* ---------- state (persisted) ----------------------------------------- */
  var STORE_KEY = 'graphs.state';
  var state = { range: 90, panels: { weight: true, drinks: true, exercises: true },
                sel: null, metric: 'top' };
  try {
    var saved = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (saved && typeof saved === 'object') {
      if (typeof saved.range === 'number') state.range = saved.range;
      if (saved.panels) state.panels = Object.assign(state.panels, saved.panels);
      if (Array.isArray(saved.sel)) state.sel = saved.sel;
      if (saved.metric) state.metric = saved.metric;
    }
  } catch (e) {}
  function persist() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) {}
  }

  var exById = {};
  DATA.exercises.forEach(function (ex) { exById[ex.exercise_id] = ex; });
  // Selection is an 8-slot array (index = color slot); a removed exercise leaves
  // a null hole so the survivors KEEP their colors — color follows the entity,
  // not its position in the list. Default: the current rotation.
  function selCount() { return state.sel.filter(function (x) { return x != null; }).length; }
  if (state.sel) {
    state.sel = state.sel.map(function (id) { return exById[id] ? id : null; });
  }
  if (!state.sel || !selCount()) {
    state.sel = DATA.rotation_ids.filter(function (id) { return exById[id]; }).slice(0, 8);
  }
  if (!selCount()) {   // no rotation yet — start with the most-trained lifts
    state.sel = DATA.exercises.slice()
      .sort(function (a, b) { return b.points.length - a.points.length; })
      .slice(0, 8).map(function (ex) { return ex.exercise_id; });
  }
  state.sel = state.sel.slice(0, 8);

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
            if (opts.zeroBase) return [0, max * 1.1 || 1];
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
      series: [{ label: 'Date', value: function (u_, v) { return v == null ? '—' : fmtDate(v); } }]
        .concat(series.map(function (s) {
          return {
            label: s.label,
            stroke: s.color,
            width: 2,
            spanGaps: true,
            points: { show: s.points !== false, size: 5, fill: s.color },
            value: function (u_, v) { return v == null ? '—' : fmtNum(v); },
          };
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
    makeChart(el, [{ label: 'Bodyweight', color: WEIGHT_COLOR[theme()] }], [
      pts.map(function (p) { return ts(p.date); }),
      pts.map(function (p) { return p.lbs; }),
    ], { height: 180 });
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
    makeChart(el, [{ label: 'Drinks', color: DRINKS_COLOR[theme()], points: false }],
              [xs, ys], { height: 180, zeroBase: true });
    if (!any) emptyNote(el, 'All sober in this range.');
  }

  function buildExercises(el) {
    if (!selCount()) return emptyNote(el, 'Pick an exercise below.');
    var pal = palette();
    // Shared x: the union of session dates across the selected lifts; each
    // series is null where it wasn't trained (spanGaps connects across).
    var dateSet = {};
    var picked = [];
    state.sel.forEach(function (id, i) {
      if (id == null) return;
      var ex = exById[id];
      var pts = ex.points.filter(function (p) { return ts(p.date) >= minTs(); });
      pts.forEach(function (p) { dateSet[ts(p.date)] = true; });
      picked.push({ ex: ex, pts: pts, color: pal.slots[i] });
    });
    var xs = Object.keys(dateSet).map(Number).sort(function (a, b) { return a - b; });
    if (!xs.length) return emptyNote(el, 'No sessions in this range.');
    var series = [], cols = [];
    picked.forEach(function (p) {
      var byTs = {};
      p.pts.forEach(function (pt) { byTs[ts(pt.date)] = pt[state.metric]; });
      series.push({ label: p.ex.name, color: p.color });
      cols.push(xs.map(function (t) { return byTs[t] != null ? byTs[t] : null; }));
    });
    makeChart(el, series, [xs].concat(cols), { height: 240 });
  }

  var BUILDERS = { weight: buildWeight, drinks: buildDrinks, exercises: buildExercises };
  var HAS_DATA = {
    weight: DATA.weight.length > 0,
    drinks: DATA.drinks.length > 0,
    exercises: DATA.exercises.length > 0,
  };

  /* ---------- render ------------------------------------------------------ */
  function render() {
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
  }

  /* ---------- controls ---------------------------------------------------- */
  function syncChips() {
    root.querySelectorAll('[data-range]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(Number(b.dataset.range) === state.range));
    });
    var pal = palette();
    var panelColor = { weight: WEIGHT_COLOR[theme()], drinks: DRINKS_COLOR[theme()], exercises: '' };
    root.querySelectorAll('[data-panel]').forEach(function (b) {
      var key = b.dataset.panel;
      b.hidden = !HAS_DATA[key];
      b.setAttribute('aria-pressed', String(!!state.panels[key]));
      if (panelColor[key]) b.style.setProperty('--dot', panelColor[key]);
      else b.querySelector('.gdot').style.display = 'none';
    });
    root.querySelectorAll('[data-ex]').forEach(function (b) {
      var id = Number(b.dataset.ex);
      var i = state.sel.indexOf(id);
      b.setAttribute('aria-pressed', String(i !== -1));
      b.style.setProperty('--dot', i !== -1 ? pal.slots[i] : 'transparent');
    });
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

  root.querySelector('[data-ex-metric]').value = state.metric;
  root.querySelector('[data-ex-metric]').addEventListener('change', function (e) {
    state.metric = e.target.value;
    persist();
    render();
  });

  // Exercise picker chips — most-trained first so the useful ones are near the top.
  var chipWrap = root.querySelector('[data-ex-chips]');
  var hint = root.querySelector('[data-ex-hint]');
  DATA.exercises
    .slice()
    .sort(function (a, b) { return b.points.length - a.points.length || a.name.localeCompare(b.name); })
    .forEach(function (ex) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'gchip';
      b.dataset.ex = ex.exercise_id;
      b.title = ex.points.length + ' session' + (ex.points.length === 1 ? '' : 's');
      var dot = document.createElement('span');
      dot.className = 'gdot';
      b.appendChild(dot);
      b.appendChild(document.createTextNode(ex.name));
      chipWrap.appendChild(b);
    });
  chipWrap.addEventListener('click', function (e) {
    var b = e.target.closest('[data-ex]');
    if (!b) return;
    var id = Number(b.dataset.ex);
    var i = state.sel.indexOf(id);
    if (i !== -1) {
      state.sel[i] = null;   // leave the hole so the others keep their colors
      while (state.sel.length && state.sel[state.sel.length - 1] == null) state.sel.pop();
    } else {
      var hole = state.sel.indexOf(null);
      if (hole !== -1) state.sel[hole] = id;
      else if (state.sel.length < 8) state.sel.push(id);
      else { hint.hidden = false; return; }
    }
    hint.hidden = selCount() < 8;
    persist();
    render();
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
