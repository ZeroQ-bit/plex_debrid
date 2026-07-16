# TorBox debrid provider for plex_debrid.
#
# This module conforms to plex_debrid's duck-typed debrid provider interface
# (see debrid/services/realdebrid.py and alldebrid.py for the reference shape).
# Required module-level attributes: name, short, api_key, session
# Required functions: setup(cls, new=False), check(element, force=False),
#                     download(element, stream=True, query='', force=False)
#
# TorBox API docs: https://api-docs.torbox.app/  (Swagger: https://api.torbox.app/docs)
# Auth: API key passed in the Authorization: Bearer header.
#       (The ?api_key= query param returns 401 even for valid keys.)
#
# Endpoints used:
#   GET  /v1/api/torrents/checkcached  — batch instant-availability by infohash
#   POST /v1/api/torrents/createtorrent — add a torrent by magnet
#   GET  /v1/api/torrents/mylist       — list user torrents (status + files)
#   GET  /v1/api/torrents/requestdl    — get a direct-downloadable file URL
#   GET  /v1/api/user/me               — validate the API key (used by the Web UI)
#import modules
from base import *
from ui.ui_print import *
import releases

# (required) Name of the Debrid service — must match what setup() adds to `active`.
name = "TorBox"
# (required) Short cache marker appended to release.cached for cached releases.
short = "TB"
# (required) Authentication of the Debrid service. Set at runtime via settings.
api_key = ""
# Define Variables
session = requests.Session()

# TorBox API base. The version segment (/v1/) is part of every call.
API_BASE = "https://api.torbox.app/v1/api"


def setup(cls, new=False):
    from debrid.services import setup
    setup(cls, new)


# Error Log
errors = [
    [400, " bad Request (see error message)"],
    [401, " unauthorized (api key invalid or missing) — check your TorBox API key"],
    [403, " forbidden (account locked / not premium / abuse protection)"],
    [404, " not found (invalid torrent or file id)"],
    [429, " rate limited (too many requests)"],
    [500, " internal server error (retry later)"],
    [503, " service unavailable"],
]


def logerror(response):
    if not response.status_code in [200, 201]:
        desc = ""
        for error in errors:
            if response.status_code == error[0]:
                desc = error[1]
        ui_print("[torbox] error: (" + str(response.status_code) + desc + ") " + str(response.content),
                 debug=ui_settings.debug)
    if response.status_code == 401:
        ui_print("[torbox] error: (401 unauthorized): TorBox api key does not seem to work. check your torbox settings.")


def _auth_header():
    """Return the Authorization header value for the configured API key.

    TorBox requires the key in the Authorization: Bearer header. The
    ?api_key= query param returns 401 even for valid keys, so we must NOT
    append it to URLs.
    """
    return 'Bearer ' + str(api_key) if api_key else None


# Get Function
def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}
    if api_key:
        headers['Authorization'] = _auth_header()
    response = None
    try:
        response = session.get(url, headers=headers)
        logerror(response)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        ui_print("[torbox] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response


# Post Function
def post(url, data=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}
    if api_key:
        headers['Authorization'] = _auth_header()
    response = None
    try:
        response = session.post(url, headers=headers, data=data)
        logerror(response)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        if hasattr(response, "status_code"):
            if response.status_code >= 300:
                ui_print("[torbox] error: (json exception): " + str(e), debug=ui_settings.debug)
        else:
            ui_print("[torbox] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response


# (required) Download Function.
# Adds the matched release's magnet to TorBox and resolves the cached files into
# direct-download links stored on release.download. Returns True on success.
#
# Mirrors alldebrid.download(): for a cached release it re-confirms availability,
# creates the torrent, waits for it to be ready, then requests a direct link for
# each wanted file.
def download(element, stream=True, query='', force=False):
    cached = element.Releases
    if query == '':
        query = element.deviation()
    wanted = [query]
    if not isinstance(element, releases.release):
        wanted = element.files()
    for release in cached[:]:
        # if release matches query
        if regex.match(query, release.title, regex.I) or force:
            if stream:
                # Cached download path. Re-check instant availability for this hash,
                # then create + resolve. If not actually instant, fall back to the
                # uncached add path when allowed.
                magnet = str(release.download[0])
                info_hash = release.hash.lower()

                # 1) Confirm cached + fetch its files in one call.
                check_url = (API_BASE + "/torrents/checkcached?format=object&listFiles=true"
                             + "&hash=" + info_hash)
                check_resp = get(check_url)
                torrent_files = []
                entry = _lookup_cached_entry(check_resp, info_hash)
                if entry is not None:
                    # entry.files may be None for cached-but-fileless entries.
                    torrent_files = entry.files if entry and entry.files else []

                if len(torrent_files) == 0:
                    ui_print("[torbox] error: release not cached (no files): " + release.title,
                             ui_settings.debug)
                    # Defer to uncached handling below only if stream is False.
                    continue

                # 2) Create the torrent in the user's account from the magnet.
                create_resp = post(API_BASE + "/torrents/createtorrent", data={'magnet': magnet})
                torrent_id = None
                try:
                    # createtorrent returns {data: {torrent_id: <id>}} on success.
                    torrent_id = getattr(create_resp.data, 'torrent_id')
                except Exception:
                    ui_print('[torbox] error: could not add magnet for release: ' + release.title,
                             ui_settings.debug)
                    continue

                # 3) Wait until TorBox reports the torrent ready (cached torrents
                #    resolve almost immediately; poll briefly just in case).
                ready = _wait_until_ready(torrent_id)
                if not ready:
                    ui_print('[torbox] error: torrent never became ready: ' + release.title,
                             ui_settings.debug)
                    continue

                # 4) Request a direct-downloadable URL for each wanted file.
                wanted_ids = _select_file_ids(torrent_files, wanted, force)
                direct_links = []
                for file_id in wanted_ids:
                    dl_url = (API_BASE + "/torrents/requestdl?torrent_id=" + str(torrent_id)
                              + "&file_id=" + str(file_id))
                    dl_resp = get(dl_url)
                    try:
                        direct_links += [dl_resp.data]
                    except Exception:
                        ui_print('[torbox] error: could not resolve direct link for file_id '
                                 + str(file_id), ui_settings.debug)
                        continue

                if len(direct_links) > 0:
                    release.download = direct_links
                    ui_print('[torbox] adding cached release: ' + release.title)
                    return True
                else:
                    ui_print('[torbox] error: no direct links resolved for: ' + release.title,
                             ui_settings.debug)
                    return False
            else:
                # Uncached download path — just submit the magnet and return.
                try:
                    post(API_BASE + "/torrents/createtorrent", data={'magnet': str(release.download[0])})
                    ui_print('[torbox] adding uncached release: ' + release.title)
                    return True
                except Exception:
                    continue
        else:
            ui_print('[torbox] error: rejecting release: "' + release.title
                     + '" because it doesnt match the allowed deviation', ui_settings.debug)
    return False


def _wait_until_ready(torrent_id, timeout=60, interval=2):
    """Poll mylist until the torrent's status indicates it is downloaded/cached."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = get(API_BASE + "/torrents/mylist")
        try:
            for t in resp.data:
                if str(getattr(t, 'id', '')) == str(torrent_id):
                    status = getattr(t, 'download_state', '')
                    # TorBox download_state values: cached / downloading / paused /
                    # error / ... . 'cached' (and a few equivalents) mean ready.
                    if status in ("cached", "completed", "seeding"):
                        return True
                    if status in ("error", "failed"):
                        return False
                    break
        except Exception:
            pass
        time.sleep(interval)
    return False


def _select_file_ids(torrent_files, wanted, force):
    """Pick the file ids whose names match the wanted patterns.

    torrent_files is the list returned by checkcached (format=object, listFiles=true).
    Falls back to all files when force is set or nothing matches.
    """
    if force or len(wanted) == 0:
        return [f.id for f in torrent_files if hasattr(f, 'id')]
    wanted_patterns = [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in wanted]
    matched = []
    for f in torrent_files:
        name = str(getattr(f, 'name', ''))
        if name.endswith('.exe') or name.endswith('.txt'):
            continue
        for pattern in wanted_patterns:
            if pattern.search(name):
                matched.append(f.id)
                break
    if len(matched) == 0:
        # Nothing matched the wanted patterns; return the biggest file as a
        # pragmatic fallback (single-file releases).
        best = None
        for f in torrent_files:
            if best is None or int(getattr(f, 'size', 0)) > int(getattr(best, 'size', 0)):
                best = f
        return [best.id] if best is not None else [f.id for f in torrent_files if hasattr(f, 'id')]
    return matched


def _lookup_cached_entry(response, info_hash):
    """Look up a cached-torrent entry from a checkcached (format=object) response.

    TorBox keys the data object by the UPPERCASE infohash; release hashes are
    stored lowercase. Match case-insensitively so callers don't have to care.
    Returns the entry object or None.
    """
    if response is None or not hasattr(response, 'data'):
        return None
    data = getattr(response, 'data')
    if data is None:
        return None
    h = info_hash.lower()
    if hasattr(data, h):
        return getattr(data, h)
    if hasattr(data, h.upper()):
        return getattr(data, h.upper())
    return None


# (required) Check Function.
# Queries TorBox's batch instant-availability endpoint and marks cached releases
# by appending the 'TB' short string to release.cached. Follows the simpler
# alldebrid pattern (no per-file version tree).
def check(element, force=False):
    if force:
        wanted = ['.*']
    else:
        wanted = element.files()
    hashes = []
    for release in element.Releases[:]:
        if len(release.hash) == 40:
            hashes += [release.hash]
        else:
            ui_print("[torbox] error (missing torrent hash): ignoring release '"
                     + release.title + "' ", ui_settings.debug)
            element.Releases.remove(release)
    if len(hashes) == 0:
        return
    # TorBox checkcached accepts repeated hash= params. Cap at 200 like the other
    # providers do for their batch endpoints.
    hash_query = "&hash=".join(h[:200] for h in hashes[:200])
    # Always query with the full list and lowercase the comparison, since TorBox
    # keys the response object by the *uppercase* infohash in format=object mode.
    response = get(API_BASE + "/torrents/checkcached?format=object&listFiles=true&hash=" + hash_query)
    ui_print("[torbox] checking and sorting all release files ...", ui_settings.debug)
    for release in element.Releases:
        if _lookup_cached_entry(response, release.hash) is not None:
            release.cached += ['TB']
    ui_print("done", ui_settings.debug)
