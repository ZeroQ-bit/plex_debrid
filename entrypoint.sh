#!/bin/sh
# entrypoint.sh — supervise the web UI (foreground) and let it manage the
# plex_debrid engine subprocess.
#
# Why the web UI is the foreground process: it must stay up so Umbrel's
# app_proxy always has something to serve, even if the engine is stopped or
# crashed. The web UI starts/stops/restarts main.py as a child.
set -eu

CONFIG_DIR="${PD_CONFIG_DIR:-/config}"
LOG_DIR="${PD_LOG_DIR:-/logs}"

mkdir -p "${CONFIG_DIR}" "${LOG_DIR}"

# Seed a minimal settings.json on first run so plex_debrid can boot headless
# without prompting. The user fills in the rest via the Web UI. plex_debrid
# stores booleans as the strings "true"/"false".
if [ ! -f "${CONFIG_DIR}/settings.json" ]; then
    echo "[entrypoint] seeding initial settings.json"
    cat > "${CONFIG_DIR}/settings.json" <<'EOF'
{
    "version": ["2.95", "Settings compatible update", []],
    "Debrid Services": ["TorBox"],
    "TorBox API Key": "",
    "Content Services": [],
    "Library collection service": [],
    "Library update services": [],
    "Library ignore services": [],
    "Sources": [],
    "Versions": [],
    "Show Menu on Startup": "false",
    "Debug printing": "true",
    "Log to file": "true"
}
EOF
fi

export PD_CONFIG_DIR PD_LOG_DIR

echo "[entrypoint] starting web UI on :8080 (config=${CONFIG_DIR}, logs=${LOG_DIR})"
exec python3 /app/web_ui/server.py
