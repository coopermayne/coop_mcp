FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Remote mode. DB lives on a mounted volume so it survives redeploys.
# FASTMCP_HOME also points at /data so the OAuth proxy's client registrations
# and refresh tokens persist across redeploys — otherwise every push wipes them
# and you're forced to re-authenticate the connector.
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

CMD ["python", "server.py"]
