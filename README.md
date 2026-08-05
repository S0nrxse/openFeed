# OpenFeed

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
- FreshRSS synchronisation for subscriptions
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

## Requirements

- Docker Engine
- Docker Compose v2
- Linux, macOS, or another Docker-capable host

## Quick Start

1. Create your local environment:

   ```bash
   cp .env.example .env
   ```

2. Restrict it:

   ```bash
   chmod 600 .env
   ```

3. Replace every `replace-with-*` value in `.env`. Generate secrets with:

   ```bash
   openssl rand -hex 32
   ```

4. Start the application and worker:

   ```bash
   ./deploy.sh --profile worker up -d --build
   ```

5. Open `http://localhost:8060`.

FreshRSS is available on `http://localhost:8050` and Invidious on `http://localhost:8061`
by default. All published ports bind to loopback unless changed in `.env`.

## Configuration

`.env.example` documents the supported settings. `.env` is ignored by Git and must never
be committed.

Authentication is disabled by default. To enable Authentik OIDC, configure the
`OPENFEED_OIDC_*` values, set `OPENFEED_AUTH_MODE=authentik_oidc`, and set
`OPENFEED_REQUIRE_LOGIN=true`. Access and administrator groups are optional
comma-separated values in `OPENFEED_REQUIRED_GROUP` and `OPENFEED_ADMIN_GROUPS`.

Persistent files are stored under `OPENFEED_DATA_DIR`, which defaults to `./data`.

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
can be merged, and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for participation
expectations.

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
[yt-dlp](https://github.com/yt-dlp/yt-dlp), [FreshRSS](https://freshrss.org/),
[SponsorBlock](https://sponsor.ajay.app/) and
[Return YouTube Dislike](https://returnyoutubedislike.com/). It is not affiliated with
YouTube or Google.
