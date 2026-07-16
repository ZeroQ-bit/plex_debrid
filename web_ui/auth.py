"""auth.py — OAuth / token-validation helpers for the plex_debrid Web UI.

Stdlib-only (urllib). No outbound calls happen at import time; every function
makes exactly the HTTP it needs when called. Designed so tests can monkeypatch
`_http_request` to feed in canned responses.

Flows:
  - Plex app-pin (PIN auth v2): start -> user authenticates in the Plex app ->
    poll until an authToken appears.
  - Trakt device OAuth: start -> user enters code at trakt.tv/device -> poll
    until an access_token is issued.
  - Debrid key validation: hit each provider's known "is this key valid" endpoint.
  - Overseerr user discovery: list users for the multiselect.
"""
import json
import os
import secrets
import urllib.error
import urllib.request

# plex_debrid's official Trakt OAuth credentials (from content/services/trakt.py).
# Safe to ship — these are the app's public client id/secret, not user secrets.
TRAKT_CLIENT_ID = "0183a05ad97098d87287fe46da4ae286f434f32e8e951caad4cc147c947d79a3"
TRAKT_CLIENT_SECRET = "87109ed53fe1b4d6b0239e671f36cd2f17378384fa1ae09888a32643f83b7e6c"

USER_AGENT = "plex-debrid-umbrel/1.0"


# --------------------------------------------------------------------------
# Low-level HTTP
# --------------------------------------------------------------------------
def _http_request(url, method="GET", headers=None, data=None, timeout=15):
    """Perform an HTTP request and return (status, body_str_or_None).

    body is the decoded text on success or the error body on failure.
    Raises urllib.error.URLError on network failure (caller catches).
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, raw


def _json_or_none(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def client_identifier(config_dir):
    """Return a stable per-install X-Plex-Client-Identifier UUID.

    Plex expects a persistent identifier so it can remember the app; generating
    a new one each time would pile up "devices" on the user's Plex account.
    """
    path = os.path.join(config_dir, "plex_client_id")
    if os.path.isfile(path):
        try:
            with open(path) as fh:
                cid = fh.read().strip()
            if cid:
                return cid
        except OSError:
            pass
    cid = secrets.token_hex(16)
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(cid)
    except OSError:
        pass
    return cid


# --------------------------------------------------------------------------
# Plex app-pin (PIN auth v2)
# --------------------------------------------------------------------------
# Plex requires a FULL client-identification header set to issue a token that
# can access Discover/Watchlist. A token created with only Product+Client-ID
# authenticates (not 401) but lacks the user scope, so the watchlist endpoint
# returns "Section 'watchlist' not found!" (404). Send the complete set.
PLEX_PRODUCT = "plex_debrid (ZeroQ Umbrel)"
PLEX_PIN_URL = "https://plex.tv/api/v2/pins"


def _plex_headers(cid):
    """Full Plex client-identification headers (matches the official clients)."""
    return {
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": "1.0.0",
        "X-Plex-Client-Identifier": cid,
        "X-Plex-Platform": "Linux",
        "X-Plex-Platform-Version": "1.0",
        "X-Plex-Device": "Linux",
        "X-Plex-Device-Name": PLEX_PRODUCT,
        "X-Plex-Device-Screen-Resolution": "1920x1080",
        "X-Plex-Language": "en",
        "X-Plex-Provides": "controller",
        "Accept": "application/json",
    }


def plex_start_pin(config_dir):
    """Start a Plex PIN auth flow. Returns {id, code, url} or {error}."""
    cid = client_identifier(config_dir)
    headers = _plex_headers(cid)
    headers["Strong"] = "true"
    status, body = _http_request(PLEX_PIN_URL, method="POST", headers=headers)
    if status not in (200, 201):
        return {"error": f"plex pin start failed (HTTP {status})"}
    data = _json_or_none(body)
    if not data or "id" not in data:
        return {"error": "plex pin start: unexpected response"}
    pin_id = data["id"]
    code = data.get("code", "")
    # The user authenticates by opening this link with the code embedded.
    url = "https://app.plex.tv/desktop/#!?clientIdentifier={}&code={}".format(cid, code)
    return {"id": pin_id, "code": code, "url": url, "client_identifier": cid}


def plex_poll_pin(pin_id, config_dir):
    """Poll a Plex PIN. Returns {done, token?, valid?}.

    done=True once Plex has authenticated; token is the Plex auth token. We also
    validate the token against the watchlist endpoint so the UI can show validity.
    """
    cid = client_identifier(config_dir)
    headers = _plex_headers(cid)
    status, body = _http_request("{}/{}".format(PLEX_PIN_URL, pin_id), headers=headers)
    if status != 200:
        return {"done": False, "error": f"plex poll failed (HTTP {status})"}
    data = _json_or_none(body)
    if not data:
        return {"done": False, "error": "plex poll: unexpected response"}
    token = data.get("authToken")
    if not token:
        return {"done": False}  # not authenticated yet
    valid = plex_validate_token(token)
    return {"done": True, "token": token, "valid": valid}


def plex_validate_token(token):
    """Return True if the Plex token can read the watchlist."""
    if not token:
        return False
    url = ("https://metadata.provider.plex.tv/library/sections/watchlist/all"
           "?X-Plex-Token=" + token)
    status, _ = _http_request(url)
    return status == 200


def plex_test_token(token):
    """Validate a Plex token and report whether it can read the watchlist.

    PIN-issued tokens authenticate (account works) but lack Discover scope, so
    the watchlist endpoint returns 404. Legacy device tokens (from
    plex.tv/devices.xml) have full scope. This tells the UI which is which.
    """
    if not token:
        return {"valid": False, "error": "no token provided"}
    # 1. does the token identify an account?
    status, body = _http_request(
        "https://plex.tv/api/v2/user?X-Plex-Token=" + token,
        headers={"Accept": "application/json"})
    if status != 200:
        return {"valid": False, "error": f"token rejected (HTTP {status})"}
    username = None
    data = _json_or_none(body)
    if isinstance(data, dict):
        username = data.get("username") or data.get("title")
    # 2. can it read the watchlist? (the real test)
    wl_status, _ = _http_request(
        "https://metadata.provider.plex.tv/library/sections/watchlist/all?X-Plex-Token=" + token)
    if wl_status == 200:
        return {"valid": True, "username": username, "watchlist": True}
    if wl_status == 404:
        return {"valid": True, "username": username, "watchlist": False,
                "error": "token works for your account but Plex says it has no "
                         "Discover watchlist scope. Use a legacy device token from "
                         "plex.tv/devices.xml instead of the PIN flow."}
    return {"valid": True, "username": username, "watchlist": False,
            "error": f"watchlist check returned HTTP {wl_status}"}


def plex_library_sections(server_url, token):
    """List a Plex server's library sections. Returns [{key, title, type}, ...].

    Used to auto-populate the 'Plex library refresh' and 'Plex library check'
    multiselects so the user never has to look up section numbers manually.
    """
    if not token:
        return {"error": "no plex token (connect Plex first)"}
    base = (server_url or "").rstrip("/")
    if not base:
        return {"error": "no plex server url set"}
    url = base + "/library/sections/?X-Plex-Token=" + token
    try:
        status, body = _http_request(url)
    except urllib.error.URLError as e:
        return {"error": "could not reach plex server: " + str(e)}
    if status == 401:
        return {"error": "plex token rejected (401) — reconnect Plex"}
    if status != 200:
        return {"error": f"plex returned HTTP {status}"}
    # The response is XML: <MediaContainer><Directory key="1" title="Movies" type="movie"/>...</MediaContainer>
    sections = _parse_plex_sections(body)
    if not sections:
        return {"error": "no library sections found (set up at least one Plex library)"}
    return {"sections": sections}


def _parse_plex_sections(body_text):
    """Extract Directory entries from a /library/sections response.

    Plex returns either XML (<MediaContainer><Directory .../>) or JSON
    ({"MediaContainer":{"Directory":[...]}}) depending on the Accept header.
    Since _http_request sends Accept: application/json, we usually get JSON —
    but we handle both to be safe.
    """
    import xml.etree.ElementTree as ET
    if not body_text:
        return []
    # Try JSON first (what we asked for).
    try:
        data = json.loads(body_text)
        container = data.get("MediaContainer", data)
        dirs = container.get("Directory", [])
        if isinstance(dirs, dict):
            dirs = [dirs]
        sections = []
        for d in dirs:
            key = d.get("key")
            if key:
                sections.append({"key": str(key), "title": d.get("title", "(untitled)"),
                                 "type": d.get("type", "unknown")})
        if sections:
            return sections
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
        pass
    # Fall back to XML parsing.
    try:
        root = ET.fromstring(body_text)
    except ET.ParseError:
        return []
    sections = []
    for d in root.iter("Directory"):
        key = d.get("key")
        if not key:
            continue
        sections.append({
            "key": key,
            "title": d.get("title", "(untitled)"),
            "type": d.get("type", "unknown"),
        })
    return sections


# --------------------------------------------------------------------------
# Trakt device OAuth
# --------------------------------------------------------------------------
TRAKT_CODE_URL = "https://api.trakt.tv/oauth/device/code"
TRAKT_TOKEN_URL = "https://api.trakt.tv/oauth/device/token"
TRAKT_HEADERS = {"Content-Type": "application/json",
                 "trakt-api-key": TRAKT_CLIENT_ID, "trakt-api-version": "2"}


def trakt_start():
    """Start a Trakt device-code flow. Returns {user_code, url, device_code, expires_in, interval}."""
    status, body = _http_request(
        TRAKT_CODE_URL, method="POST",
        headers=TRAKT_HEADERS, data={"client_id": TRAKT_CLIENT_ID})
    if status != 200:
        return {"error": f"trakt device/code failed (HTTP {status})"}
    data = _json_or_none(body)
    if not data or "device_code" not in data:
        return {"error": "trakt device/code: unexpected response"}
    return {
        "user_code": data.get("user_code"),
        "url": data.get("verification_url", "https://trakt.tv/device"),
        "device_code": data.get("device_code"),
        "expires_in": data.get("expires_in", 600),
        "interval": data.get("interval", 5),
    }


def trakt_poll(device_code):
    """Poll the Trakt device flow. Returns {done, token?} or {done, error}.

    Trakt returns 400 ('pending') until the user authorizes; that's not done.
    """
    status, body = _http_request(
        TRAKT_TOKEN_URL, method="POST", headers=TRAKT_HEADERS,
        data={"code": device_code, "client_id": TRAKT_CLIENT_ID,
              "client_secret": TRAKT_CLIENT_SECRET})
    if status == 200:
        data = _json_or_none(body)
        token = (data or {}).get("access_token")
        if token:
            return {"done": True, "token": token}
        return {"done": True, "error": "no access_token in response"}
    if status == 400:
        return {"done": False}  # pending — user hasn't entered the code yet
    if status == 409:
        return {"done": True, "error": "already used"}
    if status == 410:
        return {"done": True, "error": "code expired — restart"}
    if status == 418:
        return {"done": True, "error": "denied"}
    return {"done": False, "error": f"trakt poll (HTTP {status})"}


# --------------------------------------------------------------------------
# Debrid key validation
# --------------------------------------------------------------------------
# Each provider's validation endpoint. Returns 200 when the key works.
# Tuple: (url_template, auth_header_template_or_None)
#   - url_template uses {} as the key placeholder
#   - auth_header_template (e.g. "Bearer {}") sent as Authorization; None = no header
_DEBRID_CHECK = {
    # TorBox requires the key in the Authorization: Bearer header — the
    # ?api_key= query param returns 401 even for valid keys.
    "TorBox": ("https://api.torbox.app/v1/api/user/me", "Bearer {}"),
    "Real Debrid": ("https://api.real-debrid.com/rest/1.0/torrents?limit=1&auth_token={}", None),
    "All Debrid": ("https://api.alldebrid.com/v4/user?agent=plex_debrid&apikey={}", None),
    "Premiumize": ("https://www.premiumize.me/api/account/info?apikey={}", None),
}


def test_debrid(provider, key):
    """Validate a debrid provider API key. Returns {valid, detail}."""
    if not key:
        return {"valid": False, "error": "no api key provided"}
    spec = _DEBRID_CHECK.get(provider)
    if spec is None:
        # DebridLink / Put.io use device OAuth; treat the key as opaque.
        return {"valid": None, "detail": "{} uses device auth; key stored as-is".format(provider)}
    url_tpl, auth_tpl = spec
    url = url_tpl.format(key)
    headers = {}
    if auth_tpl:
        headers["Authorization"] = auth_tpl.format(key)
    try:
        status, body = _http_request(url, headers=headers)
    except urllib.error.URLError as e:
        return {"valid": False, "error": str(e)}
    if status == 200:
        data = _json_or_none(body) or {}
        detail = {}
        # TorBox: surface plan/premium for a friendlier result.
        if provider == "TorBox":
            plan_map = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}
            u = (data.get("data") or {})
            detail = {"email": u.get("email"),
                      "plan": plan_map.get(u.get("plan"), str(u.get("plan"))),
                      "premium": u.get("premium", False)}
        return {"valid": True, **detail}
    if status == 401:
        return {"valid": False, "error": "api key rejected (401)"}
    return {"valid": False, "error": f"HTTP {status}"}


# --------------------------------------------------------------------------
# Overseerr user discovery
# --------------------------------------------------------------------------
def overseerr_users(base_url, api_key):
    """List Overseerr users for the multiselect. Returns [{id, name}, ...] or {error}.

    Overseerr's API is at <base>/api/v1/user with header X-Api-Key.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {"error": "no base url"}
    if not api_key:
        return {"error": "no api key"}
    url = base + "/api/v1/user"
    try:
        status, body = _http_request(url, headers={"X-Api-Key": api_key})
    except urllib.error.URLError as e:
        return {"error": str(e)}
    if status != 200:
        return {"error": f"overseerr returned HTTP {status}"}
    data = _json_or_none(body)
    if not isinstance(data, list):
        return {"error": "unexpected response shape"}
    users = [{"id": u.get("id"), "name": u.get("displayName") or u.get("username") or u.get("plexUsername")}
             for u in data]
    return {"users": users}
