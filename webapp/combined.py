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

mcp_app = server.mcp.http_app(path="/mcp")   # journal + drinking, on the main origin

# Trainer host derived from TRAINER_PUBLIC_URL (e.g. https://trainer.example.com).
# Tolerate a scheme-less value ("trainer.example.com") — without a scheme urlparse puts
# the host in .path and .netloc is empty, which would silently drop us into fallback
# mode and serve the JOURNAL on the trainer host.
_trainer_public = os.environ.get("TRAINER_PUBLIC_URL", "").strip()
if _trainer_public and "://" not in _trainer_public:
    _trainer_public = "https://" + _trainer_public
TRAINER_HOST = urlparse(_trainer_public).netloc

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
    async with mcp_app.lifespan(app), trainer_app.lifespan(app):
        yield


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
