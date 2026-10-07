FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/verdicts.db

WORKDIR /workspace
COPY app ./app
COPY tests ./tests
COPY verify ./verify

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# stdlib-only health probe (slim image has no curl)
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-m", "app.server"]
