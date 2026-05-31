FROM python:3.12-slim

WORKDIR /app

# Web deps + the MCP server's deps (the webapp imports server.py for its reads).
COPY requirements.txt ./requirements-base.txt
COPY webapp/requirements.txt ./requirements-web.txt
RUN pip install --no-cache-dir -r requirements-base.txt -r requirements-web.txt

# server.py is the reused data layer; webapp/ is the frontend.
COPY server.py .
COPY webapp ./webapp

# Reads the SAME journal.db the MCP server writes — mount its volume here
# (read-only is fine; this app never writes).
ENV PORT=8001 \
    WEB_HOST=0.0.0.0 \
    JOURNAL_DB=/data/journal.db

EXPOSE 8001

RUN mkdir -p /data
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8001/health').status==200 else 1)"

CMD ["python", "webapp/app.py"]
