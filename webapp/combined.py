"""
Single-process entrypoint: the trainer MCP server and the web UI in one app,
sharing one DB.

  - Main origin (PUBLIC_URL): the browser UI under `/app` (with `/` redirecting
    there) and `/health`. The journal is written in the web app's own chat, so it
    has no MCP endpoint of its own.
  - Trainer: a full OAuth server, so when TRAINER_PUBLIC_URL is set it gets its OWN
    host — we route that hostname to the trainer app at the root, so its connector
    is `https://<trainer-host>/mcp` with clean root OAuth (discovery, /authorize,
    /auth/callback). With TRAINER_PUBLIC_URL unset (local/authless), it falls back
    to `/trainer/mcp` on the main origin.

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
import server          # noqa: E402  the trainer MCP server + the shared data layer
import app as webapp   # noqa: E402  the FastAPI UI

from starlette.applications import Starlette   # noqa: E402
from starlette.responses import RedirectResponse  # noqa: E402
from starlette.routing import Host, Mount, Route   # noqa: E402

# Apply schema + migrations BEFORE building the apps. `init_db()` only runs from
# server.py's __main__ block, which never fires in prod (the Dockerfile CMD runs
# this module). Without this call, ALTER TABLE migrations added in init_db()
# never reach the live DB, and any query that references a new column 500s.
server.init_db()

_trainer_host = urlparse(os.environ.get("TRAINER_PUBLIC_URL", "")).netloc
if _trainer_host:
    # Own-host mode: the whole trainer app at the root of its own hostname.
    trainer_app = server.trainer_mcp.http_app(path="/mcp")
    _trainer_routes = [Host(_trainer_host, app=trainer_app)]
else:
    # Same-origin fallback (authless/local only): graft the endpoint and its
    # protected-resource metadata onto the main origin.
    trainer_app = server.trainer_mcp.http_app(path="/trainer/mcp")
    _keep = {"/trainer/mcp", "/.well-known/oauth-protected-resource/trainer/mcp"}
    _trainer_routes = [r for r in trainer_app.routes if getattr(r, "path", None) in _keep]


@contextlib.asynccontextmanager
async def _lifespan(app):
    # The trainer app runs its own StreamableHTTP session manager; enter it or the
    # endpoint has no live session manager.
    async with trainer_app.lifespan(app):
        yield


async def _app_root_redirect(request):
    # Bare "/app" (no trailing slash) doesn't reach the mounted sub-app's "/" route,
    # and the main origin's root has nothing else to show — send both to "/app/".
    return RedirectResponse(url="/app/")


# Trainer routes first (the Host matches in prod, or the grafted same-origin routes
# locally), then /health, then the UI under /app.
application = Starlette(
    lifespan=_lifespan,
    routes=[
        *_trainer_routes,
        Route("/health", server.health),
        Route("/", _app_root_redirect),
        Route("/app", _app_root_redirect),
        Mount("/app", app=webapp.app),
    ],
)


if __name__ == "__main__":
    import uvicorn
    # Behind Coolify's reverse proxy: trust X-Forwarded-* so request scheme/host
    # resolve to the public HTTPS origin (the container only sees proxied traffic).
    uvicorn.run(application, host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
