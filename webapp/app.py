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

import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import data            # noqa: E402  local data layer
import server          # noqa: E402  reused for db()/reads and ALLOWED_EMAILS

from fastapi import FastAPI, Request                          # noqa: E402
from fastapi.responses import RedirectResponse                # noqa: E402
from fastapi.staticfiles import StaticFiles                   # noqa: E402
from fastapi.templating import Jinja2Templates                # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware       # noqa: E402
from starlette.middleware.sessions import SessionMiddleware    # noqa: E402

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
WEB_BASE_URL = (os.environ.get("WEB_BASE_URL") or "").rstrip("/")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
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


def set_label(s):
    w, r, rpe = s.get("weight_lbs"), s.get("reps"), s.get("rpe")
    if w is not None and r is not None:
        base = f"{num(w)} × {r}"
    elif r is not None:
        base = f"{r} rep" + ("" if r == 1 else "s")
    elif w is not None:
        base = f"{num(w)} lb"
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


def base_path(request: Request) -> str:
    """The mount prefix this app is served under ('/app' when embedded in the MCP
    process, '' when run standalone). Internal links/redirects are prefixed with it."""
    return request.scope.get("root_path", "") or ""


def page(request: Request, template: str, active: str = "", status_code: int = 200, **ctx):
    ctx.update(active=active, auth_enabled=AUTH_ENABLED,
               user=request.session.get("email"), base=base_path(request))
    return templates.TemplateResponse(request=request, name=template,
                                      context=ctx, status_code=status_code)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

PUBLIC_PATHS = {"/login", "/auth/login", "/auth/callback", "/health"}

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
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        if request.session.get("email"):
            return await call_next(request)
        return RedirectResponse((request.scope.get("root_path", "") or "") + "/login")


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
# Pages
# --------------------------------------------------------------------------- #

@app.get("/")
async def index(request: Request):
    return page(request, "index.html", active="home", d=data.dashboard())


@app.get("/journal")
async def journal(request: Request, q: str = ""):
    q = (q or "").strip()
    if q:
        res = server.search_entries(q, limit=40)
        return page(request, "journal.html", active="journal",
                    q=q, entries=res["results"], count=res["count"], searching=True)
    res = data.list_entries(limit=50)
    return page(request, "journal.html", active="journal",
                q="", entries=res["entries"], count=res["total"], searching=False)


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
    totals = {
        "sessions": len(sessions),
        "exercises": sum(s["exercise_count"] for s in sessions),
        "sets": sum(s["set_count"] for s in sessions),
    }
    return page(request, "workouts.html", active="workouts",
                sessions=sessions, totals=totals,
                muscles=brief.get("muscle_recency", []),
                profile=brief.get("profile", {}))


@app.get("/drinking")
async def drinking(request: Request):
    s = data.drinking(days=30)
    return page(request, "drinking.html", active="drinking",
                s=s, log=data.recent_drinks(limit=30))


@app.get("/people")
async def people(request: Request, q: str = ""):
    q = (q or "").strip()
    res = server.list_people(query=q or None)
    return page(request, "people.html", active="people",
                q=q, people=res["people"], count=res["count"])


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
