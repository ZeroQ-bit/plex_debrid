"""
Focused tests for the TorBox debrid provider.

plex_debrid uses heavy module globals (from base import *, ui_print, ui_settings)
and a deliberate circular import that only resolves via main.py's import order.
Rather than fight that, these tests load torbox.py as an isolated module and
inject the globals it needs, then mock the `requests.Session` so no real HTTP
calls are made.

Run with:  python3 tests/test_torbox.py   (no pytest dependency required)
       or:  pytest tests/test_torbox.py
"""
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# --- Path setup -----------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PD_ROOT = os.path.join(HERE, "..", "plex_debrid")
sys.path.insert(0, PD_ROOT)

# --- Build the minimal global namespace torbox.py expects -----------------
# torbox.py does: `from base import *`, `from ui.ui_print import *`,
# `import releases`, and references `requests`, `regex`, `time`, `json`,
# `SimpleNamespace`, `sys`. We synthesise each.
import requests
import regex
import time

# `base` module: provides json, requests, regex, time, SimpleNamespace, sys, copy, etc.
base = types.ModuleType("base")
base.requests = requests
base.regex = regex
base.time = time
base.json = json
base.sys = sys
base.SimpleNamespace = SimpleNamespace
import copy
base.copy = copy
sys.modules["base"] = base

# `ui.ui_print` + `ui` package: provides ui_print(...) and ui_settings.debug.
# NOTE: plex_debrid's real ui.ui_print does `from ui import ui_settings`, so the
# `from ui.ui_print import *` in torbox.py binds BOTH ui_print and ui_settings.
ui_pkg = types.ModuleType("ui")
ui_print_mod = types.ModuleType("ui.ui_print")
def _ui_print(*args, **kwargs):
    # capture for debugging if needed; no-op by default
    pass
ui_print_mod.ui_print = _ui_print
ui_pkg.ui_print = _ui_print
sys.modules["ui"] = ui_pkg
sys.modules["ui.ui_print"] = ui_print_mod

# `ui.ui_settings`: torbox references `ui_settings.debug`.
ui_settings_mod = types.ModuleType("ui.ui_settings")
ui_settings_mod.debug = False
sys.modules["ui.ui_settings"] = ui_settings_mod
ui_pkg.ui_settings = ui_settings_mod
# The real ui.ui_print re-exports ui_settings, so `from ui.ui_print import *`
# binds ui_settings in torbox.py. Mirror that.
ui_print_mod.ui_settings = ui_settings_mod

# `releases` package: torbox references `releases.release` and `releases.sort`.
releases_pkg = types.ModuleType("releases")
class _Release:
    """Minimal stand-in matching plex_debrid's releases.release attributes."""
    def __init__(self, title="Test.Release.S01E01.1080p.mkv", hash="", magnet="",
                 source="test", files=None, size=0):
        self.source = source
        self.type = "series"
        self.title = title
        self.files = files if files is not None else []
        self.size = size
        self.download = [magnet] if magnet else []
        self.hash = hash
        self.cached = []
        self.checked = False
        self.wanted = 0
        self.unwanted = 0
        self.seeders = 0
        self.resolution = "1080"
    def __eq__(self, other):
        return self.title == getattr(other, "title", None)
releases_pkg.release = _Release
class _Sort:
    unwanted = []
releases_pkg.sort = _Sort
sys.modules["releases"] = releases_pkg

# Now import torbox as an isolated module. We exec it so `from base import *`
# etc. resolve against our synthesised namespace.
torbox = types.ModuleType("torbox")
torbox.__dict__.update({
    "requests": requests, "regex": regex, "time": time, "json": json,
    "sys": sys, "SimpleNamespace": SimpleNamespace, "copy": copy,
})
# Provide the names that `from base import *` / `from ui.ui_print import *`
# would bind:
torbox.ui_print = _ui_print
torbox.ui_settings = ui_settings_mod
import importlib.util
spec = importlib.util.spec_from_file_location(
    "torbox", os.path.join(PD_ROOT, "debrid", "services", "torbox.py"))
mod = importlib.util.module_from_spec(spec)
# Make the module-level names visible during exec.
mod.__dict__.update(torbox.__dict__)
sys.modules["torbox"] = mod
spec.loader.exec_module(mod)


# --- Helpers to build a fake element + fake HTTP --------------------------
def _make_element(release_list):
    """Build a fake 'element' with .Releases, .files(), .deviation()."""
    el = SimpleNamespace()
    el.Releases = list(release_list)
    el.files_calls = []
    def _files():
        el.files_calls.append(True)
        return [r.title for r in release_list]
    el.files = _files
    el.deviation = lambda: ".*"
    return el

VALID_HASH = "a" * 40
MAGNET = "magnet:?xt=urn:btih:" + VALID_HASH + "&dn=Test"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
    def json(self):
        return self._payload


def _set_api_key(key="test-key-1234"):
    mod.api_key = key
    mod.session = MagicMock()


# === Tests ================================================================

def test_module_interface_attributes():
    """Provider must expose the required interface attributes."""
    assert mod.name == "TorBox", f"name is {mod.name!r}"
    assert mod.short == "TB", f"short is {mod.short!r}"
    assert hasattr(mod, "api_key")
    assert hasattr(mod, "session")
    assert callable(mod.setup)
    assert callable(mod.check)
    assert callable(mod.download)
    print("PASS test_module_interface_attributes")


def test_check_marks_cached_releases():
    """check() must append 'TB' to release.cached for hashes TorBox reports cached."""
    _set_api_key()
    rel = _Release(hash=VALID_HASH, magnet=MAGNET)
    el = _make_element([rel])
    # TorBox checkcached (format=object) keys data by the UPPERCASE hash.
    payload = {
        "success": True,
        "data": {
            VALID_HASH.upper(): {
                "hash": VALID_HASH.upper(),
                "name": "Test.Release",
                "size": 1000,
                "files": [{"id": 1, "name": "Test.Release.S01E01.mkv", "size": 1000}],
            }
        },
    }
    mod.session.get.return_value = FakeResponse(payload)
    mod.check(el, force=False)
    assert "TB" in rel.cached, f"expected 'TB' in cached, got {rel.cached}"
    print("PASS test_check_marks_cached_releases")


def test_check_skips_short_hashes():
    """Releases without a 40-char hash must be removed, never marked."""
    _set_api_key()
    rel_bad = _Release(title="nohash", hash="abc", magnet="magnet:?xt=urn:btih:abc")
    el = _make_element([rel_bad])
    payload = {"success": True, "data": {}}
    mod.session.get.return_value = FakeResponse(payload)
    mod.check(el, force=False)
    assert rel_bad not in el.Releases, "short-hash release should have been removed"
    assert "TB" not in rel_bad.cached
    print("PASS test_check_skips_short_hashes")


def test_check_empty_releases_no_http():
    """No hashes => no API call, no crash."""
    _set_api_key()
    el = _make_element([])
    mod.session.get.reset_mock()
    mod.check(el, force=False)
    assert not mod.session.get.called, "should not call API with no hashes"
    print("PASS test_empty_releases_no_http")


def test_download_cached_resolves_links():
    """download() for a cached release must populate release.download with URLs."""
    _set_api_key()
    rel = _Release(hash=VALID_HASH, magnet=MAGNET)
    el = _make_element([rel])

    # Sequence of GET/POST responses torbox.download issues:
    # 1) checkcached GET -> data keyed by hash with files
    # 2) createtorrent POST -> {data: {torrent_id: 77}}
    # 3) mylist GET (via _wait_until_ready) -> [{id:77, download_state:'cached'}]
    # 4) requestdl GET -> {data: 'https://dl.torbox.app/...'}
    cached_payload = {
        "success": True,
        "data": {
            VALID_HASH.upper(): {
                "hash": VALID_HASH.upper(), "name": "Test", "size": 1000,
                "files": [{"id": 1, "name": "Test.Release.S01E01.mkv", "size": 1000}],
            }
        },
    }
    create_payload = {"success": True, "data": {"torrent_id": 77, "hash": VALID_HASH.upper()}}
    mylist_payload = {"success": True, "data": [{"id": 77, "download_state": "cached"}]}
    dl_payload = {"success": True, "data": "https://dl.torbox.app/abc123/Test.Release.S01E01.mkv"}

    # session.get is called for checkcached, mylist, requestdl; session.post for createtorrent.
    mod.session.get.side_effect = [
        FakeResponse(cached_payload),
        FakeResponse(mylist_payload),
        FakeResponse(dl_payload),
    ]
    mod.session.post.return_value = FakeResponse(create_payload)

    result = mod.download(el, stream=True, query=".*", force=True)
    # Diagnostics on failure so the mock sequence can be corrected.
    if result is not True:
        gets = [str(c) for c in mod.session.get.call_args_list]
        posts = [str(c) for c in mod.session.post.call_args_list]
        print(f"\n  DEBUG gets={gets}\n  DEBUG posts={posts}")
    assert result is True, "download should return True for a cached release"
    assert len(rel.download) > 0, "release.download should be populated with URLs"
    assert rel.download[0].startswith("https://dl.torbox.app/"), \
        f"unexpected download url: {rel.download[0]}"
    print("PASS test_download_cached_resolves_links")


def test_download_uncached_just_submits():
    """download() with stream=False should just POST the magnet and return True."""
    _set_api_key()
    rel = _Release(hash=VALID_HASH, magnet=MAGNET)
    el = _make_element([rel])
    create_payload = {"success": True, "data": {"torrent_id": 99}}
    mod.session.post.return_value = FakeResponse(create_payload)
    mod.session.get.reset_mock()
    result = mod.download(el, stream=False, query=".*", force=True)
    assert result is True
    assert mod.session.post.called, "should POST createtorrent"
    print("PASS test_download_uncached_just_submits")


def test_auth_params_in_get_url():
    """GET URLs must include the api_key query param."""
    _set_api_key("my-secret-key")
    rel = _Release(hash=VALID_HASH, magnet=MAGNET)
    el = _make_element([rel])
    payload = {"success": True, "data": {VALID_HASH.upper(): {"files": []}}}
    mod.session.get.return_value = FakeResponse(payload)
    mod.check(el, force=True)
    called_url = mod.session.get.call_args[0][0]
    assert "api_key=my-secret-key" in called_url, \
        f"api_key missing from GET url: {called_url}"
    assert "checkcached" in called_url
    print("PASS test_auth_params_in_get_url")


def test_download_not_cached_returns_false_or_continues():
    """If checkcached reports no files for the hash, the cached path should not crash."""
    _set_api_key()
    rel = _Release(hash=VALID_HASH, magnet=MAGNET)
    el = _make_element([rel])
    cached_payload = {"success": True, "data": {VALID_HASH.upper(): {"files": None}}}
    mod.session.get.return_value = FakeResponse(cached_payload)
    mod.session.post.reset_mock()
    # No createtorrent should be needed because we bail before creating.
    result = mod.download(el, stream=True, query=".*", force=True)
    assert result is False, f"expected False when not cached, got {result}"
    print("PASS test_download_not_cached_returns_false_or_continues")


# --- Runner ---------------------------------------------------------------
def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
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
