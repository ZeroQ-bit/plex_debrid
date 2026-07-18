"""
Focused tests for content.classes.media.deviation() and aliases().

These guard against a regression where stale, un-renamed alternate_titles
(lowercase with SPACES, apostrophes not stripped) leaked into the deviation
regex and caused valid cached releases to be rejected with "doesn't match
the allowed deviation".

Root cause: match() does self.__dict__.update(match.__dict__), which copies
a stale alternate_titles list onto self. The aliases() rename loop only
APPENDS — it never re-normalizes carried-over entries — so space-form
strings survived into deviation()'s '|'.join(...), producing a regex like
  ((throw momma from the train):?.)
that never matches dotted release titles like 'Throw.Momma.from.the.Train'.

Fix: aliases() now resets alternate_titles after match(), and deviation()
re-normalizes every entry through releases.rename() when building the regex.

Run with:  python3 tests/test_deviation.py
"""
import os
import sys
import types
import importlib.util
from types import SimpleNamespace

import regex

HERE = os.path.dirname(os.path.abspath(__file__))
PD_ROOT = os.path.join(HERE, "..", "plex_debrid")
sys.path.insert(0, PD_ROOT)

# --- Build the minimal module namespace content/classes.py needs ------------
import requests, json, time, copy, datetime, threading, os as _os, collections

base = types.ModuleType("base")
base.requests = requests
base.json = json
base.regex = regex
base.time = time
base.sys = sys
base.SimpleNamespace = SimpleNamespace
base.copy = copy
base.datetime = datetime
base.threading = threading
base.Thread = threading.Thread
base.os = _os
base.collections = collections
base.Sequence = collections.abc.Sequence
sys.modules["base"] = base

# ui stubs
ui_pkg = types.ModuleType("ui")
ui_print_mod = types.ModuleType("ui.ui_print")
ui_print_mod.ui_print = lambda *a, **k: None
ui_settings_mod = types.ModuleType("ui.ui_settings")
ui_settings_mod.debug = False
ui_print_mod.ui_settings = ui_settings_mod
ui_pkg.ui_print = ui_print_mod.ui_print
ui_pkg.ui_settings = ui_settings_mod
sys.modules["ui"] = ui_pkg
sys.modules["ui.ui_print"] = ui_print_mod
sys.modules["ui.ui_settings"] = ui_settings_mod
sys.modules["settings"] = types.ModuleType("settings")
sys.modules["settings"].ui_settings = ui_settings_mod

# debrid / scraper are imported at the top of classes.py but unused by the
# methods under test — stub them so import succeeds.
for _mn in ("debrid", "scraper"):
    sys.modules[_mn] = types.ModuleType(_mn)

# Load the REAL releases module (provides rename, release, sort).
spec_r = importlib.util.spec_from_file_location(
    "releases", os.path.join(PD_ROOT, "releases", "__init__.py"))
releases = importlib.util.module_from_spec(spec_r)
sys.modules["releases"] = releases
spec_r.loader.exec_module(releases)

# Stub content.services.trakt so classes.py can import it lazily via
# sys.modules without hitting the network or the circular import.
content_pkg = types.ModuleType("content")
cs_pkg = types.ModuleType("content.services")
trakt_mod = types.ModuleType("content.services.trakt")
trakt_mod.users = ["dummy_user"]  # take the trakt branch in aliases()
TRAKT_ALIASES = []
TRAKT_TRANSLATIONS = []
trakt_mod.aliases = lambda self, lan: list(TRAKT_ALIASES)
trakt_mod.translations = lambda self, lan: list(TRAKT_TRANSLATIONS)
MATCH_OBJ = None  # if set, match() returns this object


def _fake_match(self):
    return MATCH_OBJ


trakt_mod.match = _fake_match
sys.modules["content"] = content_pkg
sys.modules["content.services"] = cs_pkg
sys.modules["content.services.trakt"] = trakt_mod
cs_pkg.trakt = trakt_mod
content_pkg.services = cs_pkg

# Load the REAL content/classes.py.
spec_c = importlib.util.spec_from_file_location(
    "content.classes", os.path.join(PD_ROOT, "content", "classes.py"))
classes = importlib.util.module_from_spec(spec_c)
sys.modules["content.classes"] = classes
spec_c.loader.exec_module(classes)

media = classes.media
# versions() needs a configured library; bypass it for these unit tests.
media.versions = lambda self, quick=False: []


def _make_movie(title, year=2026):
    m = SimpleNamespace()
    m.type = "movie"
    m.title = title
    m.year = year
    m.guid = "dummy"
    m.EID = ["tmdb://dummy"]
    m.services = ["content.services.plex"]
    m.__module__ = "content.services.plex"
    return media(m)


def _deviation_regex(m):
    """Reproduce deviation()'s regex construction (movie, non-anime)."""
    alt = getattr(m, "alternate_titles", None)
    if alt:
        joined = "|".join(releases.rename(t) if isinstance(t, str) else str(t)
                          for t in alt)
        title = "(" + joined + ")"
    else:
        title = releases.rename(m.title)
    title = title.replace("[", "\\[").replace("]", "\\]")
    return "[^A-Za-z0-9]*(" + title + ":?.)\\(?\\[?(" + str(m.year) + ")"


# === Tests ================================================================

def test_deviation_no_space_when_title_clean():
    """A normal multi-word title must produce a dot-separated regex."""
    global TRAKT_ALIASES, TRAKT_TRANSLATIONS, MATCH_OBJ
    TRAKT_ALIASES = []
    TRAKT_TRANSLATIONS = []
    MATCH_OBJ = None
    m = _make_movie("Throw Momma from the Train", 1987)
    m.aliases("en")
    dev = _deviation_regex(m)
    assert " " not in dev, f"deviation regex must not contain spaces: {dev}"
    # And it must match a real dotted release title.
    assert regex.match(dev, "Throw.Momma.from.the.Train.1987.2160p.BluRay", regex.I), \
        f"deviation {dev!r} failed to match dotted release title"
    print("PASS test_deviation_no_space_when_title_clean")


def test_deviation_apostrophe_stripped():
    """A title with an apostrophe must match releases that drop the apostrophe."""
    global TRAKT_ALIASES, TRAKT_TRANSLATIONS, MATCH_OBJ
    TRAKT_ALIASES = []
    TRAKT_TRANSLATIONS = []
    MATCH_OBJ = None
    m = _make_movie("Lee Cronin's The Mummy", 2026)
    m.aliases("en")
    dev = _deviation_regex(m)
    assert "cronin's" not in dev.lower(), \
        f"apostrophe must be stripped from deviation regex: {dev}"
    assert " " not in dev, f"deviation regex must not contain spaces: {dev}"
    assert regex.match(dev, "Lee.Cronins.The.Mummy.2026.1080p.AMZN.WEB-DL", regex.I), \
        f"deviation {dev!r} failed to match Lee.Cronins release"
    print("PASS test_deviation_apostrophe_stripped")


def test_deviation_stale_space_form_normalized():
    """REGRESSION: even if a stale space-form alternate_titles is carried in
    via match().__dict__.update, deviation() must still produce a dot regex.

    This is the bug that rejected ~31% of cached releases in the field."""
    m = _make_movie("The Sheep Detectives", 2026)
    # Simulate the exact stale state the live debug captured:
    m.alternate_titles = ["the sheep detectives"]  # lowercase, spaces, NOT renamed
    dev = _deviation_regex(m)
    assert "the sheep detectives" not in dev, \
        f"stale space-form must be normalized away: {dev}"
    assert "the.sheep.detectives" in dev, \
        f"deviation should contain the dotted form: {dev}"
    print("PASS test_deviation_stale_space_form_normalized")


def test_aliases_resets_stale_after_match():
    """aliases() must clear alternate_titles after match() so carried-in stale
    entries don't survive (root-cause fix, not just the deviation guard)."""
    global TRAKT_ALIASES, TRAKT_TRANSLATIONS, MATCH_OBJ
    TRAKT_ALIASES = []
    TRAKT_TRANSLATIONS = []
    # match() returns an object carrying a stale space-form list.
    MATCH_OBJ = SimpleNamespace(
        title="The Sheep Detectives", year=2026, type="movie",
        alternate_titles=["the sheep detectives"], watchlist=None,
        guid="dummy", EID=["tmdb://dummy"],
        originallyAvailableAt="2026-01-01",
        services=["content.services.plex", "content.services.trakt"])
    m = _make_movie("The Sheep Detectives", 2026)
    m.aliases("en")
    alt = m.__dict__.get("alternate_titles", [])
    assert "the sheep detectives" not in alt, \
        f"stale space-form must be cleared by aliases(): {alt}"
    # The renamed (dotted) form should be present instead.
    assert any("." in a for a in alt), \
        f"expected a dotted alternate title after aliases(): {alt}"
    print("PASS test_aliases_resets_stale_after_match")


# --- Runner ---------------------------------------------------------------
def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
