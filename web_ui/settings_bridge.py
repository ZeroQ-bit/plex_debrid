"""settings_bridge — typed schema mapping plex_debrid's flat settings.json to
a UI-friendly structure with control types.

Each field describes how the UI should render and edit it:
  control: text | password | toggle | select | multiselect | connect | list
  options: [..] for select/multiselect (when from a fixed set)
  show_if: {"<other_key>": "<value>"} — only render when the condition holds.
           Used so e.g. the TorBox key field only shows when TorBox is enabled.

plex_debrid stores booleans as the strings "true"/"false" and several values
as lists-of-lists (Plex users [[name, token], ...]). This module normalises
both directions.
"""
import json
import os
import threading

# Fixed option sets (what plex_debrid actually supports).
DEBRID_PROVIDERS = ["TorBox", "Real Debrid", "All Debrid", "Premiumize", "Debrid Link", "Put.io"]
CONTENT_SERVICES = ["Plex Watchlist", "Trakt Watchlist", "Overseerr"]
LIBRARY_COLLECTION = ["Plex Library", "Trakt Collection", "Local", "Jellyfin"]
LIBRARY_UPDATE = ["Plex Libraries", "Trakt Collections", "Jellyfin Libraries", "Local"]
LIBRARY_IGNORE = ["Plex Discover Watch Status", "Trakt Watch Status", "Local ignore list"]
SCRAPER_SOURCES = ["Torrentio", "Jackett", "Prowlarr", "Nyaa", "Orionoid", "Rarbg", "1337x"]
AUTO_REMOVE_OPTS = ["movie", "show", "both", "none"]

# (json_key, label, control, {options/show_if/help})
# Grouped top-to-bottom in the order the UI should present them.
SCHEMA = [
    ("Debrid Provider", [
        ("Debrid Services", "Providers", "multiselect", {"options": DEBRID_PROVIDERS,
            "help": "Pick one or more debrid services. plex_debrid sends torrents here."}),
        ("TorBox API Key", "TorBox API Key", "password", {"show_if": {"Debrid Services": "TorBox"},
            "help": "TorBox → Settings → API Key. Use the Test button to validate."}),
        ("Real Debrid API Key", "Real-Debrid API Key", "password", {"show_if": {"Debrid Services": "Real Debrid"},
            "help": "real-debrid.com → Account → Get my API token."}),
        ("All Debrid API Key", "AllDebrid API Key", "password", {"show_if": {"Debrid Services": "All Debrid"},
            "help": "alldebrid.com → Settings → API key."}),
        ("Premiumize API Key", "Premiumize API Key", "password", {"show_if": {"Debrid Services": "Premiumize"},
            "help": "premiumize.me → Account → API Key."}),
        ("Debrid Link API Key", "DebridLink API Key", "password", {"show_if": {"Debrid Services": "Debrid Link"},
            "help": "OAuth device flow — visit debrid-link.fr/device."}),
        ("Put.io API Key", "Put.io API Key", "password", {"show_if": {"Debrid Services": "Put.io"},
            "help": "OAuth device flow — visit put.io/link."}),
    ]),
    ("Plex", [
        ("Plex users", "Plex Account", "connect", {"flow": "plex",
            "help": "Click Connect and authenticate in your Plex app. No token hunting."}),
        ("Plex server address", "Plex Server URL", "text", {"placeholder": "http://192.168.1.43:32400",
            "help": "Used to trigger library scans after a download."}),
        ("Plex auto remove", "Auto-remove from Watchlist", "select", {"options": AUTO_REMOVE_OPTS,
            "help": "Remove from your Plex Watchlist after a successful download."}),
    ]),
    ("Trakt", [
        ("Trakt users", "Trakt Account", "connect", {"flow": "trakt",
            "help": "Click Connect, enter the code at trakt.tv/device."}),
        ("Trakt early movie releases", "Early movie releases", "toggle",
            {"help": "Check Trakt 'latest releases' lists for early movie grabs."}),
    ]),
    ("Overseerr", [
        ("Overseerr Base URL", "Overseerr URL", "text", {"placeholder": "http://192.168.1.43:5055",
            "help": "Your Overseerr / Jellyseerr base URL."}),
        ("Overseerr API Key", "Overseerr API Key", "password",
            {"help": "Overseerr → Settings → API → Copy. Use Discover to load users."}),
        ("Overseerr users", "Overseerr Users", "multiselect", {"dynamic_options": "overseerr",
            "help": "Which Overseerr users' requests to download."}),
    ]),
    ("Library", [
        ("Library collection service", "Collection Service", "multiselect", {"options": LIBRARY_COLLECTION,
            "help": "Where plex_debrid records which media it has collected."}),
        ("Library update services", "Library Scan Services", "multiselect", {"options": LIBRARY_UPDATE,
            "help": "Which libraries to scan after a download so media appears."}),
        ("Library ignore services", "Library Ignore Services", "multiselect", {"options": LIBRARY_IGNORE,
            "help": "Where plex_debrid checks for already-have media."}),
        ("Plex partial scan", "Plex Partial Scan", "toggle",
            {"help": "Scan only the affected library folder (faster)."}),
        ("Plex refresh delay", "Plex Refresh Delay (seconds)", "text", {"placeholder": "0",
            "help": "Wait this long before triggering a library scan."}),
    ]),
    ("Scrapers", [
        ("Sources", "Scraper Sources", "multiselect", {"options": SCRAPER_SOURCES,
            "help": "Where plex_debrid searches for torrents. Torrentio is the easiest."}),
        ("Torrentio Scraper Parameters", "Torrentio Manifest URL", "text",
            {"show_if": {"Sources": "Torrentio"},
             "help": "Configure at torrentio.strem.fun/configure (skip debrid), copy the manifest URL."}),
        ("Jackett Base URL", "Jackett URL", "text", {"show_if": {"Sources": "Jackett"},
            "placeholder": "http://192.168.1.43:9117"}),
        ("Jackett API Key", "Jackett API Key", "password", {"show_if": {"Sources": "Jackett"}}),
        ("Jackett resolver timeout", "Jackett Resolver Timeout (s)", "text", {"show_if": {"Sources": "Jackett"}, "placeholder": "1"}),
        ("Jackett indexer filter", "Jackett Indexer Filter", "text", {"show_if": {"Sources": "Jackett"},
            "help": "Comma-separated indexer names, or 'all'."}),
        ("Prowlarr Base URL", "Prowlarr URL", "text", {"show_if": {"Sources": "Prowlarr"},
            "placeholder": "http://192.168.1.43:9696"}),
        ("Prowlarr API Key", "Prowlarr API Key", "password", {"show_if": {"Sources": "Prowlarr"}}),
        ("Nyaa parameters", "Nyaa Parameters", "text", {"show_if": {"Sources": "Nyaa"},
            "help": "e.g. &c=1_0&s=seeders&o=desc (c: 1_0 anime, 1_4 raw, 1_2 EN-sub)."}),
        ("Nyaa sleep time", "Nyaa Sleep (s)", "text", {"show_if": {"Sources": "Nyaa"}, "placeholder": "5"}),
        ("Nyaa proxy", "Nyaa Proxy Domain", "text", {"show_if": {"Sources": "Nyaa"}, "placeholder": "nyaa.si"}),
        ("Orionoid API Key", "Orionoid Token", "password", {"show_if": {"Sources": "Orionoid"},
            "help": "OAuth: visit auth.orionoid.com."}),
    ]),
    ("Versions (Quality Rules)", [
        ("Versions", "Quality Rule Sets", "list",
            {"help": "plex_debrid's quality filter rule tuples. Edit as JSON; see the plex_debrid wiki for the rule format.",
             "code": True}),
    ]),
    ("General", [
        ("Debug printing", "Debug Logging", "toggle", {"help": "Verbose engine logs (helps troubleshoot)."}),
        ("Log to file", "Log to File", "toggle", {"help": "Write engine logs to pd.log."}),
        ("Show Menu on Startup", "Show CLI Menu on Startup", "toggle",
            {"help": "Keep OFF for headless. The Web UI replaces the menu."}),
    ]),
]

# Flat key set we manage.
_MANAGED_KEYS = {k for _, group in SCHEMA for k, _, _, _ in group}


class SettingsStore:
    """Thread-safe load/save of plex_debrid's settings.json."""

    def __init__(self, config_dir):
        self.config_dir = config_dir
        self.path = os.path.join(config_dir, "settings.json")
        self._lock = threading.Lock()

    def exists(self):
        return os.path.isfile(self.path)

    def load_raw(self):
        with self._lock:
            if not os.path.isfile(self.path):
                return {}
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return {}

    def save_raw(self, data):
        os.makedirs(self.config_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4)
            os.replace(tmp, self.path)

    def get_field(self, key):
        """Look up a field's schema entry by json_key."""
        for _, group in SCHEMA:
            for fkey, label, control, meta in group:
                if fkey == key:
                    return (fkey, label, control, meta)
        return None

    def load_grouped(self):
        """Return settings grouped per SCHEMA with values coerced for the UI."""
        raw = self.load_raw()
        groups = []
        for group_name, fields in SCHEMA:
            items = []
            for fkey, label, control, meta in fields:
                items.append({
                    "key": fkey, "label": label, "control": control,
                    "options": meta.get("options"),
                    "dynamic_options": meta.get("dynamic_options"),
                    "show_if": meta.get("show_if"),
                    "flow": meta.get("flow"),
                    "placeholder": meta.get("placeholder"),
                    "help": meta.get("help"),
                    "code": meta.get("code", False),
                    "value": _coerce_for_ui(raw.get(fkey), control),
                })
            groups.append({"name": group_name, "fields": items})
        extras = {k: v for k, v in raw.items() if k not in _MANAGED_KEYS}
        return {"groups": groups, "extras": extras, "version": raw.get("version")}

    def schema(self):
        """Return the static schema (labels/controls/options), no values."""
        groups = []
        for group_name, fields in SCHEMA:
            items = []
            for fkey, label, control, meta in fields:
                items.append({
                    "key": fkey, "label": label, "control": control,
                    "options": meta.get("options"),
                    "dynamic_options": meta.get("dynamic_options"),
                    "show_if": meta.get("show_if"),
                    "flow": meta.get("flow"),
                    "placeholder": meta.get("placeholder"),
                    "help": meta.get("help"),
                    "code": meta.get("code", False),
                })
            groups.append({"name": group_name, "fields": items})
        return {"groups": groups}

    def apply_edits(self, edits):
        """Merge a {key: value} dict of UI edits onto stored settings.

        - bools (from toggles) -> stored as "true"/"false" strings.
        - connect fields (Plex/Trakt users) arrive already in plex_debrid's
          [[name, token], ...] list form.
        - everything else stored as-is.
        """
        raw = self.load_raw()
        for key, value in edits.items():
            if isinstance(value, bool):
                raw[key] = "true" if value else "false"
            else:
                raw[key] = value
        self.save_raw(raw)
        return raw


def _coerce_for_ui(value, control):
    """Convert plex_debrid's stored form into a UI-friendly value."""
    if control == "toggle":
        return str(value).lower() == "true"
    if control == "connect":
        # Plex/Trakt users stored as [[name, token], ...] — surface a summary.
        if isinstance(value, list) and len(value) > 0:
            return [{"name": u[0], "connected": True} for u in value if isinstance(u, (list, tuple))]
        return []
    if control in ("multiselect", "list"):
        return value if isinstance(value, list) else (["true"] if (control == "list" and value) else [])
    return value if value is not None else ""
