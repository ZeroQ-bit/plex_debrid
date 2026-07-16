#!/usr/bin/env python3
"""server.py — Web UI + engine supervisor for the plex_debrid Umbrel app.

Stdlib-only HTTP server (no Flask/FastAPI/pip deps) that:
  - serves the dashboard SPA at / and /index.html
  - exposes a JSON API to read/edit plex_debrid's settings.json
  - tests the TorBox API key against TorBox's /user/me
  - launches/stops/restarts the plex_debrid engine as a child subprocess
  - tails the engine log for the dashboard

The server is the container's foreground process (started by entrypoint.sh);
the engine is a child it manages. If the engine crashes, the server stays up.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from settings_bridge import SettingsStore  # noqa: E402
import auth  # noqa: E402

PD_ROOT = os.environ.get("PD_ROOT", "/app/plex_debrid")
CONFIG_DIR = os.environ.get("PD_CONFIG_DIR", "/config")
LOG_DIR = os.environ.get("PD_LOG_DIR", "/logs")
PD_LOG = os.path.join(LOG_DIR, "pd.log")
ENGINE_PIDFILE = os.path.join(LOG_DIR, "engine.pid")
LISTEN_PORT = int(os.environ.get("PD_WEB_PORT", "8080"))

store = SettingsStore(CONFIG_DIR)


# --------------------------------------------------------------------------
# Engine subprocess management
# --------------------------------------------------------------------------
class Engine:
    """Supervises the plex_debrid main.py -service child process."""

    def __init__(self):
        self.proc = None
        self._lock = threading.Lock()

    @property
    def running(self):
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "already running"
            main_py = os.path.join(PD_ROOT, "main.py")
            if not os.path.isfile(main_py):
                return False, f"engine not found at {main_py}"
            os.makedirs(LOG_DIR, exist_ok=True)
            log_fh = open(PD_LOG, "ab")
            # plex_debrid is a CLI script that calls input() even in service
            # mode when preflight fails. Feed it /dev/null on stdin so it gets
            # EOF rather than blocking the subprocess forever.
            self.proc = subprocess.Popen(
                [sys.executable, os.path.join(PD_ROOT, "main.py"),
                 "--config-dir", CONFIG_DIR, "-service"],
                cwd=PD_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            self._write_pidfile()
            return True, f"started pid={self.proc.pid}"

    def stop(self):
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = None
                self._clear_pidfile()
                return False, "not running"
            pid = self.proc.pid
            try:
                # Try graceful first, then escalate.
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            finally:
                self.proc = None
                self._clear_pidfile()
            return True, f"stopped pid={pid}"

    def restart(self):
        self.stop()
        time.sleep(1)
        return self.start()

    def status(self):
        return {"running": self.running, "pid": self.proc.pid if self.running else None}

    def _write_pidfile(self):
        try:
            with open(ENGINE_PIDFILE, "w") as fh:
                fh.write(str(self.proc.pid if self.proc else ""))
        except OSError:
            pass

    def _clear_pidfile(self):
        try:
            os.remove(ENGINE_PIDFILE)
        except OSError:
            pass


engine = Engine()


def tail(path, lines=200):
    """Return the last `lines` lines of a file as a string."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            chunk = fh.readlines()[-lines:]
        return b"".join(chunk).decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
STATIC_DIR = os.path.join(HERE, "static")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "plex-debrid-ui/1.0"

    def log_message(self, *args):
        pass  # quiet

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200, content_type="text/plain"):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # --- GET routes -------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path == "/api/health":
            return self._send_json({"ok": True, "name": "plex-debrid"})
        if path == "/api/schema":
            return self._send_json(store.schema())
        if path == "/api/settings":
            return self._send_json(store.load_grouped())
        if path == "/api/status":
            return self._send_json({
                "engine": engine.status(),
                "configured": store.exists(),
                "log_tail": tail(PD_LOG, 200),
            })
        if path == "/api/logs":
            return self._send_json({"lines": tail(PD_LOG, 500)})
        if path == "/api/plex/pin/poll":
            pin_id = (qs.get("id") or [""])[0]
            if not pin_id:
                return self._send_json({"error": "missing id"}, 400)
            return self._send_json(auth.plex_poll_pin(pin_id, CONFIG_DIR))
        if path == "/api/trakt/device/poll":
            code = (qs.get("code") or [""])[0]
            if not code:
                return self._send_json({"error": "missing code"}, 400)
            return self._send_json(auth.trakt_poll(code))
        if path == "/api/overseerr/users":
            base = (qs.get("base") or [""])[0]
            key = (qs.get("key") or [""])[0]
            return self._send_json(auth.overseerr_users(base, key))
        if path == "/api/plex/sections":
            raw = store.load_raw()
            server = raw.get("Plex server address", "")
            users = raw.get("Plex users", [])
            token = users[0][1] if users else ""
            return self._send_json(auth.plex_library_sections(server, token))
        # static asset fallback
        asset = path.lstrip("/")
        if asset and os.path.isfile(os.path.join(STATIC_DIR, asset)):
            return self._serve_static(asset)
        # SPA fallback: unknown non-API routes serve index.html
        if not path.startswith("/api/"):
            return self._serve_static("index.html")
        return self._send_json({"error": "not found"}, 404)

    # --- POST routes ------------------------------------------------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/settings":
            body = self._read_json()
            if body is None:
                return self._send_json({"error": "invalid json"}, 400)
            store.apply_edits(body)
            return self._send_json({"ok": True, "saved": True})
        if path == "/api/plex/pin/start":
            return self._send_json(auth.plex_start_pin(CONFIG_DIR))
        if path == "/api/trakt/device/start":
            return self._send_json(auth.trakt_start())
        if path == "/api/test-debrid":
            body = self._read_json() or {}
            provider = body.get("provider", "")
            key = body.get("api_key")
            if not key:
                # fall back to the stored key for this provider
                field = "{} API Key".format(provider)
                key = store.load_raw().get(field, "")
            return self._send_json(auth.test_debrid(provider, key))
        if path == "/api/engine/start":
            ok, msg = engine.start()
            return self._send_json({"ok": ok, "message": msg, "engine": engine.status()})
        if path == "/api/engine/stop":
            ok, msg = engine.stop()
            return self._send_json({"ok": ok, "message": msg, "engine": engine.status()})
        if path == "/api/engine/restart":
            ok, msg = engine.restart()
            return self._send_json({"ok": ok, "message": msg, "engine": engine.status()})
        return self._send_json({"error": "not found"}, 404)

    def _serve_static(self, name):
        full = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(full):
            return self._send_text("not found", 404)
        ext = os.path.splitext(name)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            self._send_text(fh.read(), 200, ctype)

    def do_OPTIONS(self):
        # Permissive CORS for the dashboard (consistent with Vortexo server).
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def endheaders_with_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    # Patch: add CORS header to all responses by wrapping end_headers.
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    # Auto-start the engine if a settings.json already exists and looks ready.
    if store.exists():
        raw = store.load_raw()
        if raw.get("TorBox API Key") or raw.get("Real Debrid API Key"):
            try:
                engine.start()
            except Exception as e:
                print(f"[web_ui] auto-start failed: {e}", file=sys.stderr)
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[web_ui] listening on :{LISTEN_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        server.server_close()


if __name__ == "__main__":
    main()
