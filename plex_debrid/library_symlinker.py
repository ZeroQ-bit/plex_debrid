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

import requests

# TorBox API (matches debrid/services/torbox.py).
TORBOX_API_BASE = "https://api.torbox.app/v1/api"

# Video MIME types we consider for symlinking.
_VIDEO_MIMETYPES = ("video/",)
# File extensions we accept when MIME is missing/unreliable.
_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts")
# Sample-ish tags we avoid when picking the "main" file.
_SKIP_NAMES = ("sample", ".exe", ".txt", ".nfo", ".jpg", ".png", ".sfv")


def _log(log_fn, msg):
    if log_fn:
        try:
            log_fn(msg)
        except Exception:
            pass


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
            if rest:
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


def _list_raw_folder(mount_dir, torrent_name):
    """List files in the raw mount folder for a torrent.

    torrent_name is TorBox's `name` field (the folder under .vortexo-source/).
    Returns a list of (filename, full_path) tuples, or [] if the folder is
    missing/unreadable.
    """
    folder = os.path.join(mount_dir, torrent_name)
    if not os.path.isdir(folder):
        return []
    out = []
    try:
        for entry in sorted(os.listdir(folder)):
            full = os.path.join(folder, entry)
            if os.path.isfile(full):
                out.append((entry, full))
    except OSError:
        return []
    return out


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
    """Create library_dir/folder_name/filename -> target. Idempotent."""
    folder = os.path.join(library_dir, folder_name)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        _log(log_fn, f"could not create library folder {folder}: {e}")
        return None
    link_path = os.path.join(folder, filename)
    # Idempotent: existing valid symlink is a no-op.
    if os.path.islink(link_path) or os.path.exists(link_path):
        return link_path
    try:
        os.symlink(target, link_path)
        _log(log_fn, f"symlinked {filename[:60]} -> {target[:80]}")
        return link_path
    except OSError as e:
        _log(log_fn, f"could not create symlink {link_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def symlink_item(item, mount_dir, library_dirs, log_fn=None):
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

        raw_root = os.path.join(mount_dir, ".vortexo-source")
        files = _list_raw_folder(raw_root, torrent_name)
        if not files:
            # Mount may not be ready yet, or the folder name differs.
            _log(log_fn, f"skipped symlink: raw mount folder not found for "
                 f"{torrent_name[:60]!r} (is Debrid Mount running?)")
            return None
        picked = _pick_video_file(files, getattr(release, "title", ""))
        if not picked:
            _log(log_fn, f"skipped symlink: no video file in {torrent_name[:60]!r}")
            return None
        file_name, file_path = picked

        folder_name = library_folder_name(item, provider, guid)
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


def sweep(api_key, mount_dir, library_dirs, log_fn=None, max_items=None):
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
            match = _match_torrent_to_folder(name, existing)
            if not match:
                continue
            folder_name, library_dir = match
            folder_path = os.path.join(library_dir, folder_name)
            files = _list_raw_folder(raw_root, name)
            if not files:
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
        _log(log_fn, f"sweep complete: {created} new symlinks")
    except Exception as e:
        _log(log_fn, f"sweep failed: {e!r}")
    return created


# ---------------------------------------------------------------------------
# Sweep helpers
# ---------------------------------------------------------------------------

def _index_library_folders(library_dirs):
    """Return {folder_name_lower: (folder_name, library_dir)} for existing
    library folders that carry a {provider-id} tag."""
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
            index[entry.lower()] = (entry, library_dir)
    return index


def _match_torrent_to_folder(torrent_name, index):
    """Best-effort match of a raw-mount torrent folder name to an existing
    {tmdb-ID}/{tvdb-ID} library folder. Returns (folder_name, library_dir)
    or None.

    Matching is by normalized title+year prefix, since the library folder is
    'Title (Year) {id}' and the torrent name usually begins with the title.
    """
    tn = _normalize_title(torrent_name)
    if not tn:
        return None
    best = None
    best_score = 0
    for low_key, (folder_name, library_dir) in index.items():
        # Strip the '{id}' suffix for comparison.
        base = re.sub(r"\s*\{[^}]+\}\s*$", "", folder_name)
        norm_base = _normalize_title(base)
        if not norm_base:
            continue
        if tn.startswith(norm_base[:25]) or norm_base.startswith(tn[:25]):
            score = len(set(norm_base.split()) & set(tn.split()))
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
