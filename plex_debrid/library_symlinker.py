"""Library symlinker for plex_debrid.

After plex_debrid adds a torrent to TorBox, the media is exposed by the
separate "Debrid Mount" app as a raw rclone mount at .vortexo-source/. Plex,
however, reads from a tidy library tree whose folders are named with TMDB/TVDB
ids, e.g.:

    /downloads/vortexo/Movies/Beast (2026) {tmdb-12345}/
        Beast (2026) {tmdb-12345} [2160p WEB HEVC AAC].mkv
          -> /downloads/.vortexo-source/<torrent name>/<file>.mkv

This module creates those symlinks so newly-downloaded media appears in Plex.
It runs in two ways:

  - symlink_item(): called immediately after a successful download, with the
    media item's resolved EIDs and the winning release's torrent name.
  - sweep(): a periodic background job that reconciles the library against
    TorBox's mylist, retroactively symlinking anything missing.

Both are idempotent: an existing symlink is never recreated, and a missing
raw mount (Debrid Mount app stopped) is tolerated.

The module is self-contained: it only uses the stdlib + `requests`, reads its
configuration from the caller (no plex_debrid globals), and never raises out
of the public entry points — failures are logged and skipped.
"""
import os
import re
import time

import requests

# TorBox API (matches debrid/services/torbox.py).
TORBOX_API_BASE = "https://api.torbox.app/v1/api"

# Video MIME types we consider for symlinking.
_VIDEO_MIMETYPES = ("video/",)
# File extensions we accept when MIME is missing/unreliable.
_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts")
# Sample-ish tags we avoid when picking the "main" file.
_SKIP_NAMES = ("sample", ".exe", ".txt", ".nfo", ".jpg", ".png", ".sfv")
# Durable hand-off from the engine process to the web UI sweep. Plex ignores
# dotfiles; the marker remains until a scan is observed completing.
SCAN_PENDING_MARKER = ".plex-scan-pending"


def _log(log_fn, msg):
    if log_fn:
        try:
            log_fn(msg)
        except Exception:
            pass


def _mark_scan_pending(folder_path, log_fn=None):
    try:
        with open(os.path.join(folder_path, SCAN_PENDING_MARKER), "a",
                  encoding="utf-8"):
            pass
        return True
    except OSError as e:
        _log(log_fn, f"could not mark Plex scan pending for {folder_path}: {e}")
        return False


def clear_scan_pending(folder_path, log_fn=None):
    marker = os.path.join(folder_path, SCAN_PENDING_MARKER)
    try:
        if os.path.isfile(marker) and not os.path.islink(marker):
            os.unlink(marker)
        return True
    except OSError as e:
        _log(log_fn, f"could not clear Plex scan marker for {folder_path}: {e}")
        return False


def _pending_scan_paths(library_dirs):
    pending = []
    for kind, library_dir in library_dirs.items():
        if kind not in ("movie", "tv") or not library_dir:
            continue
        try:
            entries = os.listdir(library_dir)
        except OSError:
            continue
        for entry in entries:
            folder = os.path.join(library_dir, entry)
            marker = os.path.join(folder, SCAN_PENDING_MARKER)
            if os.path.isdir(folder) and os.path.isfile(marker):
                pending.append((kind, folder))
    return pending


# ---------------------------------------------------------------------------
# ID extraction
# ---------------------------------------------------------------------------

def _first_eid(eids, scheme):
    """Return the id portion of the first 'scheme://id' entry, or None.

    Handles legacy plex GUIDs like 'tmdb://12345?lang=en' by stripping any
    '?...' suffix.
    """
    if not eids:
        return None
    prefix = scheme + "://"
    for eid in eids:
        if not isinstance(eid, str):
            continue
        if eid.startswith(prefix):
            rest = eid[len(prefix):]
            # Strip query/fragment suffixes from legacy agents.
            rest = rest.split("?", 1)[0].split("#", 1)[0]
            rest = rest.strip()
            # TMDB and TVDB identifiers are numeric. Reject path-like or
            # otherwise malformed external IDs before they reach folder names.
            if re.fullmatch(r"\d+", rest):
                return rest
    return None


def resolve_ids(item):
    """Resolve (provider, id) for a media item from its EIDs.

    Returns ('tmdb', '<id>'), ('tvdb', '<id>'), or None.
    Movies prefer tmdb; shows/seasons/episodes prefer tvdb then tmdb.
    Falls back through parent/grandparent EIDs for episodes and seasons.
    """
    itype = getattr(item, "type", "movie")
    # Gather candidate EID lists in priority order.
    candidates = []
    if itype in ("season", "episode"):
        candidates.append(getattr(item, "grandparentEID", None) or
                          getattr(item, "parentEID", None))
    candidates.append(getattr(item, "parentEID", None))
    candidates.append(getattr(item, "EID", None))

    if itype in ("show", "season", "episode"):
        for eids in candidates:
            tvdb = _first_eid(eids, "tvdb")
            if tvdb:
                return ("tvdb", tvdb)
        for eids in candidates:
            tmdb = _first_eid(eids, "tmdb")
            if tmdb:
                return ("tmdb", tmdb)
    else:
        for eids in candidates:
            tmdb = _first_eid(eids, "tmdb")
            if tmdb:
                return ("tmdb", tmdb)
        for eids in candidates:
            tvdb = _first_eid(eids, "tvdb")
            if tvdb:
                return ("tvdb", tvdb)
    return None


# ---------------------------------------------------------------------------
# Naming (matches the existing vortexo/Movies & vortexo/TV format)
# ---------------------------------------------------------------------------

# Characters Windows/Plex dislike in folder/file names.
_BAD_NAME_CHARS = '<>:"/\\|?*'


def _sanitize_for_fs(s):
    """Make a string safe for a single path component, keeping it readable."""
    if s is None:
        return ""
    out = []
    for ch in str(s):
        if ch in _BAD_NAME_CHARS:
            continue
        out.append(ch)
    cleaned = "".join(out).strip().rstrip(".")
    return cleaned


def library_folder_name(item, provider, guid):
    """Build '{Title} ({Year}) {{provider-id}}' for the library folder."""
    title = getattr(item, "title", None) or getattr(item, "parentTitle", None) or "Unknown"
    # Seasons/episodes carry the show title on parentTitle/grandparentTitle.
    if getattr(item, "type", None) in ("season", "episode"):
        title = (getattr(item, "grandparentTitle", None)
                 or getattr(item, "parentTitle", None) or title)
    year = (getattr(item, "grandparentYear", None) or getattr(item, "parentYear", None)
            or getattr(item, "year", None))
    title = _sanitize_for_fs(title)
    name = title
    if year:
        name += f" ({year})"
    name += f" {{{provider}-{guid}}}"
    return name


def _quality_tag(release_title, filename):
    """Best-effort '[2160p BluRay HEVC AAC]'-style tag from the file name."""
    src = (filename or "") + " " + (release_title or "")
    src_l = src.lower()
    res = ""
    for r in ("2160p", "1080p", "720p", "480p"):
        if r in src_l:
            res = r
            break
    if "remux" in src_l:
        res = (res + " remux").strip()
    codec = ""
    if "hevc" in src_l or "x265" in src_l or "h265" in src_l:
        codec = "HEVC"
    elif "avc" in src_l or "x264" in src_l or "h264" in src_l:
        codec = "AVC"
    audio = ""
    if "atmos" in src_l:
        audio = "Atmos"
    elif "ddp" in src_l or "dd+" in src_l or "eac3" in src_l:
        audio = "AAC"
    elif "aac" in src_l:
        audio = "AAC"
    elif "ac3" in src_l:
        audio = "AC3"
    parts = [p for p in (res, codec, audio) if p]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Raw-mount file discovery
# ---------------------------------------------------------------------------

def _is_video_file(name, mimetype=None):
    if name:
        nl = name.lower()
        if any(s in nl for s in _SKIP_NAMES):
            return False
        if mimetype and mimetype.startswith(_VIDEO_MIMETYPES):
            return True
        if nl.endswith(_VIDEO_EXTS):
            return True
    return False


def _parse_season_episode(name):
    """Parse a (season, episode) pair from a release/file name.

    Recognizes S02E10, s2e5, 2x10, and Season NN patterns. Returns
    (season_int, episode_int), (season_int, None) for season-packs, or
    (None, None) if nothing matched.
    """
    if not name:
        return (None, None)
    nl = name.lower()
    # S02E10 / s2e5
    m = re.search(r"[sx](\d{1,2})\s?e(\d{1,3})", nl)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # 2x10
    m = re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", nl)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # Season 2/Episode 10 (common in nested season-pack paths).
    m = re.search(r"season\s*(\d{1,2}).*?(?:episode|ep)\s*(\d{1,3})", nl)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # A flat season pack may use episode01.mkv while the torrent title carries
    # only S02. Return the episode independently so the caller can combine it
    # with the release-level season.
    m = re.search(r"(?:^|[^a-z0-9])(?:episode|ep)[ ._-]*(\d{1,3})(?:[^0-9]|$)",
                  nl)
    if m:
        return (None, int(m.group(1)))
    # Season pack: "S02" or "Season 2"
    m = re.search(r"(?:^|[^0-9])s(\d{1,2})(?:[^0-9]|$)", nl)
    if m:
        return (int(m.group(1)), None)
    m = re.search(r"season\s(\d{1,2})", nl)
    if m:
        return (int(m.group(1)), None)
    return (None, None)


def _season_folder_name(season):
    """Return 'Season 02' (zero-padded) for Plex's TV scanner."""
    if season is None:
        return None
    return "Season {:02d}".format(int(season))


def _list_raw_folder(mount_dir, torrent_name):
    """List files in the raw mount folder for a torrent.

    torrent_name is TorBox's `name` field (the folder under .vortexo-source/).
    Returns a list of (filename, full_path) tuples, or [] if the folder is
    missing/unreadable.
    """
    root_path = os.path.realpath(mount_dir)
    source = os.path.realpath(os.path.join(root_path, torrent_name))
    try:
        if os.path.commonpath([root_path, source]) != root_path:
            return []
    except (OSError, ValueError):
        return []
    # Single-file torrents can be exposed directly at the mount root, with
    # TorBox's `name` itself ending in .mkv/.mp4 rather than naming a folder.
    if os.path.isfile(source):
        return [(os.path.basename(source), source)]
    if not os.path.isdir(source):
        return []
    out = []
    try:
        # Season packs often contain Show/Season NN/<episode> rather than
        # placing every file directly in the torrent root. Preserve the
        # relative name so season information in parent directories remains
        # available to the TV parser.
        for root, dirs, entries in os.walk(source):
            dirs.sort()
            for entry in sorted(entries):
                full = os.path.join(root, entry)
                resolved = os.path.realpath(full)
                try:
                    contained = os.path.commonpath(
                        [root_path, resolved]) == root_path
                except (OSError, ValueError):
                    contained = False
                if contained and os.path.isfile(full):
                    out.append((os.path.relpath(full, source), full))
    except OSError:
        return []
    return out


def _list_raw_folder_with_retry(mount_dir, torrent_name, attempts=1,
                                retry_delay=2.0, log_fn=None):
    """Wait briefly for a newly-added torrent to appear in the rclone mount.

    TorBox can report a cached torrent ready before the separate rclone mount's
    directory cache exposes it. `attempts` includes the initial lookup, so the
    default keeps direct unit callers non-blocking while the post-download
    integration can opt into a bounded wait.
    """
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 1
    try:
        retry_delay = max(0.0, float(retry_delay))
    except (TypeError, ValueError):
        retry_delay = 2.0
    for index in range(attempts):
        files = _list_raw_folder(mount_dir, torrent_name)
        if files:
            if index:
                _log(log_fn, f"raw mount became ready after {index + 1} checks")
            return files
        if index + 1 < attempts:
            time.sleep(retry_delay)
    return []


def _pick_video_file(files, release_title=""):
    """From a list of (name, full_path), pick the best video file.

    Prefers the largest video file (movies), since rclone-mount torrent folders
    sometimes include samples. Returns (name, full_path) or None.
    """
    video = [(n, p) for (n, p) in files if _is_video_file(n)]
    if not video:
        return None
    best = None
    best_size = -1
    for name, full in video:
        try:
            sz = os.path.getsize(full)
        except OSError:
            sz = 0
        if sz > best_size:
            best_size = sz
            best = (name, full)
    return best or video[0]


# ---------------------------------------------------------------------------
# TorBox mylist (used by the periodic sweep)
# ---------------------------------------------------------------------------

def _fetch_mylist(api_key, timeout=30):
    """Return TorBox mylist torrents as plain dicts, or [] on failure."""
    if not api_key:
        return []
    try:
        resp = requests.get(
            TORBOX_API_BASE + "/torrents/mylist",
            headers={"Authorization": "Bearer " + str(api_key)},
            timeout=timeout)
        if resp.status_code != 200:
            return []
        body = resp.json()
        data = body.get("data") or []
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Symlink creation
# ---------------------------------------------------------------------------

def _symlink_video(library_dir, folder_name, filename, target, log_fn):
    """Create library_dir/folder_name/filename -> target. Idempotent.

    A symlink already existing (either a real symlink or, in rare legacy cases,
    a regular file) is treated as success and not an error — multiple torrents
    can legitimately resolve to the same library filename (e.g. two rips of the
    same movie), and the first one wins.
    """
    folder = os.path.join(library_dir, folder_name)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        _log(log_fn, f"could not create library folder {folder}: {e}")
        return None
    link_path = os.path.join(folder, filename)
    # Idempotent: existing symlink/file is a no-op. (islink catches both valid
    # and broken symlinks; lexists is the most reliable single check.)
    if os.path.lexists(link_path):
        return link_path
    try:
        os.symlink(target, link_path)
        _log(log_fn, f"symlinked {filename[:60]} -> {target[:80]}")
        return link_path
    except OSError as e:
        # EEXIST can still race between the lexists check and the symlink call
        # (two torrents mapping to the same name within one sweep). Treat it as
        # success — the link is there, which is what we wanted.
        if getattr(e, "errno", None) == 17:  # EEXIST
            return link_path
        _log(log_fn, f"could not create symlink {link_path}: {e}")
        return None


def _link_tv_episodes(library_dir, folder_name, files, release_title, log_fn):
    """Link every video file of a TV torrent into Season NN/ subfolders.

    Plex's TV scanner requires Show/Season NN/SxxExx-name.ext structure, so a
    flat torrent folder (one file per episode) must be fanned out into
    per-season subfolders with SxxExx-aware names. Returns the number of new
    symlinks created and the deepest show folder path (for Plex refresh).
    """
    show_path = os.path.join(library_dir, folder_name)
    try:
        os.makedirs(show_path, exist_ok=True)
    except OSError as e:
        _log(log_fn, f"could not create show folder {show_path}: {e}")
        return (0, None)
    created = 0
    for file_name, file_path in files:
        if not _is_video_file(file_name):
            continue
        season, episode = _parse_season_episode(file_name)
        release_season, release_episode = _parse_season_episode(release_title)
        # Fall back independently: a nested path may provide the season while
        # only the torrent title provides the episode (or vice versa).
        if season is None:
            season = release_season
        if episode is None:
            episode = release_episode
        season_folder = _season_folder_name(season)
        if season_folder:
            sub = os.path.join(show_path, season_folder)
            try:
                os.makedirs(sub, exist_ok=True)
            except OSError:
                sub = show_path
        else:
            sub = show_path
        # Build a Plex-parseable filename: prefer the original release filename
        # (it usually already has SxxExx), falling back to folder/quality form.
        base = file_name
        # The raw-mount filename may include the torrent folder prefix; strip
        # everything except the basename if it looks like a path.
        if "/" in base or "\\" in base:
            base = os.path.basename(base)
        # A generic raw filename such as `video.mkv` is not parseable by the
        # Plex TV scanner even if its torrent title contains S02E01. Inject the
        # recovered marker while retaining the extension.
        _, base_episode = _parse_season_episode(base)
        if base_episode is None and season is not None and episode is not None:
            ext = os.path.splitext(base)[1] or ".mkv"
            base = (folder_name + " - S{:02d}E{:02d}".format(
                    int(season), int(episode)) + ext)
        link_path = os.path.join(sub, _sanitize_for_fs(base))
        if os.path.lexists(link_path):
            continue
        try:
            os.symlink(file_path, link_path)
            created += 1
        except OSError as e:
            if getattr(e, "errno", None) == 17:  # EEXIST
                pass
            else:
                _log(log_fn, f"could not create symlink {link_path}: {e}")
    if created:
        _log(log_fn, f"linked {created} TV episode(s) under {folder_name}")
    return (created, show_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def symlink_item(item, mount_dir, library_dirs, log_fn=None,
                 mount_attempts=1, retry_delay=2.0):
    """Create a library symlink for a freshly-downloaded media item.

    Args:
        item: the plex_debrid media object. Must carry type/EID/title/year and
              Releases[0].torrent_name (set by the TorBox provider).
        mount_dir: the raw mount root containing .vortexo-source (typically
                   /downloads); the torrent folder is at
                   <mount_dir>/.vortexo-source/<torrent_name>.
        library_dirs: {'movie': <dir>, 'tv': <dir>}. The symlink is created
                      under the one matching item.type.
        log_fn: optional callable(str) for logging.

    Returns the library folder path created (so the caller can refresh Plex at
    that path), or None if nothing was symlinked.
    """
    try:
        resolved = resolve_ids(item)
        if not resolved:
            _log(log_fn, "skipped symlink: no tmdb/tvdb id on item "
                 + repr(getattr(item, "title", None)))
            return None
        provider, guid = resolved

        itype = getattr(item, "type", "movie")
        is_tv = itype in ("show", "season", "episode")
        library_dir = library_dirs.get("tv" if is_tv else "movie")
        if not library_dir:
            _log(log_fn, f"skipped symlink: no library dir for type {itype!r}")
            return None

        # Locate the winning release's torrent folder in the raw mount.
        releases = getattr(item, "Releases", None) or []
        if not releases:
            _log(log_fn, "skipped symlink: item has no Releases")
            return None
        release = releases[0]
        torrent_name = (getattr(release, "torrent_name", None)
                        or getattr(release, "title", None))
        if not torrent_name:
            _log(log_fn, "skipped symlink: release has no torrent name")
            return None

        folder_name = library_folder_name(item, provider, guid)
        folder_path = os.path.join(library_dir, folder_name)
        # Reserve the canonical ID folder before consulting the eventually
        # consistent mount. If the bounded wait still misses, the periodic
        # sweep can now match the torrent and recover it later.
        try:
            os.makedirs(folder_path, exist_ok=True)
        except OSError as e:
            _log(log_fn, f"could not reserve library folder {folder_path}: {e}")
            return None

        raw_root = os.path.join(mount_dir, ".vortexo-source")
        files = _list_raw_folder_with_retry(
            raw_root, torrent_name, attempts=mount_attempts,
            retry_delay=retry_delay, log_fn=log_fn)
        if not files:
            _log(log_fn, f"pending symlink: raw mount source not found for "
                 f"{torrent_name[:60]!r}; reserved {folder_name!r} for sweep recovery")
            return None

        if is_tv:
            # TV: link EVERY episode file into Season NN/ subfolders with
            # SxxExx-aware names so Plex's TV scanner can parse them. A single
            # flat .mkv in the show root (the old behaviour) is invisible to
            # Plex's episode matcher.
            video_files = [(n, p) for (n, p) in files if _is_video_file(n)]
            if not video_files:
                _log(log_fn, f"skipped symlink: no video file in {torrent_name[:60]!r}")
                return None
            count, show_path = _link_tv_episodes(
                library_dir, folder_name, video_files,
                getattr(release, "title", ""), log_fn)
            if show_path is None:
                return None
            if count:
                season, _ = _parse_season_episode(getattr(release, "title", ""))
                sf = _season_folder_name(season)
                _log(log_fn, f"linked {itype} {getattr(item,'title','')!r} "
                     f"as {folder_name}"
                     + (f" / {sf}" if sf else ""))
            _mark_scan_pending(show_path, log_fn)
            return show_path

        # Movie: single best video file in a flat folder.
        picked = _pick_video_file(files, getattr(release, "title", ""))
        if not picked:
            _log(log_fn, f"skipped symlink: no video file in {torrent_name[:60]!r}")
            return None
        file_name, file_path = picked
        quality = _quality_tag(getattr(release, "title", ""), file_name)
        link_base = folder_name
        if quality:
            link_base += f" [{quality}]"
        # Keep the extension from the source file.
        ext = os.path.splitext(file_name)[1] or ".mkv"
        link_filename = _sanitize_for_fs(link_base) + ext

        created = _symlink_video(library_dir, folder_name, link_filename,
                                 file_path, log_fn)
        if created is None:
            return None
        _log(log_fn, f"linked {itype} {getattr(item,'title','')!r} "
             f"as {folder_name}")
        _mark_scan_pending(folder_path, log_fn)
        return os.path.join(library_dir, folder_name)
    except Exception as e:
        _log(log_fn, f"symlink_item failed: {e!r}")
        return None


def symlink_release_titles(item):
    """Return the {tmdb-ID}/{tvdb-ID} library folder name(s) for an item.

    Intended to feed plex_debrid's `downloaded_releases` list so the Plex
    partial-refresh (?path=) targets the symlink folder instead of the raw
    torrent-name folder. Returns [] if no id can be resolved (caller falls
    back to the existing release titles).
    """
    resolved = resolve_ids(item)
    if not resolved:
        return []
    provider, guid = resolved
    return [library_folder_name(item, provider, guid)]


def canonical_downloaded_release_paths(item):
    """Collect canonical ID-folder refresh paths from an item and children.

    Show downloads are delegated to Season/Episode objects. Their successful
    symlink paths must be propagated back to the parent Show before it asks
    Plex to scan; otherwise Plex falls back to the entire TV root. Only
    canonical `{tmdb-ID}`/`{tvdb-ID}` paths are returned, never raw torrent
    titles left by the debrid dispatcher.
    """
    found = []
    seen_paths = set()
    seen_objects = set()

    def visit(node):
        if node is None or id(node) in seen_objects:
            return
        seen_objects.add(id(node))
        # This explicit field is written only after the symlinker succeeds;
        # do not infer trust from raw torrent titles in downloaded_releases.
        for path in getattr(node, "symlink_library_paths", None) or []:
            if not isinstance(path, str):
                continue
            basename = os.path.basename(path.rstrip(os.sep))
            if not re.fullmatch(r".+\{(?:tmdb|tvdb)-[^{}]+\}", basename,
                                flags=re.IGNORECASE):
                continue
            if path not in seen_paths:
                seen_paths.add(path)
                found.append(path)
        for attr in ("Seasons", "Episodes"):
            for child in getattr(node, attr, None) or []:
                visit(child)

    visit(item)
    return found


def sweep(api_key, mount_dir, library_dirs, log_fn=None, max_items=None,
          changed_paths=None):
    """Periodic reconciliation: symlink any TorBox torrents missing from the
    library.

    Note: the sweep can only reliably symlink torrents whose target library
    folder already exists (i.e. Plex metadata already created the
    {tmdb-ID}/{tvdb-ID} folder), because without per-item EIDs we cannot name
    a new folder from scratch. Torrents that don't match an existing library
    folder are skipped with a debug log line — the immediate symlink_item()
    call handles brand-new downloads where the item's EIDs are known.

    Returns the count of new symlinks created.
    """
    created = 0
    try:
        if not os.path.isdir(os.path.join(mount_dir, ".vortexo-source")):
            _log(log_fn, "sweep skipped: raw mount not present "
                 "(Debrid Mount app stopped?)")
            return 0
        # Build an index of existing library folders by lowercased name for
        # quick matching against torrent names.
        existing = _index_library_folders(library_dirs)
        if not existing:
            _log(log_fn, "sweep skipped: no library folders found")
            return 0

        torrents = _fetch_mylist(api_key)
        _log(log_fn, f"sweep: {len(torrents)} torrents in TorBox, "
             f"{len(existing)} library folders")
        raw_root = os.path.join(mount_dir, ".vortexo-source")
        processed = 0
        for t in torrents:
            if max_items is not None and processed >= max_items:
                break
            processed += 1
            name = t.get("name")
            if not name:
                continue
            # Only cached/completed torrents expose files in the mount.
            if not (t.get("cached") or t.get("download_finished")):
                continue
            kind_hint = _infer_media_kind(name)
            match = _match_torrent_to_folder(name, existing, kind_hint)
            if not match:
                continue
            folder_name, library_dir = match
            folder_path = os.path.join(library_dir, folder_name)
            files = _list_raw_folder(raw_root, name)
            if not files:
                continue
            file_kind = _infer_media_kind(*[file_name for file_name, _ in files])
            if file_kind and file_kind != kind_hint:
                typed_match = _match_torrent_to_folder(
                    name, existing, file_kind)
                if not typed_match:
                    continue
                folder_name, library_dir = typed_match
                folder_path = os.path.join(library_dir, folder_name)
            # TV library: fan episodes out into Season NN/ subfolders. Movie
            # library: single best file, flat. (library_dir matches one of the
            # configured 'tv'/'movie' paths.)
            is_tv = library_dir == library_dirs.get("tv")
            if is_tv:
                video_files = [(n, p) for (n, p) in files if _is_video_file(n)]
                if not video_files:
                    continue
                # Reconcile every episode individually. _link_tv_episodes is
                # already idempotent; an "any target exists" precheck used to
                # skip the rest of a partially-linked season pack.
                count, _ = _link_tv_episodes(library_dir, folder_name,
                                             video_files, name, log_fn)
                created += count
                if count:
                    _mark_scan_pending(folder_path, log_fn)
                continue
            picked = _pick_video_file(files, name)
            if not picked:
                continue
            file_name, file_path = picked
            ext = os.path.splitext(file_name)[1] or ".mkv"
            quality = _quality_tag(name, file_name)
            link_base = folder_name
            if quality:
                link_base += f" [{quality}]"
            link_filename = _sanitize_for_fs(link_base) + ext
            # Skip if any symlink already exists in this folder pointing at
            # this file (idempotent across sweeps).
            if _folder_already_links(folder_path, file_path):
                continue
            created_link = _symlink_video(library_dir, folder_name,
                                          link_filename, file_path, log_fn)
            if created_link:
                created += 1
                _mark_scan_pending(folder_path, log_fn)
        if changed_paths is not None:
            changed_paths.extend(_pending_scan_paths(library_dirs))
        _log(log_fn, f"sweep complete: {created} new symlinks")
    except Exception as e:
        _log(log_fn, f"sweep failed: {e!r}")
    return created


# ---------------------------------------------------------------------------
# Sweep helpers
# ---------------------------------------------------------------------------

def _index_library_folders(library_dirs):
    """Index ID folders as (folder_name, library_dir, media_kind)."""
    index = {}
    for key, library_dir in library_dirs.items():
        if not library_dir or not os.path.isdir(library_dir):
            continue
        try:
            entries = os.listdir(library_dir)
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(library_dir, entry)
            if not os.path.isdir(full):
                continue
            if not re.search(r"\{(tmdb|tvdb)-", entry):
                continue
            index[entry.lower()] = (entry, library_dir, key)
    return index


def _infer_media_kind(*names):
    """Return 'tv' when a torrent/file name carries episode/season markers."""
    for name in names:
        season, episode = _parse_season_episode(name)
        if season is not None or episode is not None:
            return "tv"
    return None


def _title_prefix(s):
    """Extract the leading title words from a release/folder name, stopping at
    the first season/year/SxxExx/quality marker.

    'Landman.S02.COMPLETE' -> 'landman'
    'Landman (2024) {tvdb-397424}' -> 'landman'   (after stripping (year){id})
    'Beast.2026.2160p.WEB' -> 'beast'
    '2 Broke Girls' -> '2 broke girls'
    """
    if not s:
        return ""
    # Strip {id} and (year) suffixes first (library-folder form).
    s = re.sub(r"\s*\{[^}]+\}\s*$", "", s)
    s = re.sub(r"\s*\(\d{4}\)\s*$", "", s)
    # Cut at the first season/episode/year/quality/resolution/complete marker.
    s = re.split(r"[.\s\-_]*(?:s\d{1,2}e\d{1,3}|s\d{1,2}\b|\d{1,2}x\d{1,3}|"
                 r"\b19\d{2}\b|\b20\d{2}\b|\b2160p\b|\b1080p\b|\b720p\b|"
                 r"\b480p\b|\b4k\b|\buhd\b|\bcomplete\b|\bseason\s*\d)",
                 s, maxsplit=1, flags=re.IGNORECASE)[0]
    return _normalize_title(s)


def _match_torrent_to_folder(torrent_name, index, media_kind=None):
    """Best-effort match of a raw-mount torrent folder name to an existing
    {tmdb-ID}/{tvdb-ID} library folder. Returns (folder_name, library_dir)
    or None.

    Matches on the leading title prefix (everything before season/year/quality
    markers), so 'Landman.S02.COMPLETE' matches 'Landman (2024) {tvdb-397424}'.
    """
    tn_prefix = _title_prefix(torrent_name)
    if not tn_prefix:
        return None
    best = None
    best_score = 0
    for low_key, value in index.items():
        folder_name, library_dir = value[:2]
        folder_kind = value[2] if len(value) > 2 else None
        if media_kind and folder_kind and folder_kind != media_kind:
            continue
        base_prefix = _title_prefix(folder_name)
        if not base_prefix:
            continue
        # Require the title prefixes to overlap (one starts with the other).
        if base_prefix.startswith(tn_prefix) or tn_prefix.startswith(base_prefix):
            # Prefer the longest matching prefix (most specific).
            score = min(len(base_prefix), len(tn_prefix))
            if score > best_score:
                best_score = score
                best = (folder_name, library_dir)
    return best


def _normalize_title(s):
    """Lowercase, drop non-alphanumerics, collapse spaces — for fuzzy match."""
    if not s:
        return ""
    out = []
    for ch in str(s).lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _folder_already_links(folder_path, target):
    """True if folder already contains a symlink pointing at `target`."""
    try:
        for entry in os.listdir(folder_path):
            full = os.path.join(folder_path, entry)
            if os.path.islink(full):
                try:
                    if os.path.realpath(full) == os.path.realpath(target):
                        return True
                except OSError:
                    pass
    except OSError:
        pass
    return False


def _folder_already_links_any(folder_path, targets):
    """True if folder (recursively) already symlinks any of `targets`.

    Used for TV sweeps where episodes live under Season NN/ subfolders.
    """
    target_set = set()
    for t in targets:
        try:
            target_set.add(os.path.realpath(t))
        except OSError:
            pass
    if not target_set:
        return False
    try:
        for root, _dirs, entries in os.walk(folder_path):
            for entry in entries:
                full = os.path.join(root, entry)
                if os.path.islink(full):
                    try:
                        if os.path.realpath(full) in target_set:
                            return True
                    except OSError:
                        pass
    except OSError:
        pass
    return False
