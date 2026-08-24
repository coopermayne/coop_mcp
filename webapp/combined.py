"""
Single-process entrypoint: both MCP servers and the read-only web UI in one app,
sharing one DB so each Claude project loads only its own tools.

  - Journal + drinking: served on the main origin (PUBLIC_URL). `/mcp` plus its OAuth
    discovery (`/.well-known/*`, `/auth/callback`) and the browser UI under `/app`.
  - Trainer: a full OAuth server can't share an origin with the journal (their
    `/authorize`, `/token`, `/auth/callback` paths collide), so when TRAINER_PUBLIC_URL
    is set the trainer gets its OWN host. We route that hostname to the trainer app at
    the root, so its connector is `https://<trainer-host>/mcp` with clean root OAuth.
    With TRAINER_PUBLIC_URL unset (local/authless), the trainer falls back to
    `/trainer/mcp` on the main origin (no OAuth, so the same-origin collision is moot).

Run (local, one origin, trainer at /trainer/mcp):
    MCP_TRANSPORT=http PORT=8000 JOURNAL_DB=./journal.db python webapp/combined.py
Prod (trainer on its own host): also set TRAINER_PUBLIC_URL=https://<trainer-host> and
point that domain at this same Coolify service.
"""

import os
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import contextlib     # noqa: E402
import server          # noqa: E402  the MCP servers (`mcp`, `trainer_mcp`) + reads
import app as webapp   # noqa: E402  the FastAPI UI

from starlette.applications import Starlette   # noqa: E402
from starlette.responses import RedirectResponse  # noqa: E402
from starlette.routing import Host, Mount, Route   # noqa: E402

# Apply schema + migrations BEFORE building the apps. `init_db()` only runs from
# server.py's __main__ block, which never fires in prod (the Dockerfile CMD runs
# this module). Without this call, ALTER TABLE migrations added in init_db()
# never reach the live DB, and any query that references a new column 500s.
server.init_db()

mcp_app = server.mcp.http_app(path="/mcp")   # journal + drinking, on the main origin

# Trainer host derived from TRAINER_PUBLIC_URL (e.g. https://trainer.example.com).
TRAINER_HOST = urlparse(os.environ.get("TRAINER_PUBLIC_URL", "")).netloc

# Own-host mode (TRAINER_HOST set): trainer app at the root of its own hostname → clean
# root OAuth. Otherwise same-origin fallback: trainer at /trainer/mcp on the main origin
# (authless/local only — two real OAuth servers can't share one origin).
trainer_app = server.trainer_mcp.http_app(path="/mcp" if TRAINER_HOST else "/trainer/mcp")

if TRAINER_HOST:
    _trainer_routes = [Host(TRAINER_HOST, app=trainer_app)]
else:
    _same_origin = {"/trainer/mcp", "/.well-known/oauth-protected-resource/trainer/mcp"}
    _trainer_routes = [r for r in trainer_app.routes
                       if getattr(r, "path", None) in _same_origin]


@contextlib.asynccontextmanager
async def _lifespan(app):
    # Both apps run their own StreamableHTTP session manager; enter both or the
    # trainer endpoint has no live session manager.
    #
    # The UI app is MOUNTED, and Starlette does not run a mounted app's lifespan —
    # so webapp.app's own _lifespan never fires here and the Telegram bots would
    # never start in prod. Starting them from this outer lifespan is that fix; the
    # two paths call the same pair of functions, and only one of them ever runs.
    async with mcp_app.lifespan(app), trainer_app.lifespan(app):
        await webapp.tg.startup()
        try:
            yield
        finally:
            await webapp.tg.shutdown()


async def _app_root_redirect(request):
    # Bare "/app" (no trailing slash) doesn't reach the mounted sub-app's "/" route,
    # so send it to "/app/" explicitly.
    return RedirectResponse(url="/app/")


# Trainer route(s) first (the Host match in prod, or the grafted same-origin routes
# locally), then the UI under /app, then the journal app catch-all at "/" (root OAuth,
# /mcp, /health). Journal-host requests never match the trainer Host route, so the
# journal endpoint is byte-for-byte unchanged.
application = Starlette(
    lifespan=_lifespan,
    routes=[
        *_trainer_routes,
        Route("/app", _app_root_redirect),
        Mount("/app", app=webapp.app),
        Mount("/", app=mcp_app),
    ],
)


if __name__ == "__main__":
    import uvicorn
    # Behind Coolify's reverse proxy: trust X-Forwarded-* so request scheme/host
    # resolve to the public HTTPS origin (the container only sees proxied traffic).
    uvicorn.run(application, host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
