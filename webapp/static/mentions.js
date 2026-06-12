// Inline mention resolver — shared by the pending queue and the entry page.
// Any element with a `data-mention` attribute (JSON: {id, surface, candidates})
// becomes a trigger that opens a small modal to pin that mention to a person:
//   - click a candidate / searched person to LINK it, or
//   - create a brand-new person and link, or
//   - dismiss a stray mention (delete the row).
// All write-through to /mention/* (server.py's website-only helpers). On success
// we just reload — the server is the one render path.
(function () {
  "use strict";

  // Base mount prefix ('/app' embedded, '' standalone): derive from our own src
  // so links resolve in both modes (same trick the templates use server-side).
  var BASE = (function () {
    var s = document.currentScript || document.querySelector('script[src$="mentions.js"]');
    var src = s ? s.getAttribute("src") : "";
    return src.replace(/\/static\/mentions\.js.*$/, "");
  })();

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  async function post(url, body) {
    var res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    var data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  // ---- the modal (built once, reused) ------------------------------------ //
  var overlay, box, state = null;

  function close() {
    if (overlay) overlay.style.display = "none";
    state = null;
  }

  function build() {
    overlay = el("div");
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;" +
      "z-index:60;align-items:flex-start;justify-content:center;padding:5vh 1rem;overflow:auto;";
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    box = el("div", "bg-white w-full max-w-md rounded-[4px] border border-gray-200 shadow-lg");
    box.style.cssText += "max-height:90vh;overflow:auto;";
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.style.display !== "none") close();
    });
  }

  function err(msg) {
    var e = box.querySelector("[data-err]");
    if (e) e.textContent = msg || "";
  }

  function personButton(p) {
    // p: {person_id, name, role?}
    var label = p.name + (p.role ? " · " + p.role : "");
    var b = el("button", "set-pill hover:border-black transition-colors", label);
    b.type = "button";
    b.dataset.pid = p.person_id;
    b.addEventListener("click", function () { linkOne(p.person_id); });
    return b;
  }

  async function linkOne(pid) {
    try {
      err("");
      await post(BASE + "/mention/" + state.id + "/resolve", {
        person_id: pid,
        learn_alias: box.querySelector("[data-learn]").checked,
      });
      location.reload();
    } catch (e) { err(e.message); }
  }

  var searchTimer = null;
  function onSearch(input, results) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async function () {
      var q = input.value.trim();
      results.innerHTML = "";
      if (!q) return;
      try {
        var res = await fetch(BASE + "/mention/people-search?q=" + encodeURIComponent(q));
        var data = await res.json();
        (data.people || []).forEach(function (p) { results.appendChild(personButton(p)); });
        if (!(data.people || []).length) results.appendChild(el("span", "text-xs text-gray-400", "No matches."));
      } catch (e) { err(e.message); }
    }, 200);
  }

  function open(trigger) {
    var data = JSON.parse(trigger.getAttribute("data-mention"));
    state = { id: data.id };
    box.innerHTML = "";

    var head = el("div", "px-5 py-4 border-b border-gray-100");
    head.appendChild(el("p", "text-[10px] uppercase tracking-widest text-gray-400 mb-1", "Resolve mention"));
    head.appendChild(el("p", "text-sm font-medium", "“" + data.surface + "”"));
    box.appendChild(head);

    var body = el("div", "px-5 py-4 space-y-4");
    box.appendChild(body);

    // candidates + search results share one container
    var lblPeople = el("p", "text-[10px] uppercase tracking-widest text-gray-400 mb-2", "People");
    body.appendChild(lblPeople);
    var people = el("div", "flex flex-wrap gap-2");
    (data.candidates || []).forEach(function (c) {
      people.appendChild(personButton({ person_id: c.person_id, name: c.name, role: c.role }));
    });
    if (!(data.candidates || []).length) people.appendChild(el("span", "text-xs text-gray-400", "No close matches — search or add below."));
    body.appendChild(people);

    // search
    var search = el("input", "w-full border border-gray-200 rounded-[3px] px-3 py-1.5 text-sm");
    search.placeholder = "Search other people…";
    body.appendChild(search);
    var results = el("div", "flex flex-wrap gap-2");
    body.appendChild(results);
    search.addEventListener("input", function () { onSearch(search, results); });

    // new person
    var newWrap = el("div", "pt-3 border-t border-gray-100 space-y-2");
    newWrap.appendChild(el("p", "text-[10px] uppercase tracking-widest text-gray-400", "Or add a new person"));
    var nameIn = el("input", "w-full border border-gray-200 rounded-[3px] px-3 py-1.5 text-sm");
    nameIn.placeholder = "Name";
    var roleIn = el("input", "w-full border border-gray-200 rounded-[3px] px-3 py-1.5 text-sm");
    roleIn.placeholder = "Role (e.g. father, coworker) — optional";
    var addBtn = el("button", "set-pill hover:border-black transition-colors", "Create & link");
    addBtn.type = "button";
    addBtn.addEventListener("click", async function () {
      var name = nameIn.value.trim();
      if (!name) { err("Enter a name."); return; }
      try {
        err("");
        await post(BASE + "/mention/" + state.id + "/new-person", {
          canonical_name: name,
          role: roleIn.value.trim(),
          learn_alias: box.querySelector("[data-learn]").checked,
        });
        location.reload();
      } catch (e) { err(e.message); }
    });
    newWrap.appendChild(nameIn);
    newWrap.appendChild(roleIn);
    newWrap.appendChild(addBtn);
    body.appendChild(newWrap);

    // learn-alias option
    var learnWrap = el("label", "flex items-center gap-2 text-xs text-gray-500 cursor-pointer pt-2");
    var learnCb = el("input"); learnCb.type = "checkbox"; learnCb.checked = true;
    learnCb.setAttribute("data-learn", "");
    learnWrap.appendChild(learnCb);
    learnWrap.appendChild(el("span", null, "Remember “" + data.surface + "” as a name for this person"));
    body.appendChild(learnWrap);

    // error line
    body.appendChild(el("p", "text-xs text-red-500", "")).setAttribute("data-err", "");

    // footer
    var foot = el("div", "px-5 py-3 border-t border-gray-100 flex items-center justify-between");
    var dismiss = el("button", "text-xs uppercase tracking-widest text-red-400 hover:text-red-600 transition-colors", "Dismiss");
    dismiss.type = "button";
    dismiss.addEventListener("click", async function () {
      try {
        err("");
        await post(BASE + "/mention/" + state.id + "/dismiss", {});
        location.reload();
      } catch (e) { err(e.message); }
    });
    var closeBtn = el("button", "text-xs uppercase tracking-widest text-gray-400 hover:text-black transition-colors", "Close");
    closeBtn.type = "button";
    closeBtn.addEventListener("click", close);
    foot.appendChild(dismiss);
    foot.appendChild(closeBtn);
    box.appendChild(foot);

    overlay.style.display = "flex";
  }

  function init() {
    if (!document.querySelector("[data-mention]")) return;
    build();
    document.addEventListener("click", function (e) {
      var t = e.target.closest("[data-mention]");
      if (t) { e.preventDefault(); open(t); }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
