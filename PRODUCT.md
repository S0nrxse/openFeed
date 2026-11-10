# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are **self-hosters** — people already comfortable running a Docker
Compose stack who want a privacy-preserving YouTube front end. They edit `.env`
files, read container logs, and expect density and direct control over
hand-holding. They are administrator and viewer at once: the person who installs
OpenFeed is the person who watches on it every day.

The interface is used on both desktop and mobile.

One install serves one person, a household, or a public instance that strangers sign up
to. With sign-in on, each person has their own account (built-in username and password with
optional two-step verification, passkeys, any number of OIDC providers such as Authentik or
Pocket ID, or a forward-auth proxy; one account can link several), and every account's subscriptions,
history, likes, playlists, preferences and recommendations are its own. Several people can
also share one login through household profiles, each with their own subscriptions and
history. Feeds are fetched once for the whole install however many people follow them, but
who follows what belongs to each person. Videos are one shared catalog too (a cache of public
YouTube data), but each person is only ever offered their own part of it: what they follow,
what they searched, opened or imported, and what is related to that. One account's searches
never turn up in another's Home, Shorts, Up next or search.

On a shared install, an admin runs the server from Admin (Server, Users, Status): third-party
players and public instances, server switches for outside lookups, download rights and
per-member quotas, per-account rate limits, and maintenance. Members get only personal
settings and never see each other's data or the server's internals.

## Product Purpose

OpenFeed replaces YouTube's front end with a self-hosted one that builds a
personalised feed from the user's own subscriptions and watch history, plays
videos through a local Invidious instance, and keeps every piece of watch data on
the user's own machine.

Success is a feed good enough to use daily in place of YouTube — not a
demonstration that self-hosting is possible.

Status is **0.1.0-alpha**: early, under active development, with the README
explicitly warning users to back up `OPENFEED_DATA_DIR` before upgrading and never
to treat an instance as the only copy of anything.

## Positioning

Other privacy front ends drop personalisation along with the tracking — you get a
subscription list and nothing else. OpenFeed keeps the recommendation engine and
moves it onto the user's own hardware. Since ranker v2 (`app/openfeed/ranker.py`) the
Recommended shelves rank the whole library by how YouTube's own related-video lists link
each video to the channels the user follows and to videos they watched at least halfway or
liked. The shelf is a slot mix, the way YouTube's own home feed is built from several
candidate sources: "because you watched", new uploads from creators those links point to,
and the wider world of the user's channels. Rotation moves past what was already shown on
earlier days. Each card says why it is there. A mode that mixes in a popularity-and-age
prior is available in Settings. The same index orders the deeper Home pages, the Shorts
shelf and the watch page's Up next (the video's own related list first, as YouTube shows it),
and an imported Takeout history seeds it as weak evidence. The feed controls (how far to
wander, subscription priority, followed and muted topics) act on it directly. The original
TF-IDF topic and watch-time ranker stays available as the Classic mode, and the remaining
lanes and filter chips still use it. Candidate acquisition can surface channels the user
has never subscribed to.

The ranking is measurable rather than asserted, and the evidence is stated with its
limits. On a sealed test v2 placed held-out channels far above the old ranker, which scored
at chance (0.116 vs 0.004 nDCG@20 on the own library, 0.31 vs 0.04 on KuaiRec). Against the
strongest simple baseline the gain is not proven on OpenFeed's own data. A later feel audit
(blind judges, real profile) preferred the final mix over the earlier graph mode 19 of 20
times, at equal or better relevance. For a household that wants a live answer, the opt-in
"Help tune recommendations" interleaves Graph with Graph + popularity on the Recommended
shelf and reports which side gets opened more, entirely on the server. Details are in
README → Ranking and `docs/ranker-v2/`.

## Operating Context

- Runs as a Docker Compose stack; `./deploy.sh` wraps `docker compose`. Editing
  application code requires an image rebuild before the change is visible.
- Two user-facing services, all bound to `127.0.0.1` by default: the app
  (`:8060`) and Invidious for playback and metadata (`:8061`). Subscriptions
  are polled directly from YouTube RSS by the worker.
- Optional profiles: `worker` (background sync), `vectors` (Qdrant), `search`
  (Typesense).
- Roughly 4 GB of RAM for the full stack. Linux, macOS, or Windows via WSL2.
- Persistent files live under `OPENFEED_DATA_DIR`, default `./data`.
- Authentication is **off** by default; the common path is still a single trusted user
  on a private network. `OPENFEED_AUTH_MODE=accounts` turns on built-in accounts (first-run
  /setup, admin Users page, invites or open sign-up, no email), and Authentik OIDC with
  group-based access remains available (docs/accounts.md).
- Playback depends on third-party sources that break without warning; the stack
  carries a configurable fallback ladder and per-source health tracking because of
  it.

## Capabilities and Constraints

**Confirmed functionality.** Search, channels, playlists, collections, likes, and
watch history; channel pages with tabs, filters, links, and community posts;
direct YouTube RSS polling for subscriptions (per person, one fetch per followed feed);
OPML import and export of a person's subscriptions; Google Takeout import for watch
history and subscriptions with library backfill; on-demand caption text for one video as
data (`/api/transcript/{id}/fetch`), through the install's own Invidious; feed customisation with per-signal controls
and real measured coverage; watch page with theater mode, resume position,
chapters, and an action row; threaded comments with timestamps and creator hearts;
a Shorts shelf and dedicated player; downloads at full quality via yt-dlp and
FFmpeg with a resolution picker and progress tracking. Captions in every same-origin player,
proxied as WebVTT through OpenFeed, with automatic tracks labelled and the last choice
remembered; Media Session lock-screen and headphone controls; casting through the browser's
own Remote Playback API (no Google Cast SDK), shown only for sources that can be cast. Up to
six household profiles per login, with a minimal kids flag (no comments, no Shorts, no
age-restricted videos).

**Optional integrations**, each off by default, read-only, server-side, and
carrying a privacy note in Settings: SponsorBlock (queried by 4-character hash
prefix so the server never learns which video was requested), DeArrow community titles
and thumbnails (same hash-prefix lookup; thumbnail frames are cut on the server from the
Invidious stream, never fetched from DeArrow by video ID), Return YouTube
Dislike (the user's own votes stay local and are never submitted), read-only
community posts and polls, and live chat and chat replay (read through yt-dlp on the server
without cookies; author photos and emoji images are never loaded).

**Technical constraints.**

- Python / FastAPI, server-rendered HTML. No JavaScript framework and no build
  step.
- `yt-dlp` is deliberately left unpinned; pinning it guarantees it goes stale when
  YouTube changes its player.
- The test suite needs no database and no network.

**Open decision — UI architecture.** Markup and the page script currently live inside
`app/openfeed/main.py` as Python f-strings; the file is ~12,000 lines and serves ~25
HTML routes. CSS and the token layer have moved out to `app/openfeed/assets/css/`, and
shared UI behaviour to `app/openfeed/assets/js/ui-kit.js`, still with no build step.
How the rest grew was not chosen, and is **explicitly open**: design work may propose
extracting templates or the page script into separate files. Not yet decided, and not
to be treated as settled in either direction.

## Brand Commitments

**Binding:**

- The name **OpenFeed** and the logo at `assets/openfeed-logo.png`.
- The interface continues to track the redesign concept from Juxtopposed's
  ["I Redesigned the ENTIRE YouTube UI from Scratch"](https://youtu.be/OUM6XmhViN4),
  which the README credits as the inspiration. OpenFeed is an independent
  implementation, not affiliated with or endorsed by the video's author. Future
  visual work stays in conversation with that concept rather than diverging into
  an unrelated visual world.

**Current state, explicitly not binding** — recorded so later work knows these are
open to reconsideration rather than commitments:

- Dark-only. `:root` sets `color-scheme: dark` and there is no
  `prefers-color-scheme` handling anywhere in the codebase.
- The red accent `#e5484d` (with `--accent-soft: rgba(229,72,77,.12)`).
- The system font stack (`-apple-system`, `BlinkMacSystemFont`, `"SF Pro Text"`,
  `"Inter"`, `"Segoe UI"`, `Roboto`, `system-ui`).

## Evidence on Hand

- `README.md` — full feature inventory, install guide, and platform notes.
- `assets/openfeed-logo.png`; app icons at `app/openfeed/assets/favicon-32.png`
  and `apple-touch-icon.png`.
- `CONTRIBUTING.md`, `SECURITY.md`, `.env.example` (documents every supported
  setting).
- Offline ranking evaluation (`app/openfeed/evaluation.py`): leave one subscribed
  channel out, nDCG@20 and recall@100 per mode against a random baseline, served at
  `/api/recommendations/evaluate`. The time-holdout harness it replaced could not produce
  a non-zero number (watched videos are excluded from the feed by design).
- The ranker v2 pre-registration, deviations (D16 fixed what ships), experiment reports
  and sealed verification are in `docs/ranker-v2/`.
- `feed_impressions` rows (`model_version` `v2-graph` / `v2-blend`) record which
  recommendation slots were shown, for future evaluation against real exposure.
- A diagnostic endpoint that walks Invidious → player → manifest → media segment
  and names the first broken hop.
- A branch `redesign/ui-library-collections-artwork` exists with in-flight UI work.

**Absences future work must not fabricate:** there are no testimonials, no
customers, no user counts, no benchmarks against other front ends, no pricing, and
no hosted/SaaS offering. Poll results are simulated locally and must stay labelled
as such, because YouTube publishes no per-option breakdown.

## Product Principles

1. **The data stays on the machine.** Anything reaching a third party is off by
   default, read-only, and explained in Settings before it is enabled. The viewer's
   browser talks only to the OpenFeed server: a third-party player (the YouTube embed, a
   public Invidious instance) needs the admin to allow it, the user to opt in under
   Settings → Privacy, and a press on a button that names it, and is never fallen back to
   automatically. Where the admin has not allowed one, the pane says "Off on this server"
   and that an admin of this OpenFeed can allow it (admins get a link to Admin → Server).
   [`PRIVACY.md`](PRIVACY.md) is the statement of what goes where.
2. **Keep the personalisation, drop the surveillance.** The recommendation engine
   is the product; removing it to achieve privacy is a failure, not a trade-off.
3. **Ranking is inspectable and adjustable, not magic.** Signals are exposed with
   real measured coverage, every recommended card carries the reason it was chosen
   (on hover and as "Why this video" in its menu, so phones see it too),
   the mode is a user setting, and changes are validated against the evaluation
   harness rather than asserted.
4. **Fail loudly and name the cause.** Upstream sources break constantly; the
   product's job is to fall back cleanly and say exactly which hop failed rather
   than showing an empty player.
5. **Never invent what the source does not publish.** Where real data is
   unavailable, the substitute is labelled as a substitute.

## Accessibility & Inclusion

No formal standard has been established for this project — recorded as an open
decision rather than a requirement.

Current state: ~114 `aria-label` attributes and 9 `prefers-reduced-motion` guards
are present across the interface; the codebase has no `prefers-color-scheme`
handling, so the dark theme is unconditional regardless of system preference.
