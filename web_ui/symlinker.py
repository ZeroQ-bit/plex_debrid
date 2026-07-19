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
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PD_ROOT = os.environ.get("PD_ROOT", "/app/plex_debrid")
if PD_ROOT not in sys.path:
    sys.path.insert(0, PD_ROOT)

import library_symlinker  # noqa: E402

PD_LOG = os.path.join(os.environ.get("PD_LOG_DIR", "/logs"), "pd.log")
PENDING_SCANS_FILE = os.path.join(
    os.environ.get("PD_CONFIG_DIR", "/config"),
    "symlinker-pending-scans.json")

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


def _plex_token(raw):
    users = raw.get("Plex users", [])
    if (users and isinstance(users[0], (list, tuple))
            and len(users[0]) > 1):
        return str(users[0][1])
    return ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the Plex token from being forwarded across HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PLEX_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _plex_get(url, token, timeout=30):
    """Authenticated Plex GET without placing the token in URLs or logs."""
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Plex-Token": token,
    })
    with _PLEX_OPENER.open(request, timeout=timeout) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        return status, response.read().decode("utf-8", errors="replace")


def _plex_sections(base_url, token):
    """Return [{key, type, locations}] for the server's library sections."""
    status, body = _plex_get(base_url.rstrip("/") + "/library/sections/",
                             token)
    if status != 200:
        raise RuntimeError("Plex sections returned HTTP {}".format(status))
    directories = []
    try:
        data = json.loads(body)
        directories = data.get("MediaContainer", data).get("Directory", [])
    except (TypeError, ValueError, AttributeError):
        root = ET.fromstring(body)
        for node in root.findall(".//Directory"):
            directories.append({
                "key": node.attrib.get("key", ""),
                "type": node.attrib.get("type", ""),
                "refreshing": node.attrib.get("refreshing", "0"),
                "scannedAt": node.attrib.get("scannedAt", ""),
                "updatedAt": node.attrib.get("updatedAt", ""),
                "Location": [
                    {"path": child.attrib.get("path", "")}
                    for child in node.findall("Location")
                ],
            })
    if isinstance(directories, dict):
        directories = [directories]
    result = []
    for directory in directories or []:
        locations = directory.get("Location", []) or []
        if isinstance(locations, dict):
            locations = [locations]
        result.append({
            "key": str(directory.get("key", "")),
            "type": str(directory.get("type", "")),
            "refreshing": directory.get("refreshing", False),
            "scanned_at": str(directory.get("scannedAt", "")),
            "updated_at": str(directory.get("updatedAt", "")),
            "locations": [str(item.get("path", "")) for item in locations
                          if isinstance(item, dict) and item.get("path")],
        })
    return result


def _request_plex_scan(base_url, token, section, path=None):
    endpoint = (base_url.rstrip("/") + "/library/sections/"
                + urllib.parse.quote(str(section), safe="") + "/refresh")
    if path:
        endpoint += "?" + urllib.parse.urlencode({"path": path})
    status, _ = _plex_get(endpoint, token)
    if status != 200:
        raise RuntimeError("Plex refresh returned HTTP {}".format(status))


def _is_refreshing(value):
    return value is True or str(value).lower() in ("1", "true")


def _scan_marker(section):
    return (section.get("scanned_at", ""), section.get("updated_at", ""))


def _wait_for_plex_scan(base_url, token, section_key, before_marker,
                        timeout=180, poll_interval=1.0):
    """Require evidence that Plex actually ran the accepted section scan."""
    deadline = time.monotonic() + timeout
    saw_refreshing = False
    while time.monotonic() < deadline:
        sections = _plex_sections(base_url, token)
        section = next((item for item in sections
                        if item["key"] == str(section_key)), None)
        if section is not None:
            refreshing = _is_refreshing(section.get("refreshing"))
            marker_changed = (_scan_marker(section) != before_marker
                              and any(_scan_marker(section)))
            if refreshing:
                saw_refreshing = True
            elif saw_refreshing or marker_changed:
                return True
        time.sleep(poll_interval)
    return False


def _coalesce_changes(changed_paths):
    changed = []
    seen = set()
    for value in changed_paths or []:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        pair = (str(value[0]), str(value[1]))
        if pair[0] not in ("movie", "tv") or not pair[1] or pair in seen:
            continue
        seen.add(pair)
        changed.append(pair)
    return changed


def _load_pending_scans():
    try:
        with open(PENDING_SCANS_FILE, "r", encoding="utf-8") as fh:
            return _coalesce_changes(json.load(fh))
    except FileNotFoundError:
        return []
    except (OSError, TypeError, ValueError) as e:
        _log("could not load pending Plex scans: {}".format(e))
        return []


def _save_pending_scans(changed_paths):
    pending = _coalesce_changes(changed_paths)
    temp_path = PENDING_SCANS_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(PENDING_SCANS_FILE), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump([list(pair) for pair in pending], fh,
                      ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temp_path, PENDING_SCANS_FILE)
        return True
    except OSError as e:
        _log("could not save pending Plex scans: {}".format(e))
        return False


def _path_within_location(path, location):
    try:
        candidate = os.path.abspath(path)
        root = os.path.abspath(location)
        return os.path.commonpath([candidate, root]) == root
    except (OSError, ValueError):
        return False


def _refresh_plex(store, changed_paths):
    """Request one full scan per affected configured Plex section.

    A sweep can repair many shows at once. One section scan avoids racing a
    burst of partial scans against Plex's single scanner queue. Successful
    changes are returned so the persistent pending queue can retain failures.
    """
    raw = store.load_raw() or {}
    services = raw.get("Library update services", []) or []
    if not isinstance(services, list):
        services = [services]
    if "Plex Libraries" not in services:
        _log("Plex scan deferred after sweep: Plex Libraries update service disabled")
        return set()
    base_url = str(raw.get("Plex server address", "")).strip()
    token = _plex_token(raw)
    configured = raw.get("Plex library refresh", []) or []
    if not isinstance(configured, list):
        configured = [configured]
    configured = {str(value) for value in configured if str(value)}
    if not base_url or not token or not configured:
        _log("Plex scan deferred after sweep: server, token, or refresh section missing")
        return set()

    changed = _coalesce_changes(changed_paths)
    changes_by_kind = {
        "movie": {pair for pair in changed if pair[0] == "movie"},
        "tv": {pair for pair in changed if pair[0] == "tv"},
    }
    try:
        sections = _plex_sections(base_url, token)
    except Exception as e:
        _log("Plex section discovery for pending scans failed: {}".format(e))
        return set()

    eligible = {"movie": [], "tv": []}
    for section in sections:
        kind = "tv" if section["type"] == "show" else section["type"]
        if (section["key"] in configured and kind in eligible
                and section["locations"]):
            eligible[kind].append(section)

    section_changes = {}
    pair_sections = {}
    for kind, pairs in changes_by_kind.items():
        for pair in pairs:
            direct = [
                section for section in eligible[kind]
                if any(_path_within_location(pair[1], location)
                       for location in section["locations"])
            ]
            # When Plex and the symlinker use different container mount paths,
            # direct containment is impossible. A single configured section of
            # this kind is still unambiguous; multiple sections fail closed.
            targets = direct
            if not targets and len(eligible[kind]) == 1:
                targets = eligible[kind]
            if not targets:
                _log("Plex scan remains pending: no unambiguous section for {}".format(
                    pair[1]))
                continue
            pair_sections[pair] = {section["key"] for section in targets}
            for section in targets:
                section_changes.setdefault(section["key"], {
                    "section": section, "pairs": set()})["pairs"].add(pair)

    section_results = {}
    for key, batch in section_changes.items():
        section = batch["section"]
        if _is_refreshing(section.get("refreshing")):
            section_results[key] = False
            _log("Plex scan remains pending: section {} is already scanning".format(
                key))
            continue
        try:
            _request_plex_scan(base_url, token, key)
            section_results[key] = _wait_for_plex_scan(
                base_url, token, key, _scan_marker(section))
            if section_results[key]:
                _log("verified Plex scan: section {} after {} recovered path(s)".format(
                    key, len(batch["pairs"])))
            else:
                _log("Plex accepted section {} scan but completion was not observed".format(
                    key))
        except Exception as e:
            section_results[key] = False
            _log("Plex scan failed for section {}: {}".format(
                key, e))

    successful = set()
    for pair, keys in pair_sections.items():
        if keys and all(section_results.get(key) for key in keys):
            successful.add(pair)
    return successful


def sweep_once(store):
    """Run one reconciliation pass. Returns the number of new symlinks."""
    cfg = _read_config(store)
    if cfg["enabled"] != "true":
        return 0
    if not cfg["api_key"]:
        _log("sweep skipped: no TorBox API key configured")
        return 0
    library_dirs = {"movie": cfg["movies_dir"], "tv": cfg["tv_dir"]}
    changed_paths = []
    count = library_symlinker.sweep(
        cfg["api_key"], cfg["mount_dir"], library_dirs, log_fn=_log,
        changed_paths=changed_paths)
    pending = _coalesce_changes(_load_pending_scans() + changed_paths)
    if pending:
        # Persist before contacting Plex so a process/container interruption
        # cannot lose the only scan notification for idempotently-linked media.
        _save_pending_scans(pending)
        successful = _refresh_plex(store, pending)
        cleared = set()
        for pair in successful:
            if library_symlinker.clear_scan_pending(pair[1], log_fn=_log):
                cleared.add(pair)
        remaining = [pair for pair in pending if pair not in cleared]
        _save_pending_scans(remaining)
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
