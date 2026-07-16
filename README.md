# plex_debrid + TorBox + Web UI

An Umbrel app that bundles the [plex_debrid](https://github.com/itsToggle/plex_debrid)
automation engine (itsToggle, v2.95, archived) with a **custom TorBox debrid
provider** and a **browser Web UI** so every setting is configurable without SSH.

This repo builds the Docker image published at
`ghcr.io/zeroq-bit/plex_debrid:main`. The Umbrel app manifest lives in the
[ZeroQ-bit/Umbrel-Store](https://github.com/ZeroQ-bit/Umbrel-Store) repo under
`zeroq-plex-debrid/`.

## What it does

`plex_debrid` watches your **Plex Watchlist / Trakt / Overseerr** for new
movies and shows, scrapes the best matching torrent, and sends it to your
debrid provider's cloud. It then triggers a Plex library scan so the media
appears. You configure everything once via the Web UI; after that it polls
automatically.

> **TorBox is not in upstream plex_debrid.** This package adds a conforming
> TorBox provider (`patch/torbox.py`) that talks to TorBox's API
> (`checkcached`, `createtorrent`, `mylist`, `requestdl`).

## What it does NOT do

**plex_debrid never mounts anything.** It only places torrents in your debrid
cloud and scans your library. To actually *stream* the media in Plex, keep the
separate **Debrid Mount** Umbrel app running — it provides the rclone/TorBox
filesystem that plex_debrid fills and Plex reads.

| App | Role |
|-----|------|
| **plex_debrid** (this) | Automation engine + Web UI (this is plex_debrid's job) |
| Debrid Mount | The rclone FUSE mount (TorBox → `.vortexo-source`) |
| Vortexo Server | Apple TV bridge |

## Architecture

```
Umbrel → app_proxy (:19456) → container :8080
                                ├─ web_ui/server.py   (HTTP API + dashboard)
                                └─ plex_debrid main.py -service  (child, supervised)
```

One container, two processes. The Web UI stays up even if the engine crashes;
it manages the engine as a child subprocess (start/stop/restart from the page).

## Repository layout

```
plex-debrid/
├── Dockerfile              # python:3.11-slim + plex_debrid + web_ui
├── entrypoint.sh           # seed settings.json, launch web_ui
├── plex_debrid/            # vendored itsToggle/plex_debrid v2.95 (TorBox patched in)
│   └── debrid/services/torbox.py   # the TorBox provider
├── patch/torbox.py         # canonical copy of the provider (kept in sync)
├── web_ui/
│   ├── server.py           # stdlib HTTP server + engine supervisor
│   ├── settings_bridge.py  # settings.json ↔ UI groups
│   └── static/{index.html,app.css}
├── tests/test_torbox.py    # provider tests (mocked TorBox API), 0 deps
└── .github/workflows/docker.yml   # build + push to GHCR
```

## Web UI endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dashboard SPA |
| GET | `/api/health` | Liveness (healthcheck target) |
| GET | `/api/settings` | Settings grouped for the UI |
| POST | `/api/settings` | Save edits, restart engine |
| POST | `/api/test-torbox` | Validate a TorBox key against `/user/me` |
| GET | `/api/status` | Engine running? + log tail |
| POST | `/api/engine/{start,stop,restart}` | Control the engine |

## The TorBox provider (`patch/torbox.py`)

Conforms to plex_debrid's duck-typed debrid interface (`name`, `short="TB"`,
`api_key`, `session`, `setup`, `check`, `download`). Maps to TorBox endpoints:

| plex_debrid method | TorBox API |
|---|---|
| `check(element)` | `GET /v1/api/torrents/checkcached?format=object&listFiles=true&hash=…` |
| `download(element)` | `POST /v1/api/torrents/createtorrent` → poll `GET /mylist` → `GET /requestdl` |
| key validation (UI) | `GET /v1/api/user/me` |

Registration edits the vendored `debrid/services/__init__.py` (import + append
to `__subclasses__()`) and `settings/__init__.py` (adds the `TorBox API Key`
setting entry).

## Development

```sh
# Run the TorBox provider tests (no deps beyond stdlib + requests/regex)
python3 tests/test_torbox.py

# Run the Web UI locally
PD_ROOT=$PWD/plex_debrid PD_CONFIG_DIR=./data/config PD_LOG_DIR=./data/logs PD_WEB_PORT=8080 \
  python3 web_ui/server.py

# Build the image
docker build -t plex_debrid .
```

## Smoke test (requires your real TorBox key)

The unit tests use mocked TorBox responses. To confirm against the live API:

1. Open the Web UI → Debrid section → paste your TorBox API key → **Test**.
2. If it returns `✓ valid … plan: Pro`, the provider auth path works.
3. Add a Plex/Trakt source + a scraper (e.g. Torrentio), Save & Restart, and
   add something to your Plex Watchlist — watch the engine log for the scrape
   + TorBox `createtorrent` call.

## Attribution

- [plex_debrid](https://github.com/itsToggle/plex_debrid) by itsToggle (MIT-style).
  Vendored unmodified except for the TorBox provider registration.
- TorBox provider, Web UI, and Umbrel packaging © ZeroQ.
