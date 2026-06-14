/*
 * Reusable chat client for the in-app AI surfaces (journal panel now, trainer
 * page later). Wires one chat widget inside a root element: a streaming POST to
 * `${endpoint}/send` whose SSE frames append assistant text and tool chips, plus
 * a `${endpoint}/reset` for "new chat". No framework — matches the app.
 *
 *   ChatWidget.init({ root, endpoint, extra });
 *
 * `extra` (optional) is an object merged into every /send and /reset request body —
 * the profile-page panel passes { person_id } so the server pins the thread to that
 * person. `onWrite` (optional) fires after a turn that wrote to the DB.
 *
 * The root must contain elements tagged with these data attributes:
 *   [data-chat-log]    scrollable transcript container
 *   [data-chat-form]   the composer <form>
 *   [data-chat-input]  the <textarea>
 *   [data-chat-send]   the submit <button>
 *   [data-chat-reset]  optional "new chat" trigger
 *   [data-chat-seed]   optional first-prompt hint, removed on first send
 */
(function () {
  function init(opts) {
    var root = opts.root;
    var endpoint = opts.endpoint; // e.g. "/app/chat/journal"
    var log = root.querySelector('[data-chat-log]');
    var form = root.querySelector('[data-chat-form]');
    var input = root.querySelector('[data-chat-input]');
    var sendBtn = root.querySelector('[data-chat-send]');
    var resetBtn = root.querySelector('[data-chat-reset]');
    var seed = root.querySelector('[data-chat-seed]');
    var extra = opts.extra || {}; // merged into /send and /reset bodies (e.g. person_id)
    var busy = false;

    if (root.dataset.chatReady) return; // idempotent
    root.dataset.chatReady = '1';

    function autosize() {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    }
    input.addEventListener('input', autosize);

    // Enter sends; Shift+Enter inserts a newline.
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });

    // Keep the panel within the on-screen (visual) viewport when the mobile
    // software keyboard opens. The panel is `position: fixed` to the LAYOUT
    // viewport (full height, anchored bottom), so without this, focusing the
    // composer makes the browser scroll the header + transcript up behind the
    // keyboard — only the input stays visible. We lift the panel's bottom edge
    // to sit just above the keyboard so the whole panel stays in view, then
    // clear it again when the keyboard closes (CSS `bottom-0` takes back over).
    var vv = window.visualViewport;
    if (vv) {
      var fitToViewport = function () {
        var kb = window.innerHeight - vv.height - vv.offsetTop;
        root.style.bottom = kb > 0 ? kb + 'px' : '';
        if (kb > 0) scrollDown();
      };
      vv.addEventListener('resize', fitToViewport);
      vv.addEventListener('scroll', fitToViewport);
    }

    function scrollDown() { log.scrollTop = log.scrollHeight; }

    function bubble(role) {
      var wrap = document.createElement('div');
      if (role === 'user') {
        wrap.className = 'flex justify-end';
        var b = document.createElement('div');
        b.className = 'max-w-[85%] bg-black text-white rounded-[4px] px-3.5 py-2 text-[15px] leading-relaxed whitespace-pre-wrap break-words';
        wrap.appendChild(b);
        log.appendChild(wrap);
        return b;
      }
      wrap.className = 'space-y-2';
      log.appendChild(wrap);
      return wrap; // assistant container: holds tool chips + a text node
    }

    function chip(ev) {
      var el = document.createElement(ev.href ? 'a' : 'span');
      el.className = 'inline-flex items-center gap-1.5 text-[11px] rounded-[3px] px-2 py-1 border transition-colors '
        + (ev.kind === 'write'
            ? 'border-gray-300 text-gray-700 hover:border-black hover:text-black'
            : 'border-gray-100 text-gray-400');
      if (ev.href) el.href = ev.href;
      var dot = document.createElement('span');
      dot.className = 'inline-block w-1.5 h-1.5 rounded-full ' + (ev.kind === 'write' ? 'bg-black' : 'bg-gray-300');
      el.appendChild(dot);
      el.appendChild(document.createTextNode(ev.summary || ev.name));
      return el;
    }

    function assistantText(container) {
      var p = container.querySelector('[data-text]');
      if (!p) {
        p = document.createElement('div');
        p.setAttribute('data-text', '');
        p._raw = '';
        p.className = 'chat-md text-[15px] text-gray-800 leading-relaxed break-words';
        container.appendChild(p);
      }
      return p;
    }

    // Render the accumulated raw markdown into the element. Falls back to plain
    // text (preserving newlines) if the markdown lib hasn't loaded.
    function renderMd(el) {
      var raw = el._raw || '';
      if (window.marked && window.marked.parse) {
        // Turn any over-escaped literal "\n" (backslash-n) back into real newlines
        // so a paragraph break renders as a break, not visible escape text.
        raw = raw.replace(/\\r\\n|\\n|\\r/g, '\n');
        el.innerHTML = window.marked.parse(raw, { breaks: true });
        el.querySelectorAll('a').forEach(function (a) {
          a.target = '_blank'; a.rel = 'noopener noreferrer';
        });
      } else {
        el.style.whiteSpace = 'pre-wrap';
        el.textContent = raw;
      }
    }

    function setBusy(b) {
      busy = b;
      sendBtn.disabled = b;
      input.disabled = b;
    }

    async function send(text) {
      if (busy) return; // ignore re-entrant sends (e.g. a programmatic send mid-turn)
      if (seed) { seed.remove(); seed = null; }
      var userBubble = bubble('user');
      userBubble.textContent = text;
      scrollDown();
      var container = bubble('assistant');
      setBusy(true);

      var res;
      try {
        res = await fetch(endpoint + '/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.assign({ text: text }, extra)),
        });
      } catch (e) {
        assistantText(container).textContent = 'Network error: ' + e.message;
        setBusy(false); return;
      }
      // If the session timed out while composing (the journal idle-relock, or the
      // Google login expiring), the guarded /send is bounced to /lock or /login —
      // fetch follows the redirect and hands back HTML, not our event stream.
      // `res.redirected` flags exactly that. Recover instead of dropping the message:
      // pull the two optimistic bubbles back, restore the text so nothing is lost,
      // and tell the user. (The keepalive ping normally prevents this entirely.)
      if (res.redirected) {
        if (userBubble.parentNode) userBubble.parentNode.remove();
        container.remove();
        input.value = text;
        autosize();
        setBusy(false);
        input.focus();
        alert('Your session timed out while you were typing. The message was kept in the box — unlock the journal (or sign back in), then send it again.');
        return;
      }
      if (!res.ok || !res.body) {
        assistantText(container).textContent = 'Error: ' + res.status;
        setBusy(false); return;
      }

      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      var wrote = false; // did any write tool fire this turn?
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var frames = buf.split('\n\n');
        buf = frames.pop();
        for (var i = 0; i < frames.length; i++) {
          var line = frames[i].trim();
          if (line.slice(0, 5) !== 'data:') continue;
          var payload = line.replace(/^data:\s*/, '');
          if (!payload) continue;
          var ev;
          try { ev = JSON.parse(payload); } catch (e) { continue; }
          if (ev.type === 'text') {
            var t = assistantText(container);
            t._raw += ev.text;
            renderMd(t);
          } else if (ev.type === 'tool') {
            if (ev.kind === 'write') wrote = true;
            var p = container.querySelector('[data-text]');
            if (p) container.insertBefore(chip(ev), p);
            else container.appendChild(chip(ev));
          } else if (ev.type === 'error') {
            var e1 = assistantText(container);
            e1._raw += (e1._raw ? '\n\n' : '') + '⚠ ' + ev.message;
            renderMd(e1);
            // Drop the gray class first: the dark-mode override for text-gray-800
            // outranks the bare text-red-500 utility, so red only wins once gray
            // is gone.
            e1.classList.remove('text-gray-800');
            e1.classList.add('text-red-500');
          }
          scrollDown();
        }
      }
      setBusy(false);
      input.focus();
      // A write landed in the DB — let the host page refresh its live regions
      // (e.g. the journal feed behind the panel) without a full reload.
      if (wrote && typeof opts.onWrite === 'function') opts.onWrite();
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (busy) return;
      var text = input.value.trim();
      if (!text) return;
      input.value = '';
      autosize();
      send(text);
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', async function () {
        if (busy) return;
        try {
          await fetch(endpoint + '/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(extra),
          });
        } catch (e) {}
        log.innerHTML = '';
      });
    }

    // Replay any still-active server-side thread for this session so a reload shows
    // a continuing conversation instead of a blank panel (the transcript lives on
    // the server, keyed by the session cookie — not in the browser). Runs once on
    // init; a new Pacific day returns an empty thread (the server rolls it over).
    function renderTurn(turn) {
      if (turn.role === 'user') {
        bubble('user').textContent = turn.text || '';
        return;
      }
      var container = bubble('assistant');
      (turn.chips || []).forEach(function (ch) { container.appendChild(chip(ch)); });
      if (turn.text) {
        var p = assistantText(container);
        p._raw = turn.text;
        renderMd(p);
      }
    }

    async function loadHistory() {
      var data;
      try {
        var res = await fetch(endpoint + '/history', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(extra),
        });
        if (!res.ok) return;
        data = await res.json();
      } catch (e) { return; }
      var turns = (data && data.turns) || [];
      if (!turns.length) return;
      if (seed) { seed.remove(); seed = null; }
      var note = document.createElement('p');
      note.className = 'text-[11px] uppercase tracking-widest text-gray-400 text-center';
      note.textContent = 'Continuing your conversation — “New” to start fresh';
      log.appendChild(note);
      turns.forEach(renderTurn);
      scrollDown();
    }
    loadHistory();

    // Expose a small handle so the host page can drive the chat (e.g. the trainer
    // plan card's "Replace" action sends a prefilled request to the AI).
    return { send: send };
  }

  window.ChatWidget = { init: init };
})();
