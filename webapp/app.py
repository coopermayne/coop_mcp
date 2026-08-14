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

import json
import os
import re
import secrets
import sys
import time
from typing import Optional
from urllib.parse import quote
from datetime import datetime, date as date_cls

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

# A strong random token (e.g. `openssl rand -hex 32`) that unlocks the backup
# download for a HEADLESS client — a cron `curl` from another machine — without a
# browser/Google login. Presented as `Authorization: Bearer <token>`, an
# `X-Backup-Token` header, or a `?token=` query param. Unset = no token path
# (the backup is then browser/session-only). Distinct from the session login so a
# leaked cron token grants nothing but read-only backups.
BACKUP_TOKEN = (os.environ.get("BACKUP_TOKEN") or "").strip()

# Same headless-token idea, MUCH smaller scope: unlocks GET /api/today.json, a
# read-only view of TODAY's nutrient sums (and the display targets they're read
# against) for an ambient display — a SwiftBar menu-bar plugin, a phone widget.
# Its own token on purpose: this one lives on every device that wants a glanceable
# number, so it must never be BACKUP_TOKEN, which downloads the whole journal.
# Unset = the endpoint is browser/session-only.
WIDGET_TOKEN = (os.environ.get("WIDGET_TOKEN") or "").strip()

# --------------------------------------------------------------------------- #
# Journal lock — a second, local gate on top of Google auth.
#
# Google auth keeps strangers OUT; this keeps someone who picks up the ALREADY
# signed-in device (the classic "my wife grabs my phone") from scrolling the
# journal. It guards ONLY the journal surface (entries / people / pending /
# groups + the journal chat, and now the day-header drink write, which lives on
# the feed) — the trainer stays open, it isn't private. It's deliberately
# low-security: the only person who can even reach these routes is the
# authenticated owner, so the gate's job is to stop casual reading, not a
# determined attacker.
#
# Unlock is a "secret knock": tap a rhythm on the lock screen. We compare the
# *ratios* between taps (normalized so total tempo doesn't matter), so the same
# pattern played fast or slow both pass. The reference pattern lives in the DB
# `settings` table (key `journal_lock`), set once from the lock screen.
#
# Enabled when JOURNAL_LOCK is truthy OR a knock has already been recorded (so
# it stays on across restarts without re-setting the env). Unset + no knock =
# the whole feature is dormant. LOCK_PIN is an optional digit-code fallback so a
# fumbled rhythm never locks you out; unset = knock only. LOCK_IDLE_SECONDS is
# the auto-relock window (slides on activity; also re-locks on a fresh open).
LOCK_ENABLED_ENV = (os.environ.get("JOURNAL_LOCK") or "").lower() in ("1", "true", "yes")
LOCK_PIN = (os.environ.get("JOURNAL_LOCK_PIN") or "").strip()
LOCK_IDLE_SECONDS = int(os.environ.get("JOURNAL_LOCK_IDLE", "300") or "300")
# Per-interval tolerance for the knock match, as a fraction of the knock's total
# duration (the gaps are normalized to sum to 1, so 0.15 ≈ each beat may land
# 15% of the way off). Forgiving enough to reproduce by hand, tight enough that a
# different rhythm fails.
LOCK_TOLERANCE = float(os.environ.get("JOURNAL_LOCK_TOLERANCE", "0.15") or "0.15")

# Journal-only paths the lock guards (relative to the mount prefix). Everything
# else — trainer, library, auth, static, the lock screen itself — passes
# straight through.
LOCK_PATHS_EXACT = {"/", "/journal", "/pending", "/people", "/groups"}
LOCK_PATHS_PREFIX = ("/entry/", "/person/", "/group/", "/chat/journal/", "/mention/")

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

# The eating block's rings read targets through this; a global (not a per-route
# context var) because the macro is reached through day_block, several call frames
# from the route. A FUNCTION, not the dict: targets can now come from the stored
# eating profile, so they're read fresh per render (the macro calls it once per block).
templates.env.globals["nutrient_targets"] = data.nutrient_targets

# Collection icons: the vendored Lucide subset (icons.py, generated). A global
# so any template can draw one by name — the shapes only, since each site picks
# its own size/stroke, exactly like the hand-written nav icons.
templates.env.globals["icon_paths"] = server.icon_set.ICON_PATHS
templates.env.globals["default_icon"] = server.icon_set.DEFAULT_ICON


def link_people_md(body: str, people: list[dict], base: str) -> str:
    """Return the entry body as Markdown with each resolved person's name turned into
    a Markdown link to their page (link title = "Name · role"). The body is already
    Markdown — the model writes bold/lists/etc. — so the browser renders it client-side
    with `marked` + the `.chat-md` styles (the same pipeline the AI chat uses) and tags
    the person links with `.person-link` afterwards. Names match on word boundaries,
    case-insensitively, longest form first (so "Tom Brady" wins over "Tom"); every
    occurrence is linked. Non-name text passes through untouched as Markdown source."""
    if not body:
        return ""
    form_to_person: dict[str, dict] = {}
    for p in people:
        for f in p.get("forms", []):
            f = (f or "").strip()
            if len(f) < 2:  # 1-char forms match too much
                continue
            form_to_person.setdefault(f.lower(), p)
    if not form_to_person:
        return body
    forms = sorted(form_to_person, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(f) for f in forms) + r")\b",
                         re.IGNORECASE)
    out, last = [], 0
    for mobj in pattern.finditer(body):
        out.append(body[last:mobj.start()])
        word = mobj.group(0)
        person = form_to_person.get(word.lower())
        if person:
            label = person["name"] + (f" · {person['role']}" if person.get("role") else "")
            # Keep the link's [text] and "title" from breaking Markdown link syntax.
            text = word.replace("[", r"\[").replace("]", r"\]")
            title = label.replace('"', "'")
            out.append(f'[{text}]({base}/person/{person["person_id"]} "{title}")')
        else:
            out.append(word)
        last = mobj.end()
    out.append(body[last:])
    return "".join(out)


def people_from_mentions(mentions: list[dict]) -> list[dict]:
    """Shape a `mentions` list (entry-detail) into the {person_id, name, role, forms}
    dicts link_people_md wants, so the single entry page links names inline too. Only
    resolved mentions contribute; the surface form and canonical name are both forms."""
    by_id: dict[int, dict] = {}
    for mn in mentions:
        pid = mn.get("person_id")
        if not pid:
            continue
        p = by_id.setdefault(pid, {"person_id": pid, "name": mn.get("name"),
                                   "role": mn.get("role"), "forms": set()})
        for f in (mn.get("surface_form"), mn.get("name")):
            if f:
                p["forms"].add(f)
    return list(by_id.values())


def base_path(request: Request) -> str:
    """The mount prefix this app is served under ('/app' when embedded in the MCP
    process, '' when run standalone). Internal links/redirects are prefixed with it."""
    return request.scope.get("root_path", "") or ""


def page(request: Request, template: str, active: str = "", status_code: int = 200, **ctx):
    locked_here = _is_lock_path(_rel_path(request))
    ctx.update(active=active, auth_enabled=AUTH_ENABLED, show_logout=SHOW_LOGOUT,
               chat_enabled=chat.ENABLED,
               # `lock_guard` arms the client idle-relock timer, on the guarded
               # journal pages only (never on the trainer, never on the lock
               # screen itself). `lock_in_journal` gates the nav's lock controls
               # (Lock journal / Change knock): shown ONLY while you're actually
               # inside the unlocked journal — so they're never reachable from the
               # lock screen or the open trainer pages (otherwise the knock
               # could be changed without first proving you're in).
               lock_guard=_lock_active() and locked_here,
               lock_in_journal=_lock_active() and locked_here and _is_unlocked(request),
               lock_idle_ms=LOCK_IDLE_SECONDS * 1000,
               user=request.session.get("email"), base=base_path(request))
    return templates.TemplateResponse(request=request, name=template,
                                      context=ctx, status_code=status_code)


# --------------------------------------------------------------------------- #
# Journal lock helpers (see the config block up top for the why)
# --------------------------------------------------------------------------- #

def _rel_path(request: Request) -> str:
    """Request path relative to the mount prefix ('/app' embedded, '' standalone),
    so the lock path-set matches whether the app is mounted or standalone."""
    root = request.scope.get("root_path", "") or ""
    path = request.url.path
    if root and path.startswith(root):
        path = path[len(root):] or "/"
    return path


def _is_lock_path(path: str) -> bool:
    """Whether `path` (mount-relative) is part of the journal surface the lock guards."""
    return path in LOCK_PATHS_EXACT or path.startswith(LOCK_PATHS_PREFIX)


def _get_lock() -> Optional[dict]:
    """The stored knock reference ({"gaps": [...normalized...], "count": N}) or None
    if no knock has been recorded. Reads the shared `settings` table directly — the
    same generic JSON-KV the trainer profile lives in; no server.py tool needed for a
    pure webapp-config value."""
    try:
        with server.db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='journal_lock'").fetchone()
        return json.loads(row["value"]) if row else None
    except Exception:
        return None


def _set_lock(norm_gaps: list, count: int) -> None:
    with server.db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('journal_lock', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"gaps": norm_gaps, "count": count}),),
        )


def _lock_active() -> bool:
    """The lock guards the journal when explicitly enabled OR once a knock exists."""
    return LOCK_ENABLED_ENV or (_get_lock() is not None)


def _normalize_gaps(gaps) -> Optional[list]:
    """Turn raw inter-tap durations (ms) into tempo-independent ratios that sum to 1.
    None if there aren't at least two gaps (i.e. fewer than three taps) — too little
    to be a rhythm."""
    try:
        vals = [float(g) for g in gaps if g is not None and float(g) > 0]
    except (TypeError, ValueError):
        return None
    total = sum(vals)
    if len(vals) < 2 or total <= 0:
        return None
    return [g / total for g in vals]


def _knock_matches(ref: Optional[list], attempt: Optional[list]) -> bool:
    """Same number of beats, and every normalized interval within LOCK_TOLERANCE."""
    if not ref or not attempt or len(ref) != len(attempt):
        return False
    return all(abs(r - a) <= LOCK_TOLERANCE for r, a in zip(ref, attempt))


def _unlock_session(request: Request) -> None:
    request.session["jrl_unlocked"] = True
    request.session["jrl_unlocked_at"] = int(time.time())


def _relock_session(request: Request) -> None:
    request.session.pop("jrl_unlocked", None)
    request.session.pop("jrl_unlocked_at", None)


def _is_unlocked(request: Request) -> bool:
    """Unlocked AND still within the idle window. The window slides on each guarded
    request (see LockGate), so it's an inactivity timeout, not a fixed lease."""
    if not request.session.get("jrl_unlocked"):
        return False
    ts = int(request.session.get("jrl_unlocked_at", 0) or 0)
    return (int(time.time()) - ts) <= LOCK_IDLE_SECONDS


def _safe_next(raw: Optional[str]) -> str:
    """A caller-supplied post-unlock destination, sanitized to a same-app relative
    path so it can't be turned into an open redirect. Falls back to /journal."""
    if raw and raw.startswith("/") and not raw.startswith("//") and "://" not in raw:
        return raw
    return "/journal"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

# The PWA wiring (manifest + service worker) must be fetchable without auth: the
# browser pulls them itself, and an auth redirect to /login would hand back HTML
# instead, breaking install. They expose no journal data — just app metadata.
PUBLIC_PATHS = {"/login", "/auth/login", "/auth/callback", "/health",
                "/manifest.webmanifest", "/sw.js",
                # The backup endpoint opts OUT of the redirect-to-login middleware so
                # a headless cron isn't bounced to an HTML login page; it does its OWN
                # auth (a logged-in session OR the BACKUP_TOKEN) inside the route.
                "/export/journal.db",
                # Same reason: a menu-bar plugin polling for today's macros must get
                # 401 JSON, not an HTML login redirect. Guarded by WIDGET_TOKEN in-route.
                "/api/today.json"}

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


class LockGate(BaseHTTPMiddleware):
    """Second gate (runs INSIDE RequireAuth, so login wins): when the journal lock
    is active, redirect any guarded journal path to the knock screen unless this
    session is unlocked and still inside the idle window. On a guarded, unlocked
    request it slides the idle timer forward, so the lock is an inactivity timeout
    — set the device down and it re-locks itself. Everything outside the journal
    surface (trainer, the lock screen, static) passes straight through."""
    async def dispatch(self, request: Request, call_next):
        root = request.scope.get("root_path", "") or ""
        path = _rel_path(request)
        if not _is_lock_path(path) or not _lock_active():
            return await call_next(request)
        if _is_unlocked(request):
            request.session["jrl_unlocked_at"] = int(time.time())  # slide the window
            return await call_next(request)
        dest = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(root + "/lock?next=" + quote(dest, safe=""))


# Middleware execution is outermost-first = reverse of add order: Session →
# RequireAuth → LockGate → route. Session must wrap both (they read request.session);
# RequireAuth must wrap LockGate so an unauthenticated hit goes to /login, not /lock.
app.add_middleware(LockGate)
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


# --------------------------------------------------------------------------- #
# Journal lock — the knock screen and its set/verify/relock endpoints. These
# sit behind RequireAuth (only the owner reaches them) but OUTSIDE the LockGate's
# guarded set, so the lock screen is always reachable. See the config block and
# helpers up top.
# --------------------------------------------------------------------------- #

@app.get("/lock")
async def lock_page(request: Request):
    base = base_path(request)
    if not _lock_active():
        return RedirectResponse(base + "/journal")
    nxt = _safe_next(request.query_params.get("next"))
    configured = _get_lock() is not None
    change = bool(request.query_params.get("change"))
    if not configured:
        mode = "setup"            # no knock yet → must record one to proceed
    elif change and _is_unlocked(request):
        mode = "setup"            # re-recording, allowed only from an unlocked session
    elif _is_unlocked(request):
        return RedirectResponse(base + nxt)   # already in; nothing to do here
    else:
        mode = "unlock"
    # active="journal" so base.html renders the nav: the lock only covers the journal,
    # so the rest of the app (trainer, training, library) stays reachable
    # from the lock screen without unlocking.
    return page(request, "lock.html", active="journal", next=nxt, mode=mode,
                pin_enabled=bool(LOCK_PIN), tolerance=LOCK_TOLERANCE)


@app.post("/lock/verify")
async def lock_verify(request: Request):
    """Check a knock (or the optional PIN fallback) and unlock the session. Body:
    {gaps: [ms,...]} for a knock, or {pin: "1234"} when JOURNAL_LOCK_PIN is set."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    ok = False
    pin = body.get("pin")
    if pin is not None and LOCK_PIN:
        ok = secrets.compare_digest(str(pin), LOCK_PIN)
    else:
        ref = _get_lock()
        ok = bool(ref) and _knock_matches(ref.get("gaps"), _normalize_gaps(body.get("gaps")))
    if ok:
        _unlock_session(request)
    return JSONResponse({"ok": ok})


@app.post("/lock/set")
async def lock_set(request: Request):
    """Record (or change) the secret knock. Allowed when no knock exists yet, or
    from an already-unlocked session (so you can't reset the knock without first
    being inside). Body: {gaps: [ms,...]}. Recording unlocks the session too."""
    from fastapi.responses import JSONResponse
    if _get_lock() is not None and not _is_unlocked(request):
        return JSONResponse({"error": "Unlock first to change the knock."}, status_code=403)
    body = await request.json()
    norm = _normalize_gaps(body.get("gaps"))
    if norm is None:
        return JSONResponse({"error": "Tap at least three times."}, status_code=400)
    _set_lock(norm, len(norm) + 1)
    _unlock_session(request)
    return JSONResponse({"ok": True})


@app.get("/lock/relock")
async def lock_relock(request: Request):
    """Lock the journal now (the nav's 'Lock journal' control)."""
    _relock_session(request)
    return RedirectResponse(base_path(request) + "/lock")


@app.post("/lock/keepalive")
async def lock_keepalive(request: Request):
    """Slide the idle-relock window forward while the user is ACTIVELY on a guarded
    journal page but not making requests — the classic "composing a long chat
    message" case, where no navigation happens for minutes so the server-side window
    would otherwise lapse and bounce the eventual send to the lock screen. The client
    pings this (throttled) on real interaction; it only ever refreshes an
    already-unlocked session — it NEVER unlocks one — so it can't be used to defeat
    the lock. Reports back whether the session is still unlocked so the client can
    relock itself if the window already lapsed. Lives OUTSIDE the LockGate's guarded
    set (it's under /lock) so it must slide the window itself rather than relying on
    the gate. Returns {active, unlocked}; active=False means the lock is dormant."""
    from fastapi.responses import JSONResponse
    if not _lock_active():
        return JSONResponse({"active": False})
    if _is_unlocked(request):
        request.session["jrl_unlocked_at"] = int(time.time())  # slide the window
        return JSONResponse({"active": True, "unlocked": True})
    return JSONResponse({"active": True, "unlocked": False})


@app.get("/health")
async def health():
    from fastapi.responses import JSONResponse
    try:
        with server.db() as conn:
            conn.execute("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as e:  # pragma: no cover
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


def _presented_token(request: Request, header: str) -> Optional[str]:
    """Pull a caller-presented token from (in order) an `Authorization: Bearer <t>`
    header, the given custom header (`X-Backup-Token` / `X-Widget-Token`), or a
    `?token=` query param. Returns None if none present."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get(header) or request.query_params.get("token") or None


def _token_authorized(request: Request, token: str, header: str) -> bool:
    """Shared shape for the two headless endpoints: a logged-in browser session
    (so you can also just open the URL in a signed-in tab) OR a client presenting
    `token` (constant-time compare). With auth off entirely (dev), everything is
    open anyway. An unset token means session-only — never open."""
    if not AUTH_ENABLED:
        return True
    if request.session.get("email"):
        return True
    if not token:
        return False
    presented = _presented_token(request, header)
    return bool(presented and secrets.compare_digest(presented, token))


def _backup_authorized(request: Request) -> bool:
    """Authorize the full-DB backup download — session or BACKUP_TOKEN."""
    return _token_authorized(request, BACKUP_TOKEN, "x-backup-token")


def _widget_authorized(request: Request) -> bool:
    """Authorize the today's-macros read — session or WIDGET_TOKEN.

    Deliberately a SEPARATE token from BACKUP_TOKEN, not a reuse: this one lives in
    a menu-bar plugin / phone widget on whatever device wants a glanceable figure,
    and BACKUP_TOKEN downloads the entire journal — every entry, every person. The
    blast radius of the token that sits on the most devices should be one day's
    nutrient sums, so the two are never interchangeable.
    """
    return _token_authorized(request, WIDGET_TOKEN, "x-widget-token")


@app.get("/api/today.json")
async def today_json(request: Request):
    """Today's nutrient sums, for an ambient display (SwiftBar plugin, phone widget).

    Read-only and scoped to ONE day: no entries, no people, no items — just the
    figures the journal page's rings already show, so the token that ends up on a
    laptop or phone can't be turned into a data exfil. Auth is a session or
    WIDGET_TOKEN (see `_widget_authorized`).

    Returns each nutrient as {total, target, ceiling}. `total` is null when nothing
    logged carries that nutrient — the SAME distinction the rings draw between "0 so
    far" and "unestimated", so a client can render a dashed/unknown state instead of
    claiming a zero. Targets ride along from `data.nutrient_targets()` — the stored
    eating profile's numbers over the webapp defaults, the same merge the rings
    read — so the widget always agrees with the page and with what the model is
    coaching against.

    UNITS are deliberately NOT here. They're a rendering choice that already lives in
    `macros.html`, and duplicating them server-side is how the two copies drift; a
    client that wants "92g" formats it from `protein_g` itself.
    """
    from fastapi.responses import JSONResponse
    if not _widget_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    day = server.today()
    intake = server.get_intake(days=1, include_items=False)
    # get_intake omits days with nothing logged, so an unlogged day has no entry.
    totals = next((d["totals"] for d in intake["days"] if d["food_date"] == day), {})
    # Keyed off server.NUTRIENTS, not the targets dict, so an UNTARGETED nutrient
    # (fat) still reports its figure with target=null — the same thing the journal
    # page does when it draws fat's ring with a dashed track and no arc. Iterating
    # the targets instead would silently drop it.
    targets = data.nutrient_targets()
    return JSONResponse({
        "date": day,
        "nutrients": {
            key: {"total": totals.get(key),
                  "target": (targets.get(key) or {}).get("target"),
                  "ceiling": (targets.get(key) or {}).get("ceiling", False)}
            for key in server.NUTRIENTS
        },
    })


@app.get("/export/journal.db")
async def export_db(request: Request):
    """Stream a consistent SQLite snapshot of the entire journal DB.

    Two ways in (see `_backup_authorized`): a logged-in browser session or, for a
    headless cron, the `BACKUP_TOKEN` as a bearer token / `X-Backup-Token` header
    / `?token=`. There is no UI for this — it's a plain URL you hit with curl. The
    file is a plain SQLite db built with VACUUM INTO — restore is "drop it in at
    JOURNAL_DB and restart" (see README, "Backup & restore"). Pull it on a
    schedule from off-box to survive a lost volume. Built in a temp dir and
    deleted after the response is sent.
    """
    from fastapi.responses import Response as _Resp
    if not _backup_authorized(request):
        return _Resp(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    import tempfile, shutil
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from starlette.concurrency import run_in_threadpool
    tmpdir = tempfile.mkdtemp(prefix="journal-export-")
    fname = f"journal-{server.today()}.db"
    dest = os.path.join(tmpdir, fname)
    # VACUUM INTO can block; keep it off the event loop for a large DB.
    await run_in_threadpool(server.snapshot_db, dest)
    return FileResponse(
        dest,
        media_type="application/x-sqlite3",
        filename=fname,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


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
# static assets. Everything the shell needs is now self-hosted (compiled
# Tailwind, Inter, marked — no CDNs), so it's all precached and the app styles
# itself offline. Bump VERSION to retire old caches on the next visit.
_SERVICE_WORKER_TMPL = """\
const VERSION = 'v7';
const CACHE = 'journal-' + VERSION;
const BASE = '__BASE__';
const PRECACHE = [
  BASE + '/static/icon-192.png',
  BASE + '/static/favicon.svg',
  BASE + '/static/tailwind.css',
  BASE + '/static/fonts/inter-latin.woff2',
  BASE + '/static/vendor/marked.min.js',
  BASE + '/static/body-symbols.svg',
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
async def journal(request: Request, q: str = "", since: str = "", kind: str = ""):
    q = (q or "").strip()
    since = (since or "").strip() or None
    # Feed filter: "" / "all" = everything; "thought" = reflections only;
    # "log" = everything except reflections. Anything else is treated as "all".
    kind = (kind or "").strip().lower()
    kind_filter = kind if kind in ("thought", "log") else None
    base = base_path(request)
    pending_n = _pending_count()
    if q:
        # Browse-page search: the user reads all their own matches, so no small cap
        # (the 20-ish default on the MCP tool is for the token-budgeted conversation).
        res = server.search_entries(q, limit=100_000)
        entries = data.attach_people(res["results"])
        for e in entries:
            e["body_md"] = link_people_md(e["body"], e["people"], base)
        return page(request, "journal.html", active="journal",
                    q=q, entries=entries, count=res["count"], searching=True,
                    pending_count=pending_n, kind=kind_filter)
    res = data.list_days(since=since, kind=kind_filter)
    for day in res["days"]:
        for e in day["entries"]:
            e["body_md"] = link_people_md(e["body"], e["people"], base)
    # Calendar marks ALL dates that have an entry (of the active kind), not just the
    # loaded window — so every month is reachable even before its days are paged in.
    months = data.calendar_months(data.all_entry_dates(kind=kind_filter),
                                  today=server.today())
    return page(request, "journal.html", active="journal",
                q="", days=res["days"], count=res["total"], searching=False,
                pending_count=pending_n, months=months, kind=kind_filter,
                has_more=res["has_more"], next_since=res["next_since"])


@app.get("/food")
async def food(request: Request, since: str = ""):
    # The food log's own page — split out of the journal feed. Deliberately NOT in
    # LOCK_PATHS (glancing at macros shouldn't need the knock) and carries no chat
    # panel: intake is logged through the MCP tools / the journal chat, this page
    # only reads.
    res = data.food_days(since=(since or "").strip() or None)
    return page(request, "food.html", active="food",
                days=res["days"], count=res["total"],
                has_more=res["has_more"], next_since=res["next_since"],
                # The Targets popover wants the two apart: what's actually SET
                # goes in the inputs, the defaults are only placeholders.
                defaults=data.NUTRIENT_TARGETS, ceilings=data.NUTRIENT_CEILINGS,
                stored=data.stored_targets())


@app.post("/food/targets")
async def food_targets(request: Request):
    """Save the /food page's Targets popover — the daily nutrient goals the rings
    are read against. A website-only write path through server.set_nutrient_targets
    (never a FastMCP tool), and the ONLY thing this page writes: intake CONTENT
    still enters through the MCP tools alone. Targets aren't content — they're the
    same kind of fact as a collection's Display prefs, except they live where the
    model can read them too, since it coaches against the same numbers.
    Body: {targets: {nutrient: number|null}} — null hands a goal back to its
    default. Outside the journal lock, like the page itself."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    res = server.set_nutrient_targets(body.get("targets"))
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.get("/pending")
async def pending(request: Request):
    items = data.pending_mentions()
    return page(request, "pending.html", active="journal",
                items=items, count=len(items))


# --------------------------------------------------------------------------- #
# Inline mention resolver (the pending queue + entry page can pin a mention to a
# person without leaving for chat). All write-through to server.py's website-only
# helpers; JSON fetch pattern like the trainer toggles. Lock-guarded via the
# /mention/ prefix above.
# --------------------------------------------------------------------------- #

@app.get("/mention/people-search")
async def mention_people_search(request: Request, q: str = ""):
    """People picker for the resolver — link to someone who isn't a candidate.
    Returns a compact [{person_id, name, role}] list (most-recent first)."""
    from fastapi.responses import JSONResponse
    res = server.list_people(query=q.strip() or None)
    people = [{"person_id": p["person_id"], "name": p["name"], "role": p["role"]}
              for p in res["people"][:20]]
    return JSONResponse({"people": people})


@app.post("/mention/{mention_id}/resolve")
async def mention_resolve(request: Request, mention_id: int):
    """Pin a pending mention to one person. Body: {person_id: int, learn_alias?: bool}.
    Writes through resolve_mention_web."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    pid = body.get("person_id")
    res = server.resolve_mention_web(mention_id, pid, learn_alias=bool(body.get("learn_alias")))
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.post("/mention/{mention_id}/new-person")
async def mention_new_person(request: Request, mention_id: int):
    """Create a new person and pin this mention to them in one step. Body:
    {canonical_name, role?, learn_alias?}. save_person then resolve_mention_web."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    name = (body.get("canonical_name") or "").strip()
    if not name:
        return JSONResponse({"error": "canonical_name is required"}, status_code=400)
    created = server.save_person(canonical_name=name,
                                 role=(body.get("role") or "").strip() or None)
    if isinstance(created, dict) and created.get("error"):
        return JSONResponse(created, status_code=400)
    res = server.resolve_mention_web(mention_id, created["person_id"],
                                     learn_alias=bool(body.get("learn_alias")))
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse({"ok": True, "person_id": created["person_id"]})


@app.post("/mention/{mention_id}/dismiss")
async def mention_dismiss(request: Request, mention_id: int):
    """Delete a stray mention (a group word, or noise). dismiss_mention_web."""
    from fastapi.responses import JSONResponse
    res = server.dismiss_mention_web(mention_id)
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.get("/entry/{entry_id}")
async def entry(request: Request, entry_id: int):
    e = data.entry_with_people(entry_id)
    if e is None:
        return page(request, "notfound.html", active="journal",
                    status_code=404, what="entry")
    e["body_md"] = link_people_md(e["body"], people_from_mentions(e["mentions"]),
                                  base_path(request))
    return page(request, "entry.html", active="journal", e=e)


@app.get("/workouts")
async def workouts(request: Request):
    sessions = data.workouts_full(limit=20)
    brief = server.get_fitness_briefing(recent_workouts=1)
    months = data.calendar_months([s["date"] for s in sessions], today=server.today())
    return page(request, "workouts.html", active="workouts",
                sessions=sessions,
                profile=brief.get("profile", {}),
                months=months)


@app.get("/trainer")
async def trainer(request: Request):
    """The trainer surface: the active workout plan (today's routine, tap-to-complete)
    plus the AI chat panel that builds and adjusts it. The plan card is rendered
    client-side from the bootstrapped JSON so chat-driven and tap-driven changes share
    one render path (static/trainer.js)."""
    return page(request, "trainer.html", active="trainer",
                plan=_with_bodyweight(data.active_plan()))


@app.get("/trainer/library")
async def trainer_library(request: Request, muscle: str = "", q: str = "",
                          rotation: str = "", hearted: str = "", archived: str = "",
                          error: str = ""):
    """The exercise library: browse the whole catalog — muscles (by emphasis tier),
    equipment, level/mechanic, technique, and a form gif/video per exercise. Filterable
    by muscle, name, `rotation` (the small programming pool) or `hearted` (the wider
    favorites superset it's drawn from); `archived` shows the soft-deleted movements (the
    Archived view, where each row offers Restore). Each row toggles in/out of the rotation
    and the hearted superset and can be archived (removed from the library without breaking
    past workouts). The user curates the closed catalog here: the page's AI add panel (the
    `exercise` chat agent → server.create_exercise) is the only way a new exercise enters
    it — the trainer chat can enrich technique but never creates one."""
    lib = data.exercise_library(muscle=muscle, q=q, rotation=bool(rotation),
                                hearted=bool(hearted), archived=bool(archived))
    return page(request, "library.html", active="library", error=error, **lib)


@app.post("/trainer/exercise/{exercise_id}/rotation")
async def trainer_set_rotation(request: Request, exercise_id: int):
    """Toggle one exercise in/out of the rotation (the library page's star button). Body:
    {"in_rotation": true|false}. Writes through server.set_rotation (which also hearts it
    when adding). Returns the resulting {in_rotation, hearted} so the UI can sync both."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    res = server.set_rotation(exercise_id=exercise_id, in_rotation=bool(body.get("in_rotation")))
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.post("/trainer/exercise/{exercise_id}/hearted")
async def trainer_set_hearted(request: Request, exercise_id: int):
    """Toggle one exercise in/out of the hearted superset (the library page's heart button).
    Body: {"hearted": true|false}. Writes through server.set_hearted (un-hearting also drops
    it from the rotation). Returns the resulting {in_rotation, hearted} so the UI syncs both."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    res = server.set_hearted(exercise_id=exercise_id, hearted=bool(body.get("hearted")))
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.post("/trainer/exercise/{exercise_id}/archive")
async def trainer_set_archived(request: Request, exercise_id: int):
    """Archive (soft-delete) or restore one exercise — the library row's Archive / Restore
    control. Body: {"archived": true|false} (defaults true). Archiving hides it from the
    library, search and the trainer, and drops it from the rotation, without deleting the
    row, so past workouts keep their links. Writes through server.set_archived."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    res = server.set_archived(exercise_id=exercise_id,
                              archived=bool(body.get("archived", True)))
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


def _with_bodyweight(plan: dict) -> dict:
    """Attach the workout day's latest bodyweight reading to a plan payload so the
    /trainer card's weigh-in box knows whether today's weight is already in. Webapp-only
    enrichment — deliberately kept OFF server.get_workout_plan / _plan_payload so the
    model-facing tool returns stay lean (the box is a pure UI concern). An in-progress
    plan carries no date yet (it's dated only at finish), so fall back to today for the
    lookup."""
    if isinstance(plan, dict) and plan.get("active"):
        plan["bodyweight"] = data.bodyweight_on(plan.get("workout_date") or server.today())
    return plan


@app.get("/trainer/plan.json")
async def trainer_plan(request: Request):
    from fastapi.responses import JSONResponse
    return JSONResponse(_with_bodyweight(data.active_plan()))


@app.post("/trainer/bodyweight")
async def trainer_log_bodyweight(request: Request):
    """Log today's bodyweight from the /trainer plan card's weigh-in box (the box under the
    sets — a standing nudge to weigh in while at the gym). Body: {weight_lbs}. Writes
    through server.log_bodyweight and returns the updated plan so the card re-renders with
    the reading in place (one render path)."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    w = _num(body.get("weight_lbs"))
    if w is None or w <= 0:
        return JSONResponse({"error": "weight_lbs must be a positive number"}, status_code=400)
    res = server.log_bodyweight(weight_lbs=w)
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(_with_bodyweight(data.active_plan()))


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
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(_with_bodyweight(res))


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
    return JSONResponse(_with_bodyweight(server.get_workout_plan()))


@app.post("/trainer/exercise/{exercise_id}/remove")
async def trainer_remove_exercise(request: Request, exercise_id: int):
    """Delete one exercise from the active plan (the "..." menu's Delete option). All
    its sets go. Returns the updated plan."""
    from fastapi.responses import JSONResponse
    res = server.remove_plan_exercise(exercise_id)
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(_with_bodyweight(res))


@app.post("/trainer/reorder")
async def trainer_reorder(request: Request):
    """Set the exercise order of the active plan from the /trainer card's reorder UX (the
    ↑/↓ arrows). Body: {"order": [exercise_id, ...]} in the desired sequence. Writes
    through server.reorder_plan_exercises and returns the updated plan so the card
    re-renders off one render path."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    order = body.get("order") or []
    try:
        ids = [int(x) for x in order]
    except (TypeError, ValueError):
        return JSONResponse({"error": "order must be a list of exercise ids"}, status_code=400)
    res = server.reorder_plan_exercises(ids)
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(_with_bodyweight(res))


@app.post("/trainer/plan/discard")
async def trainer_discard_plan(request: Request):
    """Delete the active plan outright (the /trainer card's plan-level "..." menu →
    Delete plan). Drops the session and all its sets through server.discard_plan and
    returns the empty-plan state so the card re-renders to its no-active-plan view."""
    from fastapi.responses import JSONResponse
    res = server.discard_plan()
    if isinstance(res, dict) and res.get("error"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(_with_bodyweight(res))


@app.get("/trainer/exercise/{exercise_id}/info.json")
async def trainer_exercise_info(request: Request, exercise_id: int):
    """Technique for the plan card's "i" button: the catalog's saved technique notes,
    common mistakes and cautions, plus a YouTube search link to quickly watch the
    movement (and any saved video_link)."""
    from fastapi.responses import JSONResponse
    from urllib.parse import quote_plus
    info = server.find_exercises(exercise_id=exercise_id)
    if isinstance(info, dict) and not info.get("error"):
        terms = ((info.get("name") or "") + " proper form technique").strip()
        info["youtube_search"] = ("https://www.youtube.com/results?search_query="
                                  + quote_plus(terms))
    code = 404 if isinstance(info, dict) and info.get("error") else 200
    return JSONResponse(info, status_code=code)


# --------------------------------------------------------------------------- #
# Graphs — one page of line charts over the trends the app already stores
# (bodyweight, drinks, per-exercise strength progress). Read-only; the whole
# history is bootstrapped into the page as JSON (single-user data is small) and
# filtered/toggled client-side by static/graphs.js.
# --------------------------------------------------------------------------- #

@app.get("/graphs")
async def graphs(request: Request):
    return page(request, "graphs.html", active="graphs", graph=data.graph_data())


@app.post("/graphs/goal")
async def graphs_goal(request: Request):
    """Set or clear the bodyweight goal from the graphs page (PRG). Stored as
    `weight_goal` in the trainer profile blob via server.update_profile — the
    same profile get_fitness_briefing surfaces, so the trainer model coaches
    within the goal without any new plumbing. The latest weigh-in at save time
    is captured as the fixed anchor the chart draws the pace line from."""
    form = await request.form()
    base = base_path(request)
    if (form.get("action") or "") == "clear":
        server.update_profile(profile={"weight_goal": None})
        return RedirectResponse(base + "/graphs", status_code=303)
    try:
        target = float((form.get("target_lbs") or "").strip())
    except ValueError:
        return RedirectResponse(base + "/graphs", status_code=303)
    target_date = (form.get("target_date") or "").strip() or None
    if target_date:
        try:
            date_cls.fromisoformat(target_date)
        except ValueError:
            target_date = None
    goal = {"target_lbs": target, "target_date": target_date, "set_on": server.today(),
            "start_lbs": None, "start_date": None}
    with server.db() as conn:
        r = conn.execute(
            "SELECT weigh_date, weight_lbs FROM body_weight "
            "ORDER BY weigh_date DESC, id DESC LIMIT 1").fetchone()
    if r:
        goal["start_lbs"], goal["start_date"] = r["weight_lbs"], r["weigh_date"]
    server.update_profile(profile={"weight_goal": goal})
    return RedirectResponse(base + "/graphs", status_code=303)


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


@app.post("/chat/{agent}/history")
async def chat_history(request: Request, agent: str):
    """Replay the still-active server-side thread so a reload shows it's a
    continuing conversation (the transcript isn't kept in the browser)."""
    from fastapi.responses import JSONResponse
    if not chat.ENABLED or not chat.is_agent(agent):
        return JSONResponse({"turns": []})
    try:
        body = await request.json()
    except Exception:
        body = {}
    turns = chat.history(agent, _chat_id(request), context=_chat_context(body))
    base = base_path(request)
    for t in turns:  # prefix tool-chip links with the mount path, like /send does
        for c in t.get("chips", []):
            if c.get("href"):
                c["href"] = base + c["href"]
    return JSONResponse({"turns": turns})


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


# Notes & collections — the flexible layer's browse pages. Like /food these sit
# OUTSIDE the journal lock (recipes and trip ideas are glanceable; the knock
# guards the journal's prose) and are strictly read-only for CONTENT: the MCP
# tools are the one write path for items, so the browser only renders what
# conversation has filed. The one thing the browser does write is PRESENTATION —
# the Display popover below, the library-★/♥ pattern applied to rendering.
@app.get("/collections")
async def collections(request: Request):
    """The collections index — or, with ?q=, a title search across every
    collection AND the inbox (one bar for the whole flexible layer, since
    "which collection is that in" is the question you're asking when you
    can't find something)."""
    q = (request.query_params.get("q") or "").strip()
    return page(request, "collections.html", active="collections", q=q,
                hits=data.search_item_titles(q) if q else None,
                **data.collections_overview())


@app.get("/collections/{name}")
async def collection(request: Request, name: str):
    c = data.collection_page(name)
    if c is None:
        return page(request, "notfound.html", active="collections",
                    status_code=404, what="collection")
    return page(request, "collection.html", active="collections", c=c)


@app.post("/collections/{name}/display")
async def collection_display(request: Request, name: str):
    """Save the collection page's Display popover: the view, which
    declared fields show, how the items are grouped/sorted, and the list view's
    extras. A website-only write path through server.set_collection_display
    (never a FastMCP tool — the model proposes a collection's shape at creation;
    what renders is the user's call). Body: {view?, hidden_fields?,
    group_by?, sort_by?, sort_dir?, show_body?, show_updated?, image_size?}."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    res = server.set_collection_display(
        name,
        view=body.get("view"),
        hidden_fields=body.get("hidden_fields"),
        group_by=body.get("group_by"), sort_by=body.get("sort_by"),
        sort_dir=body.get("sort_dir"),
        show_body=body.get("show_body"),
        show_updated=body.get("show_updated"), image_size=body.get("image_size"))
    code = 400 if isinstance(res, dict) and res.get("error") else 200
    return JSONResponse(res, status_code=code)


@app.get("/item/{item_id}")
async def item(request: Request, item_id: int):
    it = data.item_page(item_id)
    if it is None:
        return page(request, "notfound.html", active="collections",
                    status_code=404, what="item")
    return page(request, "item.html", active="collections", it=it)


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
