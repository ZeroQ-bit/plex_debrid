"""Tests for the library symlinker.

The symlinker is a self-contained module (no plex_debrid globals), so these
tests load it directly and use temp dirs + mocks for the filesystem and the
TorBox API.

Run with:  python3 tests/test_symlinker.py
"""
import os
import sys
import tempfile
import shutil
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
PD_ROOT = os.path.join(HERE, "..", "plex_debrid")
sys.path.insert(0, PD_ROOT)

import library_symlinker as sym


def _movie(title="Beast", year=2026, eids=None, torrent_name=None):
    m = SimpleNamespace()
    m.type = "movie"
    m.title = title
    m.year = year
    m.EID = eids if eids is not None else ["tmdb://555"]
    rel = SimpleNamespace()
    rel.title = title + ".2026.2160p.WEB-DL"
    rel.torrent_name = torrent_name or (title + " (2026) [2160p]")
    m.Releases = [rel]
    return m


def _show_episode(title="Landman", year=2024, eids=None):
    ep = SimpleNamespace()
    ep.type = "episode"
    ep.grandparentTitle = title
    ep.grandparentYear = year
    ep.parentEID = None
    ep.grandparentEID = eids if eids is not None else ["tvdb://100001"]
    ep.EID = []
    rel = SimpleNamespace()
    rel.title = "Landman.S02E01.2160p.WEB-DL"
    rel.torrent_name = "Landman.S02E01.2160p.WEB-DL"
    ep.Releases = [rel]
    return ep


def _make_raw_mount(tmpdir, torrent_name, files):
    """Create tmpdir/.vortexo-source/<torrent_name>/ with the given files."""
    root = os.path.join(tmpdir, ".vortexo-source", torrent_name)
    os.makedirs(root)
    for name, size in files:
        path = os.path.join(root, name)
        with open(path, "wb") as f:
            f.truncate(size)


# === ID extraction ========================================================

def test_resolve_ids_movie_prefers_tmdb():
    m = _movie(eids=["imdb://tt123", "tmdb://555"])
    assert sym.resolve_ids(m) == ("tmdb", "555")


def test_resolve_ids_show_prefers_tvdb():
    ep = _show_episode(eids=["tmdb://999", "tvdb://100001"])
    assert sym.resolve_ids(ep) == ("tvdb", "100001")


def test_resolve_ids_legacy_plex_guid_suffix():
    m = _movie(eids=["tmdb://555?lang=en"])
    assert sym.resolve_ids(m) == ("tmdb", "555")


def test_resolve_ids_none_when_empty():
    m = _movie(eids=[])
    assert sym.resolve_ids(m) is None


# === Folder/file naming ===================================================

def test_library_folder_name_movie():
    m = _movie(title="Beast", year=2026, eids=["tmdb://555"])
    name = sym.library_folder_name(m, "tmdb", "555")
    assert name == "Beast (2026) {tmdb-555}"


def test_library_folder_name_strips_bad_chars():
    m = _movie(title="Beast: Revenge", year=2026, eids=["tmdb://555"])
    name = sym.library_folder_name(m, "tmdb", "555")
    assert ":" not in name
    assert name.endswith("{tmdb-555}")


def test_library_folder_name_episode_uses_grandparent():
    ep = _show_episode(title="Landman", year=2024)
    name = sym.library_folder_name(ep, "tvdb", "100001")
    assert "Landman (2024)" in name
    assert name.endswith("{tvdb-100001}")


def test_quality_tag_picks_resolution_and_codec():
    tag = sym._quality_tag("Movie.2026.2160p.WEB-DL.DDP5.1.HEVC",
                           "Movie.2026.2160p.WEB-DL.mkv")
    assert "2160p" in tag
    assert "HEVC" in tag


def test_symlink_release_titles_returns_folder_name():
    m = _movie(title="Beast", year=2026, eids=["tmdb://555"])
    titles = sym.symlink_release_titles(m)
    assert titles == ["Beast (2026) {tmdb-555}"]


def test_symlink_release_titles_empty_when_no_id():
    m = _movie(eids=[])
    assert sym.symlink_release_titles(m) == []


# === symlink_item end-to-end ==============================================

def test_symlink_item_creates_link():
    tmp = tempfile.mkdtemp()
    try:
        lib = os.path.join(tmp, "Movies")
        os.makedirs(lib)
        _make_raw_mount(tmp, "Beast (2026) [2160p]",
                        [("Beast.2026.2160p.WEB.mkv", 1000)])
        m = _movie(title="Beast", year=2026, eids=["tmdb://555"],
                   torrent_name="Beast (2026) [2160p]")
        logs = []
        result = sym.symlink_item(m, tmp, {"movie": lib, "tv": None},
                                  log_fn=logs.append)
        assert result is not None, f"logs: {logs}"
        assert "Beast (2026) {tmdb-555}" in result
        # The symlink file exists inside the folder.
        folder = os.path.join(lib, "Beast (2026) {tmdb-555}")
        entries = os.listdir(folder)
        assert len(entries) == 1
        link = os.path.join(folder, entries[0])
        assert os.path.islink(link)
        assert "Beast (2026) {tmdb-555}" in entries[0]
        assert entries[0].endswith(".mkv")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_item_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        lib = os.path.join(tmp, "Movies")
        os.makedirs(lib)
        _make_raw_mount(tmp, "Beast (2026)",
                        [("Beast.2026.2160p.mkv", 1000)])
        m = _movie(title="Beast", year=2026, eids=["tmdb://555"],
                   torrent_name="Beast (2026)")
        sym.symlink_item(m, tmp, {"movie": lib, "tv": None})
        # Second call must not error or create a second link.
        sym.symlink_item(m, tmp, {"movie": lib, "tv": None})
        folder = os.path.join(lib, "Beast (2026) {tmdb-555}")
        assert len(os.listdir(folder)) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_item_missing_mount_returns_none():
    tmp = tempfile.mkdtemp()
    try:
        lib = os.path.join(tmp, "Movies")
        os.makedirs(lib)
        m = _movie(title="Beast", year=2026, eids=["tmdb://555"],
                   torrent_name="Nonexistent")
        logs = []
        result = sym.symlink_item(m, tmp, {"movie": lib, "tv": None},
                                  log_fn=logs.append)
        assert result is None
        assert any("not found" in l or "Debrid Mount" in l for l in logs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_item_no_id_returns_none():
    tmp = tempfile.mkdtemp()
    try:
        m = _movie(eids=[])
        result = sym.symlink_item(m, tmp, {"movie": tmp, "tv": tmp})
        assert result is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_item_skips_sample_files():
    tmp = tempfile.mkdtemp()
    try:
        lib = os.path.join(tmp, "Movies")
        os.makedirs(lib)
        # The sample is smaller; the main file must win despite name order.
        _make_raw_mount(tmp, "Beast (2026)",
                        [("Beast.2026.sample.mkv", 50),
                         ("Beast.2026.2160p.mkv", 5000)])
        m = _movie(title="Beast", year=2026, eids=["tmdb://555"],
                   torrent_name="Beast (2026)")
        sym.symlink_item(m, tmp, {"movie": lib, "tv": None})
        folder = os.path.join(lib, "Beast (2026) {tmdb-555}")
        link = os.path.join(folder, os.listdir(folder)[0])
        assert "sample" not in os.path.realpath(link).lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_item_episode_uses_tv_library():
    tmp = tempfile.mkdtemp()
    try:
        movies = os.path.join(tmp, "Movies")
        tv = os.path.join(tmp, "TV")
        os.makedirs(movies)
        os.makedirs(tv)
        _make_raw_mount(tmp, "Landman.S02E01.2160p.WEB-DL",
                        [("Landman.S02E01.2160p.mkv", 1000)])
        ep = _show_episode(title="Landman", year=2024)
        result = sym.symlink_item(ep, tmp, {"movie": movies, "tv": tv})
        assert result is not None
        assert tv in result
        assert "Landman (2024) {tvdb-100001}" in result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# === sweep =================================================================

def test_sweep_links_missing_torrent_to_existing_folder():
    tmp = tempfile.mkdtemp()
    try:
        movies = os.path.join(tmp, "Movies")
        os.makedirs(movies)
        # Existing library folder (no symlink inside yet).
        os.makedirs(os.path.join(movies, "Beast (2026) {tmdb-555}"))
        _make_raw_mount(tmp, "Beast (2026) [2160p]",
                        [("Beast.2026.2160p.WEB.mkv", 1000)])
        fake_mylist = [{"name": "Beast (2026) [2160p]", "cached": True,
                        "download_finished": True}]
        logs = []
        with patch.object(sym, "_fetch_mylist", return_value=fake_mylist):
            count = sym.sweep("fake-key", tmp, {"movie": movies, "tv": None},
                              log_fn=logs.append)
        assert count == 1, f"expected 1 symlink, got {count}; logs: {logs}"
        folder = os.path.join(movies, "Beast (2026) {tmdb-555}")
        assert len(os.listdir(folder)) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sweep_skips_when_mount_absent():
    tmp = tempfile.mkdtemp()
    try:
        logs = []
        count = sym.sweep("fake-key", tmp, {"movie": tmp, "tv": tmp},
                          log_fn=logs.append)
        assert count == 0
        assert any("not present" in l for l in logs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sweep_idempotent():
    tmp = tempfile.mkdtemp()
    try:
        movies = os.path.join(tmp, "Movies")
        os.makedirs(os.path.join(movies, "Beast (2026) {tmdb-555}"))
        _make_raw_mount(tmp, "Beast (2026)",
                        [("Beast.2026.2160p.mkv", 1000)])
        fake_mylist = [{"name": "Beast (2026)", "cached": True,
                        "download_finished": True}]
        with patch.object(sym, "_fetch_mylist", return_value=fake_mylist):
            c1 = sym.sweep("k", tmp, {"movie": movies, "tv": None})
            c2 = sym.sweep("k", tmp, {"movie": movies, "tv": None})
        assert c1 == 1
        assert c2 == 0  # already linked, second sweep adds nothing
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_video_handles_eexist_race():
    """Two torrents resolving to the same symlink name must not error.

    Simulates the EEXIST race: pre-create the symlink, then call _symlink_video
    again — it must return the path (success) without raising or logging an
    error."""
    tmp = tempfile.mkdtemp()
    try:
        folder = os.path.join(tmp, "Movies", "Beast (2026) {tmdb-555}")
        os.makedirs(folder)
        target = os.path.join(tmp, "raw.mkv")
        open(target, "wb").truncate(10)
        link = os.path.join(folder, "Beast (2026) {tmdb-555} [2160p].mkv")
        os.symlink(target, link)
        logs = []
        # Second call must not error and must return the existing path.
        result = sym._symlink_video(tmp + "/Movies", "Beast (2026) {tmdb-555}",
                                    "Beast (2026) {tmdb-555} [2160p].mkv",
                                    target, logs.append)
        assert result == link
        # No error logged for the already-existing case.
        assert not any("could not create" in l for l in logs), logs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# === TV season/episode parsing ===========================================

def test_parse_season_episode_sxxexx():
    assert sym._parse_season_episode("Landman.S02E10.Title.2160p") == (2, 10)
    assert sym._parse_season_episode("show s1e5") == (1, 5)


def test_parse_season_episode_x_format():
    assert sym._parse_season_episode("Friends.2x10") == (2, 10)


def test_parse_season_episode_season_pack():
    assert sym._parse_season_episode("Landman.S02.COMPLETE") == (2, None)


def test_parse_season_episode_none():
    assert sym._parse_season_episode("Random.Movie.2026") == (None, None)


def test_season_folder_name_padded():
    assert sym._season_folder_name(2) == "Season 02"
    assert sym._season_folder_name(10) == "Season 10"
    assert sym._season_folder_name(None) is None


# === TV symlink structure (Season NN/ folders) ===========================

def test_symlink_item_tv_creates_season_subfolder():
    """TV episodes must be linked into Season NN/ subfolders so Plex can match
    them. The flat-in-show-root layout is invisible to Plex's scanner."""
    tmp = tempfile.mkdtemp()
    try:
        movies = os.path.join(tmp, "Movies")
        tv = os.path.join(tmp, "TV")
        os.makedirs(movies); os.makedirs(tv)
        _make_raw_mount(tmp, "Landman.S02.COMPLETE",
                        [("Landman.S02E01.Pilot.2160p.mkv", 1000),
                         ("Landman.S02E10.Finale.2160p.mkv", 1200),
                         ("sample.txt", 1)])
        ep = _show_episode(title="Landman", year=2024)
        ep.Releases[0].torrent_name = "Landman.S02.COMPLETE"
        ep.Releases[0].title = "Landman.S02.COMPLETE.2160p"
        result = sym.symlink_item(ep, tmp, {"movie": movies, "tv": tv})
        assert result is not None
        show = os.path.join(tv, "Landman (2024) {tvdb-100001}")
        # Season 02 folder must exist with both episodes.
        season = os.path.join(show, "Season 02")
        assert os.path.isdir(season), f"Season 02/ missing; got {os.listdir(show)}"
        episodes = os.listdir(season)
        assert len(episodes) == 2, f"expected 2 episodes, got {episodes}"
        # No stray files at the show root (sample.txt excluded).
        root_files = [f for f in os.listdir(show) if not f.startswith("Season")]
        assert root_files == [], f"show root must be empty of media: {root_files}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_symlink_item_tv_single_episode_still_gets_season_folder():
    """A single-episode torrent (S01E05) still goes into Season 01/."""
    tmp = tempfile.mkdtemp()
    try:
        tv = os.path.join(tmp, "TV"); os.makedirs(tv)
        _make_raw_mount(tmp, "Show.S01E05",
                        [("Show.S01E05.1080p.mkv", 1000)])
        ep = _show_episode(title="Show", year=2024)
        ep.Releases[0].torrent_name = "Show.S01E05"
        ep.Releases[0].title = "Show.S01E05.1080p"
        sym.symlink_item(ep, tmp, {"movie": None, "tv": tv})
        show = os.path.join(tv, "Show (2024) {tvdb-100001}")
        assert os.path.isdir(os.path.join(show, "Season 01"))
        assert len(os.listdir(os.path.join(show, "Season 01"))) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sweep_tv_uses_season_subfolders():
    """The periodic sweep must also fan TV episodes into Season NN/."""
    tmp = tempfile.mkdtemp()
    try:
        tv = os.path.join(tmp, "TV")
        os.makedirs(os.path.join(tv, "Landman (2024) {tvdb-397424}"))
        _make_raw_mount(tmp, "Landman.S02.COMPLETE",
                        [("Landman.S02E01.mkv", 1000),
                         ("Landman.S02E02.mkv", 1100)])
        fake_mylist = [{"name": "Landman.S02.COMPLETE", "cached": True,
                        "download_finished": True}]
        with patch.object(sym, "_fetch_mylist", return_value=fake_mylist):
            count = sym.sweep("k", tmp, {"movie": None, "tv": tv})
        assert count == 2, f"expected 2 episodes linked, got {count}"
        season = os.path.join(tv, "Landman (2024) {tvdb-397424}", "Season 02")
        assert os.path.isdir(season)
        assert len(os.listdir(season)) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
