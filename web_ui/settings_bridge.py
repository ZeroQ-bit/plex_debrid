"""settings_bridge — translate plex_debrid's flat settings.json to/from UI groups.

plex_debrid stores config as a FLAT json object with human-readable keys, e.g.
    "Debrid Services": ["TorBox"]
    "TorBox API Key": "..."
    "Content Services": ["Plex Watchlist", "Trakt Watchlist"]
Booleans are stored as the strings "true"/"false". This module loads that file,
groups the keys into a friendlier structure for the web UI, and writes the UI's
edited values back in plex_debrid's expected flat format.
"""
import json
import os
import threading

# Keys we manage in the UI, grouped by section. Each entry is
# (json_key, label, kind, help). kind in: text, password, bool, list, json.
SETTINGS_SCHEMA = [
    ("Debrid", [
        ("Debrid Services", "Providers", "list",
         "Debrid providers to use, e.g. [\"TorBox\"]"),
        ("TorBox API Key", "TorBox API Key", "password",
         "From TorBox → Settings → API Key. Required for TorBox."),
        ("Real Debrid API Key", "Real-Debrid API Key", "password",
         "Optional. Only if you also use RD."),
        ("All Debrid API Key", "AllDebrid API Key", "password", "Optional."),
        ("Premiumize API Key", "Premiumize API Key", "password", "Optional."),
    ]),
    ("Content Sources", [
        ("Content Services", "Watchlists", "list",
         "e.g. [\"Plex Watchlist\", \"Trakt Watchlist\", \"Overseerr\"]"),
        ("Plex users", "Plex Users", "json",
         "[[\"username\", \"plex-token\"], ...]"),
        ("Plex server address", "Plex Server URL", "text",
         "http://<plex-ip>:32400"),
        ("Trakt users", "Trakt Users", "json", "[[\"user\", \"token\"], ...]"),
        ("Overseerr users", "Overseerr Users", "json", ""),
        ("Overseerr API Key", "Overseerr API Key", "password", ""),
        ("Overseerr Base URL", "Overseerr URL", "text",
         "http://<overseerr-ip>:5055"),
    ]),
    ("Library", [
        ("Library collection service", "Collection Service", "list",
         "e.g. [\"Plex Library\"]"),
        ("Library update services", "Update Services", "list",
         "e.g. [\"Plex Libraries\"]"),
        ("Library ignore services", "Ignore Services", "list",
         "e.g. [\"Plex Discover Watch Status\"]"),
        ("Plex library refresh", "Plex Refresh Sections", "list", "Section IDs"),
        ("Plex partial scan", "Plex Partial Scan", "bool", ""),
        ("Plex refresh delay", "Plex Refresh Delay (s)", "text", ""),
    ]),
    ("Scrapers", [
        ("Sources", "Scraper Sources", "list",
         "e.g. [\"Torrentio\", \"Jackett\", \"Prowlarr\"]"),
        ("Jackett API Key", "Jackett API Key", "password", ""),
        ("Jackett base URL", "Jackett URL", "text", ""),
        ("Prowlarr API Key", "Prowlarr API Key", "password", ""),
        ("Prowlarr base URL", "Prowlarr URL", "text", ""),
    ]),
    ("Versions (Quality Rules)", [
        ("Versions", "Versions", "json",
         "plex_debrid quality-filter rule sets. Edit as JSON; see the "
         "plex_debrid wiki for the rule tuple format."),
    ]),
    ("General", [
        ("Show Menu on Startup", "Show Menu on Startup", "bool",
         "Keep false for headless. The Web UI replaces the menu."),
        ("Debug printing", "Debug Logging", "bool", "Verbose engine logs."),
        ("Log to file", "Log to File", "bool", "Write engine logs to pd.log."),
    ]),
]

# Flat set of all json keys we manage, for fast lookup.
_MANAGED_KEYS = {k for _, group in SETTINGS_SCHEMA for k, _, _, _ in group}


class SettingsStore:
    """Thread-safe load/save of plex_debrid's settings.json."""

    def __init__(self, config_dir):
        self.config_dir = config_dir
        self.path = os.path.join(config_dir, "settings.json")
        self._lock = threading.Lock()

    def exists(self):
        return os.path.isfile(self.path)

    def load_raw(self):
        """Return the raw flat settings dict, or {} if missing/corrupt."""
        with self._lock:
            if not os.path.isfile(self.path):
                return {}
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return {}

    def save_raw(self, data):
        """Atomically write the full flat settings dict."""
        os.makedirs(self.config_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4)
            os.replace(tmp, self.path)

    def load_grouped(self):
        """Return settings grouped per SETTINGS_SCHEMA for the UI."""
        raw = self.load_raw()
        groups = []
        for group_name, fields in SETTINGS_SCHEMA:
            items = []
            for json_key, label, kind, help_text in fields:
                items.append({
                    "key": json_key,
                    "label": label,
                    "kind": kind,
                    "help": help_text,
                    "value": _coerce_for_ui(raw.get(json_key), kind),
                })
            groups.append({"name": group_name, "fields": items})
        # Also surface any unmanaged keys so nothing is silently dropped.
        extras = {k: v for k, v in raw.items() if k not in _MANAGED_KEYS}
        return {"groups": groups, "extras": extras, "version": raw.get("version")}

    def apply_edits(self, edits):
        """Merge a {key: value} dict of UI edits onto the stored settings.

        Booleans arrive as actual bools from JSON and are stringified to
        "true"/"false" for plex_debrid. Lists/dicts are stored as-is.
        """
        raw = self.load_raw()
        for key, value in edits.items():
            if isinstance(value, bool):
                raw[key] = "true" if value else "false"
            else:
                raw[key] = value
        self.save_raw(raw)
        return raw


def _coerce_for_ui(value, kind):
    """Convert plex_debrid's stored form into a UI-friendly value."""
    if kind == "bool":
        # plex_debrid stores "true"/"false" strings.
        return str(value).lower() == "true"
    if kind in ("list", "json"):
        # Keep structured types intact for the UI to render/edit.
        return value if value is not None else ([] if kind == "list" else {})
    return value if value is not None else ""
