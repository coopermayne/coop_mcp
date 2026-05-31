FROM python:3.12-slim

WORKDIR /app

# MCP server deps + web UI deps (one image runs both — see webapp/combined.py).
COPY requirements.txt ./requirements-base.txt
COPY webapp/requirements.txt ./requirements-web.txt
RUN pip install --no-cache-dir -r requirements-base.txt -r requirements-web.txt

COPY server.py .
COPY webapp ./webapp

# Remote mode. DB lives on a mounted volume so it survives redeploys. One process serves
# the journal MCP (+ read-only UI at /app) on PUBLIC_URL and, when TRAINER_PUBLIC_URL is
# set, the trainer MCP on that second host (each with its own root OAuth).
ENV MCP_TRANSPORT=http \
    PORT=8000 \
    JOURNAL_DB=/data/journal.db

EXPOSE 8000

# Create the volume mount point; Coolify maps a persistent volume here.
RUN mkdir -p /data
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["python", "webapp/combined.py"]
