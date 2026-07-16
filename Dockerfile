# plex_debrid + TorBox provider + Web UI
# Single image: Python 3.11 + plex_debrid (vendored, TorBox patched) + web_ui.
#
# The container runs two processes, supervised by the entrypoint:
#   1. web_ui/server.py  on :8080  (the Umbrel app_proxy target)
#   2. plex_debrid main.py -service (child subprocess managed by the web UI)
FROM python:3.11-slim

# Install OS deps. No mount/FUSE here — plex_debrid never mounts; the separate
# zeroq-debrid-mount app owns the rclone mount.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps for plex_debrid (vendored requirements.txt).
COPY plex_debrid/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Vendored plex_debrid source (already patched with the TorBox provider).
COPY plex_debrid /app/plex_debrid

# Web UI (stdlib-only; no extra pip deps).
COPY web_ui /app/web_ui
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /app/web_ui/server.py 2>/dev/null || true

ENV PYTHONUNBUFFERED=1 \
    PD_CONFIG_DIR=/config \
    PD_LOG_DIR=/logs

# /config holds settings.json (bind-mounted from ${APP_DATA_DIR}/config).
# /logs holds pd.log (bind-mounted from ${APP_DATA_DIR}/logs).
RUN mkdir -p /config /logs

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
