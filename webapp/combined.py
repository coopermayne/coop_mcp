"""
Single-process entrypoint: the MCP server and the read-only web UI in one app.

The MCP server keeps the origin root unchanged — `/mcp` for the connector plus its
OAuth discovery (`/.well-known/*`, `/auth/callback`) — exactly as when run alone.
The browser UI is mounted under `/app`, so it never collides with the MCP's
root-level OAuth. One container, one port, one domain.

Run:
    MCP_TRANSPORT=http PORT=8000 JOURNAL_DB=./journal.db python webapp/combined.py
The UI is then at http://localhost:8000/app and the connector at .../mcp.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import server          # noqa: E402  the MCP server (its `mcp` object + reads)
import app as webapp   # noqa: E402  the FastAPI UI

from starlette.applications import Starlette   # noqa: E402
from starlette.routing import Mount            # noqa: E402

# Starlette app for the MCP server (carries the session-manager lifespan).
mcp_app = server.mcp.http_app(path="/mcp")

# Parent app: UI under /app, everything else (/, /mcp, OAuth) to the MCP app.
# The parent must adopt the MCP app's lifespan or its session manager never starts.
application = Starlette(
    lifespan=mcp_app.lifespan,
    routes=[
        Mount("/app", app=webapp.app),
        Mount("/", app=mcp_app),
    ],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(application, host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")))
