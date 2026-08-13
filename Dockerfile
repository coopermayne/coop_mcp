FROM python:3.12-slim

WORKDIR /app

# MCP server deps + web UI deps (one image runs both — see webapp/combined.py).
COPY requirements.txt ./requirements-base.txt
COPY webapp/requirements.txt ./requirements-web.txt
RUN pip install --no-cache-dir -r requirements-base.txt -r requirements-web.txt

# server.py imports `icons` at module level — a missing icons.py is an import-time crash,
# not a degraded feature, so it ships with the server.
COPY server.py icons.py ./
COPY webapp ./webapp
# Maintenance/seed scripts (e.g. import_exercises.py for seeding the exercise library) —
# run inside the container against the mounted DB.
COPY scripts ./scripts

# Remote mode. DB lives on a mounted volume so it survives redeploys. One process serves
# the journal MCP (+ read-only UI at /app) on PUBLIC_URL and, when TRAINER_PUBLIC_URL is
# set, the trainer MCP on that second host (each with its own root OAuth).
# FASTMCP_HOME is load-bearing for redeploys. FastMCP's OAuth proxy keeps its client
# registrations and upstream token sets in an encrypted file store under
# `settings.home`, which defaults to a per-user data dir INSIDE the container — wiped
# on every deploy, so Claude's connector loses its registration and has to re-auth
# each time. Pointing it at the same persistent volume as the DB makes the auth state
# outlive a redeploy. (The store's subdirectory is keyed off the Google client secret,
# so it stays stable across restarts but rotates if you rotate the secret.)
ENV MCP_TRANSPORT=http \
    PORT=8000 \
    JOURNAL_DB=/data/journal.db \
    FASTMCP_HOME=/data/fastmcp

EXPOSE 8000

# Create the volume mount point; Coolify maps a persistent volume here.
RUN mkdir -p /data
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["python", "webapp/combined.py"]
