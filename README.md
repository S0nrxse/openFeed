# OpenFeed

OpenFeed is a self-hosted private video feed and library. It combines FreshRSS subscriptions, Invidious playback and metadata, PostgreSQL persistence, Redis, and Gorse recommendations behind a FastAPI application.

## Features

- Personalized and subscription-based video feeds, with recommendations and
  subscriptions blended together and rotating "catch up" shelves
- A separate shorts shelf and player
- Search, channels, playlists, collections, likes, and watch history
- Watch page with theater mode, like and dislike counts, threaded comment
  replies, and downloads
- FreshRSS synchronization
- Invidious-backed playback and discovery, with a configurable fallback ladder,
  per-source health tracking, and a diagnostic that names the failing hop
- Gorse recommendation signals
- Optional Authentik OIDC login and group-based access
- Responsive desktop and mobile interface

## Requirements

- Docker Engine
- Docker Compose v2
- Linux, macOS, or another Docker-capable host

## Quick Start

1. Create your local environment:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

2. Replace every `replace-with-*` value in `.env`. Generate secrets with a command such as:

   ```bash
   openssl rand -hex 32
   ```

3. Start the application and worker:

   ```bash
   ./deploy.sh --profile worker up -d --build
   ```

4. Open `http://localhost:8060`.

FreshRSS is available on `http://localhost:8050` and Invidious on `http://localhost:8061` by default. All published ports bind to loopback unless changed in `.env`.

## Configuration

`.env.example` documents the supported settings. `.env` is ignored by Git and must never be committed.

Authentication is disabled by default. To enable Authentik OIDC, configure the `OPENFEED_OIDC_*` values, set `OPENFEED_AUTH_MODE=authentik_oidc`, and set `OPENFEED_REQUIRE_LOGIN=true`. Access and administrator groups are optional comma-separated values in `OPENFEED_REQUIRED_GROUP` and `OPENFEED_ADMIN_GROUPS`.

Persistent files are stored under `OPENFEED_DATA_DIR`, which defaults to `./data`.

### Local Overrides

`deploy.sh` automatically loads an ignored `compose.local.yml` when present. Use it for host-specific reverse-proxy networks, container names, network aliases, or other settings that do not belong in the public repository.

## Optional Profiles

```bash
./deploy.sh --profile vectors up -d openfeed-qdrant
./deploy.sh --profile search up -d openfeed-typesense
```

The background synchronization worker uses the `worker` profile.

## Testing

Build the application image and run the unit tests:

```bash
docker build -t openfeed-app:test app
docker run --rm openfeed-app:test python -m unittest discover -s openfeed/tests -v
```

Validate Compose configuration with:

```bash
./deploy.sh config --quiet
```

## Security

- Do not expose PostgreSQL, Redis, Gorse, or optional search services publicly.
- Keep `.env`, local Compose overrides, database files, and backups outside version control.
- Use unique random values for every password and signing key.
- Put public deployments behind TLS and enable OIDC authentication.
- Review [`SECURITY.md`](SECURITY.md) before exposing an instance.

## Project Status

OpenFeed is under active development. Back up all persistent data before upgrading.

## License

OpenFeed is licensed under the GNU Affero General Public License v3.0. See
[`LICENSE`](LICENSE) for the full text.

Section 13 is the clause worth knowing about: if you run a modified copy where
other people can reach it over a network, you have to offer those users the
corresponding source. The sidebar carries a source link for that reason, and
`OPENFEED_SOURCE_URL` lets you point it at your own fork.
