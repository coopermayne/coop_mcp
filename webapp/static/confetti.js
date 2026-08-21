/* Confetti — the app's one celebratory flourish, thrown when the /trainer card logs a
   personal best or an all-time-low weigh-in. window.Confetti.burst(el) and nothing else.
   Hand-written rather than vendored: the effect is a page of arithmetic, and everything
   else in static/vendor/ is there because it isn't.

   Driven by requestAnimationFrame on a canvas, NOT a CSS animation, for the reason the
   .rep-loop note in base.html records: iOS Safari pauses CSS animations (and GIFs) in
   Low Power Mode — which is exactly the phone standing in a gym. rAF keeps ticking.
   Canvas rather than a swarm of absolutely-positioned nodes: 70 elements restyled every
   frame is 70 style recalcs, against one composited layer. */
(function () {
  'use strict';

  // Mid-luminance only, so nothing has to branch on the theme: near-black disappears
  // against #0a0a0a and white disappears against #fff. These are the app's own colors —
  // the weigh-in typo guard's yellow, an amber step off it, the nutrient ring's sage,
  // and gray-400 — so a burst reads as this site celebrating rather than a party widget.
  var COLORS = ['#facc15', '#f59e0b', '#8ba58f', '#9ca3af'];
  var LIFE = 2000;      // ms a piece lives
  var FADE = 600;       // ms of that spent fading out
  var MAX = 240;        // hard cap across overlapping bursts

  var canvas = null, ctx = null, bits = [], raf = 0, last = 0;

  function reduced() {
    return !!(window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function ensureCanvas() {
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.id = 'confetti-canvas';
      canvas.setAttribute('aria-hidden', 'true');
      document.body.appendChild(canvas);
      ctx = canvas.getContext('2d');
    }
    // Re-measured at each burst rather than on a resize listener: a burst is the only
    // moment the size matters, and there's no listener to leave running between them.
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(window.innerWidth * dpr);
    canvas.height = Math.floor(window.innerHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return canvas;
  }

  function spawn(x, y, n) {
    for (var i = 0; i < n && bits.length < MAX; i++) {
      // An upward cone, so the pieces rise off the thing that earned them and fall back
      // through it rather than raining from the top of the screen.
      var a = -Math.PI / 2 + (Math.random() - 0.5) * 1.9;
      var sp = 7 + Math.random() * 7;
      bits.push({
        x: x, y: y,
        vx: Math.cos(a) * sp, vy: Math.sin(a) * sp,
        w: 5 + Math.random() * 4, h: 3 + Math.random() * 2,
        rot: Math.random() * Math.PI, spin: (Math.random() - 0.5) * 0.32,
        color: COLORS[(Math.random() * COLORS.length) | 0],
        t: 0,
      });
    }
  }

  function frame(now) {
    // Scale by elapsed time, not by frame: otherwise a 120Hz phone runs the whole burst
    // in half the seconds a 60Hz laptop takes.
    var dt = last ? Math.min(now - last, 64) : 16.67;
    last = now;
    var k = dt / 16.67;
    var h = window.innerHeight, w = window.innerWidth;

    ctx.clearRect(0, 0, w, h);
    var alive = [];
    for (var i = 0; i < bits.length; i++) {
      var b = bits[i];
      b.t += dt;
      b.vy += 0.30 * k;
      b.vx *= Math.pow(0.986, k);
      b.vy *= Math.pow(0.986, k);
      b.x += b.vx * k;
      b.y += b.vy * k;
      b.rot += b.spin * k;
      if (b.t > LIFE || b.y - b.h > h) continue;
      alive.push(b);

      ctx.save();
      ctx.globalAlpha = b.t > LIFE - FADE ? Math.max(0, (LIFE - b.t) / FADE) : 1;
      ctx.translate(b.x, b.y);
      ctx.rotate(b.rot);
      ctx.fillStyle = b.color;
      // Squashing the height by the rotation is what makes a piece read as a flat scrap
      // turning edge-on, rather than a spinning brick.
      var hh = b.h * Math.abs(Math.cos(b.rot * 1.7));
      ctx.fillRect(-b.w / 2, -hh / 2, b.w, Math.max(hh, 0.7));
      ctx.restore();
    }
    bits = alive;

    if (!bits.length) {
      // Nothing left to draw: stop the loop dead and hide the canvas. No idle treadmill.
      ctx.clearRect(0, 0, w, h);
      canvas.removeAttribute('data-on');
      raf = 0; last = 0;
      return;
    }
    raf = requestAnimationFrame(frame);
  }

  // Returns whether anything was actually thrown, so a caller can tell the difference
  // between "celebrating" and "declined to". Holding a page open for an animation that
  // was never going to play is a dead wait for exactly the person who asked for less
  // motion — the one caller who does that (weight.js's reload) reads this.
  function burst(el) {
    // The one opt-out, matching .cal-zoom and the rep-loop: someone who's asked for less
    // motion gets none, and the caller doesn't have to know that.
    if (reduced() || document.hidden) return false;
    var c = ensureCanvas();
    var x = window.innerWidth / 2, y = window.innerHeight / 2;
    if (el && el.getBoundingClientRect) {
      var r = el.getBoundingClientRect();
      // The canvas is position:fixed, so viewport coordinates need no scroll math.
      if (r.width || r.height) { x = r.left + r.width / 2; y = r.top + r.height / 2; }
    }
    spawn(x, y, window.innerWidth < 480 ? 50 : 70);
    c.setAttribute('data-on', '1');
    // A second burst mid-flight just adds to the same swarm — one loop runs throughout.
    if (!raf) { last = 0; raf = requestAnimationFrame(frame); }
    return true;
  }

  window.Confetti = { burst: burst };
})();
