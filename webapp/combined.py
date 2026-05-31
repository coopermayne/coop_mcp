"""
Single-process entrypoint: the MCP server and the read-only web UI in one app.

There are two MCP servers in this one process, sharing one DB but exposed at
separate endpoints so each Claude project loads only its own tools:
  - the journal+drinking server at the origin root — `/mcp` plus its OAuth discovery
    (`/.well-known/*`, `/auth/callback`) — exactly as when run alone, and
  - the trainer server under `/trainer` — connector at `/trainer/mcp`, with its own
    OAuth discovery and callback at `/trainer/auth/callback`.
The browser UI is mounted under `/app`, so it never collides with either MCP's
OAuth. One container, one port, one domain.

Run:
    MCP_TRANSPORT=http PORT=8000 JOURNAL_DB=./journal.db python webapp/combined.py
Then: UI at http://localhost:8000/app, journal connector at .../mcp, trainer
connector at .../trainer/mcp.
"""

import os
import sys

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
from starlette.routing import Mount, Route       # noqa: E402

# Each MCP server builds its own ASGI app at the ORIGIN ROOT (not sub-mounted), so its
# OAuth discovery docs land where clients look — at the root, not under a prefix.
mcp_app = server.mcp.http_app(path="/mcp")                       # journal + drinking
trainer_app = server.trainer_mcp.http_app(path="/trainer/mcp")   # trainer

# The journal app already carries the shared OAuth authorization-server routes
# (/authorize, /token, /register, /auth/callback, /consent, the AS metadata) plus
# /mcp, /health and the journal protected-resource metadata. From the trainer app we
# graft on ONLY its two unique routes — its MCP endpoint and its own protected-resource
# metadata — so the trainer connector authenticates against the same root OAuth server
# but advertises a discovery doc that actually resolves at the root.
_TRAINER_ONLY = {"/trainer/mcp", "/.well-known/oauth-protected-resource/trainer/mcp"}
_trainer_routes = [r for r in trainer_app.routes
                   if getattr(r, "path", None) in _TRAINER_ONLY]


@contextlib.asynccontextmanager
async def _lifespan(app):
    # Both apps run their own StreamableHTTP session manager; enter both or the
    # grafted-in trainer endpoint has no live session manager.
    async with mcp_app.lifespan(app), trainer_app.lifespan(app):
        yield


async def _app_root_redirect(request):
    # Bare "/app" (no trailing slash) doesn't reach the mounted sub-app's "/" route,
    # so send it to "/app/" explicitly.
    return RedirectResponse(url="/app/")


# Parent app: UI under /app, the trainer's two routes, then the whole journal app at
# "/" (the catch-all carrying root OAuth, /mcp, /health). Specific routes precede it.
application = Starlette(
    lifespan=_lifespan,
    routes=[
        Route("/app", _app_root_redirect),
        Mount("/app", app=webapp.app),
        *_trainer_routes,
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
