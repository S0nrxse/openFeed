# Changelog

OpenFeed is alpha software. Back up `OPENFEED_DATA_DIR` before every upgrade.

## 0.2.0-alpha — unreleased

The first version published as an open-source repository. Upgrading from 0.1.0-alpha is
automatic on the first start; see [Upgrading](README.md#upgrading-feeds-and-per-user-subscriptions).

### Added

- Accounts for more than one person: passwords, passkeys, two-factor sign-in, OIDC and
  trusted-header sign-in, household profiles, and per-account recommendations over a
  shared catalog. See [docs/accounts.md](docs/accounts.md).
- Per-user subscriptions polled straight from YouTube's channel RSS feeds.
- API token management in Settings.
- A redesigned interface.
- A prebuilt multi-architecture image, `ghcr.io/s0nrxse/openfeed`, published for each
  release (set `OPENFEED_IMAGE` in `.env`).
- Continuous integration: the full test suite, including the PostgreSQL migration tests,
  and Compose validation on every push and pull request.

### Changed

- FreshRSS is no longer used. The feed tables are renamed to `feeds` and `feed_entries` on
  the first start, and subscriptions move to `POST`/`DELETE /api/subscriptions`.
- Invidious, the Invidious companion and Qdrant are pinned to tested versions instead of
  `:latest`.
- Repeat channels are spread apart in ranked home shelves, and home and filter pages
  render faster.

### Fixed

- Discovery no longer overwrites corrected publish dates.
- API token access, and download storage is kept separate per user.
- The test suite passes inside the release image: tests that read repository files skip
  when those files are absent, and the settings test no longer depends on whether yt-dlp
  is installed.

## 0.1.0-alpha — 2026-08-05

First tagged release.
