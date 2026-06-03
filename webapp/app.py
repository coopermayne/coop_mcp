"""
Read-only web frontend for the journal.

Reads the same SQLite DB as the MCP server (`server.py`) and reuses its retrieval
functions via `data.py`. Never writes. Capture, resolution and coaching stay in
the conversation with Claude; this is purely for browsing what's been recorded.

Auth mirrors the MCP server: Google OAuth (browser flow) gated by an email
allowlist. If GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset the app runs
authless — local-dev / dummy-data only.

Run:
    JOURNAL_DB=./journal.db python webapp/app.py            # http://localhost:8001
Env: PORT, WEB_HOST, WEB_BASE_URL (public origin, for the OAuth redirect),
     SESSION_SECRET, GOOGLE_CLIENT_ID/SECRET, JOURNAL_ALLOWED_EMAILS, JOURNAL_DB.
"""

import html
import json
import os
import re
import sys
from datetime import datetime

from markupsafe import Markup

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_dotenv():
    """Minimal, zero-dependency `.env` loader for local dev. Reads `KEY=value`
    lines from a `.env` at the project root (`.env` is git-ignored, so it's the
    safe home for secrets like ANTHROPIC_API_KEY). A *non-empty* existing env var
    always wins, so a real shell `export` or a Coolify-set var is never overridden
    — but a present-but-blank var (e.g. `ANTHROPIC_API_KEY=''` exported by some
    shells) is treated as unset so `.env` can fill it. Must run before the imports
    below read their config (e.g. chat.ENABLED)."""
    path = os.path.join(ROOT, ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if not os.environ.get(key):  # unset OR present-but-empty
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_dotenv()

import data            # noqa: E402  local data layer
import server          # noqa: E402  reused for db()/reads and ALLOWED_EMAILS
import chat            # noqa: E402  in-app AI chat agent loop (the one write path)

from fastapi import FastAPI, Request                          # noqa: E402
from fastapi.responses import RedirectResponse, Response       # noqa: E402
from fastapi.staticfiles import StaticFiles                   # noqa: E402
from fastapi.templating import Jinja2Templates                # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware       # noqa: E402
from starlette.middleware.sessions import SessionMiddleware    # noqa: E402

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
# Public origin used to build the OAuth redirect. Defaults to the MCP server's
# PUBLIC_URL (same bare origin) so there's no duplicate var to set.
WEB_BASE_URL = (os.environ.get("WEB_BASE_URL") or os.environ.get("PUBLIC_URL") or "").rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
# Whether the nav shows the logout icon. On automatically with auth; SHOW_LOGOUT=1
# forces it on in authless dev so the icon can be previewed (logout still works —
# it clears the session and redirects to /login, which bounces to home when auth
# is off).
SHOW_LOGOUT = AUTH_ENABLED or os.environ.get("SHOW_LOGOUT", "").lower() in ("1", "true", "yes")
ALLOWED_EMAILS = server.ALLOWED_EMAILS  # reuse the MCP server's allowlist

app = FastAPI(title="Journal")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


# --------------------------------------------------------------------------- #
# Template filters
# --------------------------------------------------------------------------- #

def _parse(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def long_date(d):
    dt = _parse(d)
    return dt.strftime("%A, %B %-d, %Y") if dt else (d or "")


def short_date(d):
    dt = _parse(d)
    return dt.strftime("%b %-d, %Y") if dt else (d or "")


def weekday(d):
    dt = _parse(d)
    return dt.strftime("%a") if dt else ""


def num(x):
    if x is None:
        return ""
    return f"{x:g}"


def dur_label(seconds):
    """Compact duration: '45m', '1h05m', '30s'."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


def set_label(s):
    w, r = s.get("weight_lbs"), s.get("reps")
    rpe = s.get("rpe")
    dur, dist = s.get("duration_seconds"), s.get("distance_miles")
    if w is not None and r is not None:
        base = f"{num(w)} × {r}"
    elif r is not None:
        base = f"{r} rep" + ("" if r == 1 else "s")
    elif w is not None:
        base = f"{num(w)} lb"
    elif dist is not None or dur is not None:
        # Cardio: distance and/or time, whichever is recorded.
        parts = []
        if dist is not None:
            parts.append(f"{num(dist)} mi")
        if dur is not None:
            parts.append(dur_label(dur))
        base = " · ".join(parts)
    else:
        base = "—"
    if rpe is not None:
        base += f"  @{num(rpe)}"
    return base


for _name, _fn in [
    ("long_date", long_date), ("short_date", short_date), ("weekday", weekday),
    ("num", num), ("set_label", set_label),
]:
    templates.env.filters[_name] = _fn


def linkify_people(body: str, people: list[dict], base: str) -> Markup:
    """Return the entry body as safe HTML with each resolved person's name linked
    inline to their page — quiet underlines that don't break the prose. Names are
    matched on word boundaries, case-insensitively, longest form first (so "Tom
    Brady" wins over "Tom"); every occurrence is linked. Text is HTML-escaped; only
    the anchors we build are markup."""
    if not body:
        return Markup("")
    form_to_person: dict[str, dict] = {}
    for p in people:
        for f in p.get("forms", []):
            f = (f or "").strip()
            if len(f) < 2:  # 1-char forms match too much
                continue
            form_to_person.setdefault(f.lower(), p)
    if not form_to_person:
        return Markup(html.escape(body))
    forms = sorted({fp for fp in form_to_person}, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(f) for f in forms) + r")\b",
                         re.IGNORECASE)
    out, last = [], 0
    for mobj in pattern.finditer(body):
        out.append(html.escape(body[last:mobj.start()]))
        word = mobj.group(0)
        person = form_to_person.get(word.lower())
        if person:
            label = person["name"] + (f" · {person['role']}" if person.get("role") else "")
            out.append(
                f'<a href="{base}/person/{person["person_id"]}" class="person-link" '
                f'title="{html.escape(label)}">{html.escape(word)}</a>'
            )
        else:
            out.append(html.escape(word))
        last = mobj.end()
    out.append(html.escape(body[last:]))
    return Markup("".join(out))


def base_path(request: Request) -> str:
    """The mount prefix this app is served under ('/app' when embedded in the MCP
    process, '' when run standalone). Internal links/redirects are prefixed with it."""
    return request.scope.get("root_path", "") or ""


def page(request: Request, template: str, active: str = "", status_code: int = 200, **ctx):
    ctx.update(active=active, auth_enabled=AUTH_ENABLED, show_logout=SHOW_LOGOUT,
               chat_enabled=chat.ENABLED,
               user=request.session.get("email"), base=base_path(request))
    return templates.TemplateResponse(request=request, name=template,
                                      context=ctx, status_code=status_code)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

# The PWA wiring (manifest + service worker) must be fetchable without auth: the
# browser pulls them itself, and an auth redirect to /login would hand back HTML
# instead, breaking install. They expose no journal data — just app metadata.
PUBLIC_PATHS = {"/login", "/auth/login", "/auth/callback", "/health",
                "/manifest.webmanifest", "/sw.js"}

oauth = None
if AUTH_ENABLED:
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        "google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


class RequireAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        # Match against the path *relative to the mount prefix* (root_path is
        # '/app' when embedded in the MCP process, '' when standalone).
        root = request.scope.get("root_path", "") or ""
        path = request.url.path
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        if request.session.get("email"):
            return await call_next(request)
        return RedirectResponse(root + "/login")


# SessionMiddleware must be outermost so request.session exists for RequireAuth.
app.add_middleware(RequireAuth)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   https_only=bool(WEB_BASE_URL.startswith("https")))


@app.get("/login")
async def login_page(request: Request):
    if not AUTH_ENABLED or request.session.get("email"):
        return RedirectResponse(base_path(request) + "/")
    return page(request, "login.html")


@app.get("/auth/login")
async def auth_login(request: Request):
    redirect_uri = (WEB_BASE_URL + base_path(request) + "/auth/callback") if WEB_BASE_URL \
        else str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        return page(request, "login.html", status_code=403,
                    error=f"{email or 'this account'} is not authorized for this journal.")
    request.session["email"] = email
    return RedirectResponse(base_path(request) + "/")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(base_path(request) + "/login")


@app.get("/health")
async def health():
    from fastapi.responses import JSONResponse
    try:
        with server.db() as conn:
            conn.execute("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as e:  # pragma: no cover
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


# --------------------------------------------------------------------------- #
# PWA — installable on phones (Add to Home Screen)
#
# Both the manifest and the service worker are generated per-request so their
# URLs carry the mount prefix (root_path is '/app' embedded, '' standalone).
# That keeps start_url / scope / icon paths correct in either deployment, and
# lets the service worker default-scope itself to the app root.
# --------------------------------------------------------------------------- #

@app.get("/manifest.webmanifest")
async def manifest(request: Request):
    from fastapi.responses import JSONResponse
    base = base_path(request)
    return JSONResponse({
        "name": "Journal",
        "short_name": "Journal",
        "description": "A private record — journal, training and drinking.",
        "start_url": f"{base}/",
        "scope": f"{base}/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "portrait",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            {"src": f"{base}/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": f"{base}/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": f"{base}/static/icon-maskable-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "maskable"},
            {"src": f"{base}/static/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")


# Service worker. Network-first for navigations (the journal is auth-gated and
# always live data, so we never cache authed HTML — only fall back to a baked
# offline page when the network is gone), stale-while-revalidate for same-origin
# static assets (icons/favicon). Cross-origin (Tailwind/Fonts CDNs) passes
# straight through. Bump VERSION to retire old caches on the next visit.
_SERVICE_WORKER_TMPL = """\
const VERSION = 'v2';
const CACHE = 'journal-' + VERSION;
const BASE = '__BASE__';
const PRECACHE = [
  BASE + '/static/icon-192.png',
  BASE + '/static/favicon.svg',
  BASE + '/manifest.webmanifest',
];
const OFFLINE_HTML = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">`
  + `<meta name="viewport" content="width=device-width, initial-scale=1">`
  + `<title>Offline</title><style>html{color-scheme:dark}`
  + `body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;`
  + `justify-content:center;gap:.5rem;background:#0a0a0a;color:#fafafa;`
  + `font-family:system-ui,sans-serif;text-align:center;padding:2rem}`
  + `p{color:#a3a3a3;font-size:.9rem;margin:0}h1{font-size:1.1rem;margin:0}</style></head>`
  + `<body><h1>You're offline</h1><p>Reconnect to read your journal.</p></body></html>`;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // let CDN/font requests pass

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match(req).then((hit) =>
          hit || new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html' } })
        )
      )
    );
    return;
  }

  // Only STATIC assets (icons/favicon/manifest) get the offline cache. Everything
  // else — notably the authed journal HTML the in-app chat re-fetches to live-update
  // the feed (a same-origin GET, but NOT a navigation, so it misses the rule above) —
  // must always hit the network. Caching it here served a stale page back on the next
  // refresh, so a freshly-added entry wouldn't appear until a full reload.
  const isStatic = url.pathname.startsWith(BASE + '/static/')
    || url.pathname === BASE + '/manifest.webmanifest';
  if (!isStatic) return;

  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((res) => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
"""


@app.get("/sw.js")
async def service_worker(request: Request):
    js = _SERVICE_WORKER_TMPL.replace("__BASE__", base_path(request))
    return Response(
        js,
        media_type="application/javascript",
        headers={
            # Default scope is the SW's own path; served at the app root that's
            # already the whole app. Allow a broader scope just in case, and keep
            # the worker itself uncached so VERSION bumps take effect promptly.
            "Service-Worker-Allowed": (base_path(request) or "") + "/",
            "Cache-Control": "no-cache",
        },
    )


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.get("/")
async def index(request: Request):
    # The journal is the landing page — there's no separate dashboard. "/app"
    # (and the bare origin) lands here.
    return RedirectResponse(base_path(request) + "/journal")


def _pending_count() -> int:
    with server.db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM mentions WHERE status='pending'"
        ).fetchone()["n"]


@app.get("/journal")
async def journal(request: Request, q: str = ""):
    q = (q or "").strip()
    base = base_path(request)
    pending_n = _pending_count()
    if q:
        res = server.search_entries(q, limit=40)
        entries = data.attach_people(res["results"])
        for e in entries:
            e["body_html"] = linkify_people(e["body"], e["people"], base)
        return page(request, "journal.html", active="journal",
                    q=q, entries=entries, count=res["count"], searching=True,
                    pending_count=pending_n)
    res = data.list_days(limit_entries=120)
    for day in res["days"]:
        for e in day["entries"]:
            e["body_html"] = linkify_people(e["body"], e["people"], base)
    months = data.calendar_months([d["date"] for d in res["days"]], today=server.today())
    return page(request, "journal.html", active="journal",
                q="", days=res["days"], count=res["total"], searching=False,
                pending_count=pending_n, months=months)


@app.get("/pending")
async def pending(request: Request):
    items = data.pending_mentions()
    return page(request, "pending.html", active="journal",
                items=items, count=len(items))


@app.get("/entry/{entry_id}")
async def entry(request: Request, entry_id: int):
    e = data.entry_with_people(entry_id)
    if e is None:
        return page(request, "notfound.html", active="journal",
                    status_code=404, what="entry")
    return page(request, "entry.html", active="journal", e=e)


@app.get("/workouts")
async def workouts(request: Request):
    sessions = data.workouts_full(limit=20)
    brief = server.get_fitness_briefing(recent_workouts=1)
    months = data.calendar_months([s["date"] for s in sessions], today=server.today())
    return page(request, "workouts.html", active="workouts",
                sessions=sessions,
                muscles=data.muscle_breakdown(),
                profile=brief.get("profile", {}),
                months=months)


@app.get("/trainer")
async def trainer(request: Request):
    """The trainer surface: the active workout plan (today's routine, tap-to-complete)
    plus the AI chat panel that builds and adjusts it. The plan card is rendered
    client-side from the bootstrapped JSON so chat-driven and tap-driven changes share
    one render path (static/trainer.js)."""
    return page(request, "trainer.html", active="trainer",
                plan=data.active_plan(),
                brief=server.get_fitness_briefing(recent_workouts=1))


@app.get("/trainer/library")
async def trainer_library(request: Request, muscle: str = "", q: str = "",
                          rotation: str = "", error: str = ""):
    """The exercise library: browse the whole catalog — muscles (by emphasis tier),
    equipment, level/mechanic, technique, and a form gif/video per exercise. Filterable
    by muscle, name, or `rotation` (the curated programming pool). Each row toggles in/out
    of the rotation. The user curates the closed catalog here: the add form
    (server.create_exercise) is the only way a new exercise enters it — the trainer chat
    can enrich technique but never creates one."""
    lib = data.exercise_library(muscle=muscle, q=q, rotation=bool(rotation))
    return page(request, "library.html", active="library", error=error, **lib)


@app.post("/trainer/library/add")
async def trainer_library_add(request: Request):
    """Manually add an exercise to the closed library — the ONLY creation path (the AI
    can't reach it). Calls server.create_exercise (defaults the new movement into the
    rotation) and redirects back (PRG). Muscle tiers arrive comma-separated; a blank name
    is rejected."""
    from urllib.parse import quote_plus
    form = await request.form()
    base = base_path(request)

    def _muscles(field):
        return [m.strip().lower() for m in (form.get(field) or "").split(",") if m.strip()]

    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse(base + "/trainer/library?error=Enter+an+exercise+name.",
                                status_code=303)
    res = server.create_exercise(
        name=name,
        in_rotation=True,  # a movement the user adds by hand is one they mean to train
        category=(form.get("category") or "").strip() or None,
        equipment=(form.get("equipment") or "").strip() or None,
        level=(form.get("level") or "").strip() or None,
        mechanic=(form.get("mechanic") or "").strip() or None,
        force=(form.get("force") or "").strip() or None,
        muscles=_muscles("muscles") or None,
        secondary_muscles=_muscles("secondary_muscles") or None,
        tertiary_muscles=_muscles("tertiary_muscles") or None,
        technique_notes=(form.get("technique_notes") or "").strip() or None,
        common_mistakes=(form.get("common_mistakes") or "").strip() or None,
        cautions=(form.get("cautions") or "").strip() or None,
        video_link=(form.get("video_link") or "").strip() or None,
        image_link=(form.get("image_link") or "").strip() or None,
    )
    if isinstance(res, dict) and res.get("error"):
        return RedirectResponse(base + "/trainer/library?error=" + quote_plus(res["error"]),
                                status_code=303)
    return RedirectResponse(base + "/trainer/library", status_code=303)


@app.post("/trainer/exercise/{exercise_id}/rotation")
async def trainer_set_rotation(request: Request, exercise_id: int):
    """Toggle one exercise in/out of the rotation (the library page's star button). Body:
    {"in_rotation": true|false}. Writes through server.set_rotation."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    res = server.set_rotation(exercise_id=exercise_id, in_rotation=bool(body.get("in_rotation")))
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


def _num(v):
    """Coerce a JSON value to float|None ('' / null -> None)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.get("/trainer/plan.json")
async def trainer_plan(request: Request):
    from fastapi.responses import JSONResponse
    return JSONResponse(data.active_plan())


@app.post("/trainer/set/{set_id}/complete")
async def trainer_complete_set(request: Request, set_id: int):
    """Tap-to-complete one planned set. Body: {weight_lbs?, reps?, rpe?, note?} —
    omitted numbers fall back to the set's targets server-side. Returns the updated
    plan so the card re-renders without a reload."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    reps = _num(body.get("reps"))
    res = server.complete_set(
        set_id,
        weight_lbs=_num(body.get("weight_lbs")),
        reps=int(reps) if reps is not None else None,
        rpe=_num(body.get("rpe")),
        note=(body.get("note") or "").strip() or None,
    )
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.post("/trainer/finish")
async def trainer_finish(request: Request):
    """Close out the active session. Body (optional): {feeling?, notes?}."""
    from fastapi.responses import JSONResponse
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    res = server.finish_workout(
        feeling=(body.get("feeling") or "").strip() or None,
        notes=(body.get("notes") or "").strip() or None,
    )
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.post("/trainer/set/{set_id}/update")
async def trainer_update_set(request: Request, set_id: int):
    """Correct an already-logged ('done') set from the plan card — fix a data-entry
    error without un-logging it. Body: {weight_lbs?, reps?, rpe?}. Saving with reps BLANK
    clears the set instead (revert a planned set to pending, or drop an ad-hoc one), since
    update_set can't blank a field to NULL. server.update_set returns just the touched
    set, so we hand back the fresh plan for the card to re-render off one render path."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    reps = _num(body.get("reps"))
    if reps is None:  # blank reps == clear the set
        res = server.clear_plan_set(set_id)
        code = 400 if isinstance(res, dict) and res.get("error") else 200
        return JSONResponse(res, status_code=code)
    res = server.update_set(
        set_id,
        weight_lbs=_num(body.get("weight_lbs")),
        reps=int(reps),
        rpe=_num(body.get("rpe")),
    )
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(server.get_workout_plan())


@app.post("/trainer/exercise/{exercise_id}/remove")
async def trainer_remove_exercise(request: Request, exercise_id: int):
    """Delete one exercise from the active plan (the "..." menu's Delete option). All
    its sets go. Returns the updated plan."""
    from fastapi.responses import JSONResponse
    res = server.remove_plan_exercise(exercise_id)
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.get("/trainer/exercise/{exercise_id}/info.json")
async def trainer_exercise_info(request: Request, exercise_id: int):
    """Technique for the plan card's "i" button: the catalog's saved technique notes,
    common mistakes and cautions, plus a YouTube search link to quickly watch the
    movement (and any saved video_link)."""
    from fastapi.responses import JSONResponse
    from urllib.parse import quote_plus
    info = server.exercises(exercise_id=exercise_id)
    if isinstance(info, dict) and not info.get("error"):
        terms = ((info.get("name") or "") + " proper form technique").strip()
        info["youtube_search"] = ("https://www.youtube.com/results?search_query="
                                  + quote_plus(terms))
    code = 404 if isinstance(info, dict) and info.get("error") else 200
    return JSONResponse(info, status_code=code)


@app.get("/drinking")
async def drinking(request: Request, error: str = ""):
    s = data.drinking(days=30)
    log = data.recent_drinks(limit=30)
    # Today's running tally for the header — drinks are one row per day, so today's
    # row (if any) IS today's total/tags. None on a sober day.
    today = server.today()
    today_drink = next((r for r in log if r["drink_date"] == today), None)
    return page(request, "drinking.html", active="drinking",
                s=s, log=log, today=today, today_drink=today_drink, error=error)


@app.post("/drinking/add")
async def drinking_add(request: Request):
    """Direct drink entry — the one write on the drinking page. Drinks are simple
    enough that they don't need the AI: this calls server.log_drinks straight and
    redirects back (Post/Redirect/Get). server.log_drinks does the validation."""
    form = await request.form()
    base = base_path(request)
    raw = (form.get("standard_drinks") or "").strip()
    try:
        amount = float(raw)
    except ValueError:
        return RedirectResponse(base + "/drinking?error=Enter+a+number+of+drinks.",
                                status_code=303)
    res = server.log_drinks(
        standard_drinks=amount,
        drink_date=(form.get("drink_date") or "").strip() or None,
        kind=(form.get("kind") or "").strip() or None,
        notes=(form.get("notes") or "").strip() or None,
    )
    if isinstance(res, dict) and res.get("error"):
        from urllib.parse import quote_plus
        return RedirectResponse(base + "/drinking?error=" + quote_plus(res["error"]),
                                status_code=303)
    return RedirectResponse(base + "/drinking", status_code=303)


@app.post("/drinking/edit")
async def drinking_edit(request: Request):
    """Edit one past day in place — overwrites the day's total/kind/notes/date with
    absolute values via server.update_drink (PRG back). Blank fields are left
    unchanged (update_drink only writes non-null args); to clear a day, delete it."""
    from urllib.parse import quote_plus
    form = await request.form()
    base = base_path(request)
    try:
        drink_id = int((form.get("drink_id") or "").strip())
    except ValueError:
        return RedirectResponse(base + "/drinking", status_code=303)
    raw = (form.get("standard_drinks") or "").strip()
    amount = None
    if raw:
        try:
            amount = float(raw)
        except ValueError:
            return RedirectResponse(base + "/drinking?error=Enter+a+number+of+drinks.",
                                    status_code=303)
    res = server.update_drink(
        drink_id=drink_id,
        standard_drinks=amount,
        drink_date=(form.get("drink_date") or "").strip() or None,
        kind=(form.get("kind") or "").strip() or None,
        notes=(form.get("notes") or "").strip() or None,
    )
    if isinstance(res, dict) and res.get("error"):
        return RedirectResponse(base + "/drinking?error=" + quote_plus(res["error"]),
                                status_code=303)
    return RedirectResponse(base + "/drinking", status_code=303)


@app.post("/drinking/delete")
async def drinking_delete(request: Request):
    """Remove one logged day entirely (server.delete_record kind='drink'); the day
    reverts to sober. PRG back to the drinking page."""
    form = await request.form()
    base = base_path(request)
    try:
        drink_id = int((form.get("drink_id") or "").strip())
    except ValueError:
        return RedirectResponse(base + "/drinking", status_code=303)
    server.delete_record(kind="drink", id=drink_id)
    return RedirectResponse(base + "/drinking", status_code=303)


# --------------------------------------------------------------------------- #
# AI chat — a write path for prose (the journal). Browse pages above stay
# read-only; drinks have their own direct-entry form. Each surface is scoped to
# one toolset: the `journal` panel (journal page) and the `trainer` page get
# different tools. Gated by RequireAuth (these paths aren't in PUBLIC_PATHS).
# --------------------------------------------------------------------------- #

def _chat_id(request: Request) -> str:
    """Per-session conversation key, stored small in the signed cookie (the
    history itself lives in chat._CONVERSATIONS, not the cookie)."""
    cid = request.session.get("chat_id")
    if not cid:
        import uuid
        cid = uuid.uuid4().hex
        request.session["chat_id"] = cid
    return cid


@app.post("/chat/{agent}/send")
async def chat_send(request: Request, agent: str):
    from fastapi.responses import JSONResponse, StreamingResponse
    if not chat.ENABLED:
        return JSONResponse({"error": "Chat is not configured."}, status_code=503)
    if not chat.is_agent(agent):
        return JSONResponse({"error": f"Unknown chat agent '{agent}'."}, status_code=404)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Empty message."}, status_code=400)
    cid = _chat_id(request)
    base = base_path(request)
    context = _chat_context(body)

    async def event_stream():
        async for ev in chat.run_turn(agent, cid, text, context=context):
            if ev.get("href"):  # prefix tool-chip links with the mount path
                ev["href"] = base + ev["href"]
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/chat/{agent}/reset")
async def chat_reset(request: Request, agent: str):
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    chat.reset(agent, _chat_id(request), context=_chat_context(body))
    return JSONResponse({"status": "ok"})


def _chat_context(body: dict):
    """Optional per-page chat context from the request body. The profile-page panel
    sends `person_id`; we resolve it server-side to a person-pinned context (None if
    absent or unknown) so the thread is scoped to that person and the model edits the
    right record."""
    pid = (body or {}).get("person_id")
    if pid is None:
        return None
    try:
        return chat.person_context(int(pid))
    except (TypeError, ValueError):
        return None


@app.get("/people")
async def people(request: Request, q: str = ""):
    q = (q or "").strip()
    res = server.list_people(query=q or None)
    return page(request, "people.html", active="people",
                q=q, people=res["people"], count=res["count"],
                groups=data.groups_overview())


@app.get("/groups")
async def groups(request: Request):
    return page(request, "groups.html", active="people",
                groups=data.groups_overview())


@app.get("/group/{name}")
async def group(request: Request, name: str):
    g = data.group_members(name)
    if g is None:
        return page(request, "notfound.html", active="people",
                    status_code=404, what="group")
    return page(request, "group.html", active="people", g=g)


@app.get("/person/{person_id}")
async def person(request: Request, person_id: int):
    p = data.person_detail(person_id)
    if p is None:
        return page(request, "notfound.html", active="people",
                    status_code=404, what="person")
    return page(request, "person.html", active="people", p=p)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("WEB_HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8001")))
