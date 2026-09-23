<p align="center">
  <img src="assets/openfeed-logo.png" width="120" alt="OpenFeed">
</p>

<h1 align="center">OpenFeed</h1>

OpenFeed is a privacy-focused self-hosted YouTube front end. It builds a personalised feed from your own
subscriptions and watch history, plays videos through a local Invidious instance, and
keeps every piece of your watch data on your computer.

> **Alpha.** This project is early and under active development. Expect bugs, expect
> rough edges, and expect things to change as features land. Back up `OPENFEED_DATA_DIR`
> before upgrading, and do not treat an OpenFeed instance as the only copy of anything.

## Inspiration

The interface and the idea come from Juxtopposed's
[**I Redesigned the ENTIRE YouTube UI from Scratch**](https://youtu.be/OUM6XmhViN4).
OpenFeed is an independent implementation of that idea rather than anything affiliated
with, endorsed by, or derived from the video's author.

## Features

### Library and account

- Search, channels, playlists, collections, likes, and watch history
- Channel pages with tabs, filters, links and community posts
- Direct YouTube RSS polling for subscriptions
- Optional Authentik OIDC login with group-based access
- Responsive desktop and mobile interface

### Feed and recommendations

- Content-based ranking built from a TF-IDF topic profile of what you actually watch,
  with exponential recency decay so interests from years ago stop outweighing current ones
- Watch time as a ranking signal, not just clicks
- Candidate acquisition from varied channels, trending and topic seeds — the feed can
  surface channels you have never subscribed to
- Feed customisation with per-signal controls and real measured coverage
- Google Takeout import for watch history *and* subscriptions, with the library
  backfilled from the channels it finds
- Offline evaluation harness (recall@k, MRR, nDCG) for measuring ranking changes

### Watching

- Watch page with theater mode, resume position, chapters and a YouTube-style action row
- Threaded comments with timestamps, creator hearts, and highlighted creator replies
- Shorts shelf and dedicated player
- Downloads at full quality via yt-dlp and FFmpeg, with a resolution picker and
  progress tracking

### Playback reliability

- Invidious as the primary backend, with a configurable fallback ladder and per-source
  health tracking
- yt-dlp as a fallback media backend for streams Invidious cannot produce
- A diagnostic endpoint that walks Invidious → player → manifest → media segment and
  names the first broken hop

### Optional integrations

Each is **off by default**, read-only, server-side, and contains a privacy note in
Settings. Nothing is enabled without you asking for it -- making you, the user aware of the consequences of 3rd party software.

- **SponsorBlock** — segment skipping with timeline markers and per-category actions.
  Looked up by a 4-character hash prefix, so the SponsorBlock server never learns which
  video you asked about.
- **Return YouTube Dislike** — dislike counts. Your own likes and dislikes stay local and
  are never submitted.
- **Community posts and polls** — read-only. Poll voting is simulated locally and
  labelled as such, because YouTube publishes no per-option breakdown.

## Installation

### 1. Get the code

```bash
git clone https://github.com/S0nrxse/openFeed.git && cd openFeed
```

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and replace every `replace-with-*` placeholder. Any random string works:

```bash
openssl rand -hex 32
```

### 3. Start

```bash
./deploy.sh --profile worker up -d --build
```

The first run builds the app image and pulls roughly 700 MB of containers, so give it a
few minutes. Later starts take seconds.

### 4. Open it

| Service | URL | What it is |
| --- | --- | --- |
| **OpenFeed** | http://localhost:8060 | The app |
| Invidious | http://localhost:8061 | Playback and metadata backend |

All ports bind to `127.0.0.1` unless you change them in `.env`, so nothing is reachable
from outside the machine by default.

## Managing the Stack

`deploy.sh` passes everything through to `docker compose`, so any Compose command works:

```bash
./deploy.sh logs -f openfeed-app     # follow the app log
```

```bash
./deploy.sh ps                       # what is running
```

```bash
./deploy.sh restart openfeed-app     # restart one service
```

```bash
./deploy.sh down                     # stop everything
```

To update to the latest code:

```bash
git pull && ./deploy.sh --profile worker up -d --build
```

`openfeed-app` and `openfeed-worker` share one image, so a rebuild has to recreate both —
the command above does.

## Requirements

- Docker Compose v2 (bundled with Docker Desktop, or the `docker-compose-plugin` package)
- Roughly 4 GB of RAM for the full stack, and disk for whatever you download
- Linux, macOS, or Windows with WSL2

## Platform Notes

Everything runs in containers, so the stack itself is identical everywhere. Only the setup
differs.

**Linux.** Install Docker Engine and the Compose plugin, then add yourself to the `docker`
group so you do not need `sudo`:

```bash
sudo usermod -aG docker $USER
```

Log out and back in for it to apply. On Fedora and RHEL, SELinux blocks containers from
writing to `./data`; `sudo chcon -Rt svirt_sandbox_file_t ./data` fixes it.

**macOS.** Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and
give it at least 4 GB under Settings → Resources. The default is sometimes 2 GB, which is
not enough for the whole stack and shows up as containers dying without explanation. Apple
Silicon is fully supported.

**Windows.** Install Docker Desktop with the WSL2 backend and run everything from inside
WSL2. Clone into the Linux home directory rather than `/mnt/c` — a clone on the Windows
filesystem makes Postgres very slow and can stop containers starting at all:

```bash
cd ~ && git clone https://github.com/S0nrxse/openFeed.git
```

If you would rather not use WSL2, `deploy.sh` will not run in PowerShell, but the command
it wraps does:

```powershell
docker compose --env-file .env -f compose.yml --profile worker up -d --build
```

## Configuration

`.env.example` documents the supported settings. `.env` is ignored by Git and must never
be committed.

Authentication is disabled by default. To enable Authentik OIDC, configure the
`OPENFEED_OIDC_*` values, set `OPENFEED_AUTH_MODE=authentik_oidc`, and set
`OPENFEED_REQUIRE_LOGIN=true`. Access and administrator groups are optional
comma-separated values in `OPENFEED_REQUIRED_GROUP` and `OPENFEED_ADMIN_GROUPS`.

Persistent files are stored under `OPENFEED_DATA_DIR`, which defaults to `./data`.

### API Authentication

Sign in with an account authorized to use OpenFeed, then open **Settings → API tokens**
(direct link: `/?settings=api-tokens`; `/settings` redirects there). Enter a name,
choose **Read only** or **Read and write**, and click **Create token**. Use **Copy token**
before closing Settings: the full value is shown once and is cleared when the
dialog closes. The same pane lists token prefixes/access/last-use and supports
revocation. Creation is also available through `POST /api/tokens` using that
browser session. Send the one-time
plaintext credential as `Authorization: Bearer ofd_...`. Only its hash is stored.
For actor-owned integrations, issue a separate credential from each actor's browser
session and store it in that actor's credential store. The API uses the issuer's
`user_key`; headers and request payloads cannot choose another token owner or add
group/admin claims.

- Every valid, unrevoked token delegates ordinary app access, including when
  `OPENFEED_REQUIRED_GROUP` is configured. Existing tokens work without group metadata.
- New tokens snapshot the browser issuer's admin capability into a server-derived
  `delegated_admin` boolean. Existing tokens default to `false` and gain no admin
  privileges. Only tokens with this persisted claim can access the admin-only
  `GET /api/status/full` and `GET /api/sync/runs` endpoints.
- Read-only tokens permit only GET/HEAD/OPTIONS. This restriction applies globally
  before API mutation handlers, including for admin-delegated tokens.
- Listing, creating, and revoking tokens require an authorized, authenticated browser
  session. Bearer tokens cannot manage credentials, even with admin delegation.
- Delegation is a snapshot, not a live IdP group lookup. Revoke a token to withdraw
  its access; revoke and reissue to change its delegated capabilities.
- Invalid or revoked bearer credentials receive 401 on the API and `/healthz`, even
  alongside a valid browser cookie. Health checks without credentials remain public.

Use `GET /api/` to verify `authenticated`, `via_token`, `user_key`, `read_only`, and
`is_admin` and discover the API. Route documentation is available at `/docs`,
`/redoc`, and `/openapi.json`.

On startup, `db.init_db()` applies the idempotent token migration:
`ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS delegated_admin BOOLEAN NOT NULL DEFAULT false`.
It applies to new and existing databases and preserves token hashes, ownership,
read-only flags, and revocations. No credential rotation is needed for ordinary
access; delegated admin access requires a newly issued token from an admin session.

### Local Overrides

`deploy.sh` automatically loads an ignored `compose.local.yml` when present. Use it for
host-specific reverse-proxy networks, container names, network aliases, or other settings
that do not belong in the public repository.

## Optional Profiles

```bash
./deploy.sh --profile vectors up -d openfeed-qdrant
```

```bash
./deploy.sh --profile search up -d openfeed-typesense
```

The background synchronisation worker uses the `worker` profile.

## Testing

The suite needs no database and no network. Build the image:

```bash
docker build -t openfeed-app:test app
```

Then run it:

```bash
docker run --rm openfeed-app:test python -m unittest discover -s openfeed/tests -v
```

Validate Compose configuration with:

```bash
./deploy.sh config --quiet
```

## Best security practices

- Do not expose PostgreSQL, Redis, or the optional Qdrant and Typesense services publicly.
- Keep `.env`, local Compose overrides, database files, and backups outside version control.
- Use unique random values for every password and signing key.
- Put public deployments behind TLS and enable OIDC authentication.
- Review [`SECURITY.md`](SECURITY.md) before exposing an instance.

## Contributing

Contributions are welcome. Currently everything goes through a reviewed pull request — see
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow and what a change needs before it
can be merged.

## License

```
Copyright (C) 2026 S0nrxse and OpenFeed contributors

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.
```

The full text is in [`LICENSE`](LICENSE).

Section 13 is the clause worth knowing about: if you run a modified copy where other
people can reach it over a network, you have to offer those users the corresponding
source. The sidebar carries a source link for that reason, and `OPENFEED_SOURCE_URL` lets
you point it at your own fork.

## Acknowledgements

OpenFeed stands on [Invidious](https://invidious.io/),
[yt-dlp](https://github.com/yt-dlp/yt-dlp),
[SponsorBlock](https://sponsor.ajay.app/) and
[Return YouTube Dislike](https://returnyoutubedislike.com/). It is not affiliated with
YouTube or Google.
