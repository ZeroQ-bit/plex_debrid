"""Periodic library-symlink sweep for the plex_debrid web UI.

Runs as a daemon thread started from server.main(). Every N minutes it calls
library_symlinker.sweep() to reconcile the Plex library ({tmdb-ID}/{tvdb-ID}
folders) against the raw TorBox mount, retroactively symlinking anything
missing — both for items added outside plex_debrid and as a safety net for
the immediate per-download symlink.

Reads its config from settings.json via the shared SettingsStore, so the
interval/enable toggle are user-configurable from the web UI. Logs to the
engine log (pd.log) with a [symlinker] prefix so it appears in the dashboard.
"""
import datetime
import os
import sys
import threading
import time

PD_ROOT = os.environ.get("PD_ROOT", "/app/plex_debrid")
if PD_ROOT not in sys.path:
    sys.path.insert(0, PD_ROOT)

import library_symlinker  # noqa: E402

PD_LOG = os.path.join(os.environ.get("PD_LOG_DIR", "/logs"), "pd.log")

# Sensible defaults if a key is absent from settings.json.
_DEFAULTS = {
    "enabled": "true",
    "interval_minutes": "15",
    "mount_dir": os.environ.get("PD_DOWNLOADS_DIR", "/downloads"),
    "movies_dir": os.environ.get("PD_LIBRARY_MOVIES_DIR", "/downloads/vortexo/Movies"),
    "tv_dir": os.environ.get("PD_LIBRARY_TV_DIR", "/downloads/vortexo/TV"),
}


def _log(msg):
    line = f"[{datetime.datetime.now():%Y/%m/%d %H:%M:%S}] [symlinker] {msg}\n"
    try:
        with open(PD_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        # Fall back to stderr (docker logs) if pd.log isn't writable.
        print(line, end="", file=sys.stderr)


def _read_config(store):
    """Pull symlinker config from settings.json, falling back to env/defaults."""
    try:
        raw = store.load_raw() or {}
    except Exception:
        raw = {}

    def _get(key, default):
        v = raw.get(key)
        return v if v not in (None, "") else default

    return {
        "enabled": str(_get("Symlinker Enabled", _DEFAULTS["enabled"])).lower(),
        "interval_minutes": str(_get("Symlinker Interval",
                                     _DEFAULTS["interval_minutes"])),
        "mount_dir": _get("Symlinker Mount Path", _DEFAULTS["mount_dir"]),
        "movies_dir": _get("Symlinker Movies Library",
                           _DEFAULTS["movies_dir"]),
        "tv_dir": _get("Symlinker TV Library", _DEFAULTS["tv_dir"]),
        "api_key": _get("TorBox API Key", ""),
    }


def _parse_interval_minutes(s, default=15):
    try:
        v = float(s)
        # Clamp to a sane range: 1 minute .. 24 hours.
        return max(1.0, min(v, 1440.0))
    except (TypeError, ValueError):
        return default


def sweep_once(store):
    """Run one reconciliation pass. Returns the number of new symlinks."""
    cfg = _read_config(store)
    if cfg["enabled"] != "true":
        return 0
    if not cfg["api_key"]:
        _log("sweep skipped: no TorBox API key configured")
        return 0
    library_dirs = {"movie": cfg["movies_dir"], "tv": cfg["tv_dir"]}
    count = library_symlinker.sweep(
        cfg["api_key"], cfg["mount_dir"], library_dirs, log_fn=_log)
    return count


def sweep_loop(store, stop_event):
    """Background loop: sweep every N minutes until stop_event is set."""
    # Run an initial pass shortly after startup (don't block server boot).
    time.sleep(10)
    while not stop_event.is_set():
        try:
            cfg = _read_config(store)
            if cfg["enabled"] == "true":
                _log("starting sweep")
                sweep_once(store)
            interval = _parse_interval_minutes(cfg["interval_minutes"]) * 60
        except Exception as e:
            _log(f"sweep loop error: {e!r}")
            interval = 15 * 60
        # wait() returns True if set, letting us shut down promptly.
        stop_event.wait(interval)
    _log("sweep loop stopped")
