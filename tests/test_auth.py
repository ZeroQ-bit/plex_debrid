"""Tests for web_ui/auth.py — OAuth and validation flows with mocked HTTP.

Every flow is driven through auth._http_request, so we monkeypatch that to
return canned (status, body) tuples keyed by URL substring. No real network.

Run: python3 tests/test_auth.py   (no pytest dependency)
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "web_ui"))

import auth  # noqa: E402


# --- Fake HTTP layer ------------------------------------------------------
def make_fake_http(routes):
    """routes: {url_substring: (status, body_str)}.

    Returns a function matching auth._http_request's signature. If the url
    contains a key, return that route; otherwise 404.
    """
    def fake(url, method="GET", headers=None, data=None, timeout=15):
        for key, (status, body) in routes.items():
            if key in url:
                return status, body
        return 404, '{"error":"no route for ' + url + '"}'
    return fake


def _setup(monkey_routes):
    """Monkeypatch auth._http_request and return nothing."""
    auth._http_request = make_fake_http(monkey_routes)


def _tmp_config():
    return tempfile.mkdtemp(prefix="pd_auth_test_")


# === Tests ================================================================

def test_plex_start_pin_returns_code_and_url():
    _setup({
        "api/v2/pins": (201, json.dumps({"id": 42, "code": "ABCD"})),
    })
    cfg = _tmp_config()
    try:
        r = auth.plex_start_pin(cfg)
        assert r["id"] == 42
        assert r["code"] == "ABCD"
        assert "app.plex.tv" in r["url"]
        assert "code=ABCD" in r["url"]
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
    print("PASS test_plex_start_pin_returns_code_and_url")


def test_plex_poll_pending_when_no_token():
    _setup({"api/v2/pins/42": (200, json.dumps({"id": 42, "code": "X"}))})
    r = auth.plex_poll_pin(42, _tmp_config())
    assert r["done"] is False, f"expected pending, got {r}"
    print("PASS test_plex_poll_pending_when_no_token")


def test_plex_poll_done_returns_token():
    # authToken present -> done. Then validate_token hits the watchlist url
    # which we also stub to 200.
    _setup({
        "api/v2/pins/42": (200, json.dumps({"id": 42, "authToken": "tok-123"})),
        "watchlist/all": (200, "[]"),
    })
    r = auth.plex_poll_pin(42, _tmp_config())
    assert r["done"] is True
    assert r["token"] == "tok-123"
    assert r["valid"] is True
    print("PASS test_plex_poll_done_returns_token")


def test_plex_validate_token_rejects_401():
    _setup({"watchlist/all": (401, "")})
    assert auth.plex_validate_token("bad") is False
    print("PASS test_plex_validate_token_rejects_401")


def test_plex_client_identifier_is_stable():
    cfg = _tmp_config()
    try:
        a = auth.client_identifier(cfg)
        b = auth.client_identifier(cfg)
        assert a == b and len(a) > 0, "identifier must be stable across calls"
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
    print("PASS test_plex_client_identifier_is_stable")


def test_trakt_start_returns_user_code():
    _setup({"oauth/device/code": (200, json.dumps({
        "device_code": "dev1", "user_code": "XYZ123",
        "verification_url": "https://trakt.tv/device", "expires_in": 600, "interval": 5}))})
    r = auth.trakt_start()
    assert r["user_code"] == "XYZ123"
    assert r["device_code"] == "dev1"
    assert r["url"] == "https://trakt.tv/device"
    print("PASS test_trakt_start_returns_user_code")


def test_trakt_poll_pending_on_400():
    _setup({"oauth/device/token": (400, '{"error":"pending"}')})
    r = auth.trakt_poll("dev1")
    assert r["done"] is False, f"400 should be pending, got {r}"
    print("PASS test_trakt_poll_pending_on_400")


def test_trakt_poll_done_returns_token():
    _setup({"oauth/device/token": (200, json.dumps({"access_token": "trak-tok"}))})
    r = auth.trakt_poll("dev1")
    assert r["done"] is True
    assert r["token"] == "trak-tok"
    print("PASS test_trakt_poll_done_returns_token")


def test_trakt_poll_expired_on_410():
    _setup({"oauth/device/token": (410, '{"error":"expired"}')})
    r = auth.trakt_poll("dev1")
    assert r["done"] is True
    assert "expired" in r.get("error", "")
    print("PASS test_trakt_poll_expired_on_410")


def test_test_debrid_torbox_valid_surfaces_plan():
    _setup({"user/me": (200, json.dumps({
        "success": True,
        "data": {"email": "me@example.com", "plan": 2, "premium": True}}))})
    r = auth.test_debrid("TorBox", "valid-key")
    assert r["valid"] is True
    assert r["plan"] == "Pro"
    assert r["premium"] is True
    assert r["email"] == "me@example.com"
    print("PASS test_test_debrid_torbox_valid_surfaces_plan")


def test_test_debrid_rejects_401():
    _setup({"torrents": (401, '{"error":"bad_token"}')})
    r = auth.test_debrid("Real Debrid", "wrong")
    assert r["valid"] is False
    assert "401" in r["error"]
    print("PASS test_test_debrid_rejects_401")


def test_test_debrid_unknown_provider_is_oauth_passthrough():
    r = auth.test_debrid("Put.io", "some-key")
    assert r["valid"] is None
    assert "device auth" in r["detail"]
    print("PASS test_test_debrid_unknown_provider_is_oauth_passthrough")


def test_plex_library_sections_parses_xml():
    _setup({"/library/sections": (200,
        '<?xml version="1.0"?><MediaContainer>'
        '<Directory key="1" title="Movies" type="movie"/>'
        '<Directory key="2" title="TV Shows" type="show"/>'
        '<Directory key="4" title="Anime" type="show"/>'
        '</MediaContainer>')})
    r = auth.plex_library_sections("http://plex:32400", "tok")
    assert r["sections"][0] == {"key": "1", "title": "Movies", "type": "movie"}
    assert r["sections"][2]["key"] == "4"
    print("PASS test_plex_library_sections_parses_xml")


def test_plex_library_sections_parses_json():
    """Plex returns JSON (not XML) when Accept: application/json is sent — which
    is what _http_request does. The parser must handle both."""
    _setup({"/library/sections": (200, json.dumps({
        "MediaContainer": {"size": 2, "Directory": [
            {"key": "4", "title": "Movies", "type": "movie"},
            {"key": "5", "title": "TV Shows", "type": "show"}]}}))})
    r = auth.plex_library_sections("http://plex:32400", "tok")
    assert r["sections"][0] == {"key": "4", "title": "Movies", "type": "movie"}
    assert r["sections"][1]["key"] == "5"
    print("PASS test_plex_library_sections_parses_json")


def test_plex_library_sections_missing_inputs():
    assert "no plex token" in auth.plex_library_sections("http://x", "")["error"]
    assert "no plex server url" in auth.plex_library_sections("", "tok")["error"]
    print("PASS test_plex_library_sections_missing_inputs")


def test_plex_library_sections_rejects_401():
    _setup({"/library/sections": (401, "<Error/>")})
    r = auth.plex_library_sections("http://plex:32400", "bad")
    assert "401" in r["error"]
    print("PASS test_plex_library_sections_rejects_401")


def test_overseerr_users_lists():
    _setup({"api/v1/user": (200, json.dumps([
        {"id": 1, "displayName": "Alice"},
        {"id": 2, "username": "Bob"}]))})
    r = auth.overseerr_users("http://overseerr:5055", "key")
    assert r["users"][0] == {"id": 1, "name": "Alice"}
    assert r["users"][1] == {"id": 2, "name": "Bob"}
    print("PASS test_overseerr_users_lists")


def test_overseerr_users_missing_inputs():
    assert "no base url" in auth.overseerr_users("", "k")["error"]
    assert "no api key" in auth.overseerr_users("http://x", "")["error"]
    print("PASS test_overseerr_users_missing_inputs")


# --- Runner ---------------------------------------------------------------
def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{'=' * 50}\n{passed}/{passed + failed} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
