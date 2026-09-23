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
moves it onto the user's own hardware: a content-based ranker built from a TF-IDF
topic profile of what the user actually watched, with exponential recency decay,
watch time as a ranking signal rather than clicks alone, and candidate acquisition
that can surface channels the user has never subscribed to.

The ranking is measurable rather than asserted — an offline evaluation harness
reports recall@k, MRR, and nDCG for ranking changes.

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
- Authentication is **off** by default. Optional Authentik OIDC with group-based
  access control is available but not the common path — the common path is a
  single trusted user on a private network.
- Playback depends on third-party sources that break without warning; the stack
  carries a configurable fallback ladder and per-source health tracking because of
  it.

## Capabilities and Constraints

**Confirmed functionality.** Search, channels, playlists, collections, likes, and
watch history; channel pages with tabs, filters, links, and community posts;
direct YouTube RSS polling for subscriptions; Google Takeout import for watch history and
subscriptions with library backfill; feed customisation with per-signal controls
and real measured coverage; watch page with theater mode, resume position,
chapters, and an action row; threaded comments with timestamps and creator hearts;
a Shorts shelf and dedicated player; downloads at full quality via yt-dlp and
FFmpeg with a resolution picker and progress tracking.

**Optional integrations**, each off by default, read-only, server-side, and
carrying a privacy note in Settings: SponsorBlock (queried by 4-character hash
prefix so the server never learns which video was requested), Return YouTube
Dislike (the user's own votes stay local and are never submitted), and read-only
community posts and polls.

**Technical constraints.**

- Python / FastAPI, server-rendered HTML. No JavaScript framework and no build
  step.
- `yt-dlp` is deliberately left unpinned; pinning it guarantees it goes stale when
  YouTube changes its player.
- The test suite needs no database and no network.

**Open decision — UI architecture.** All markup and CSS currently live inside
`app/openfeed/main.py` as Python f-strings and style-returning functions
(`_home_styles()` and friends); the file is ~12,300 lines and serves ~25 HTML
routes. This grew that way rather than being chosen, and is **explicitly open**:
design work may propose extracting templates, CSS, or a token layer into separate
files. Not yet decided, and not to be treated as settled in either direction.

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
- Offline ranking evaluation harness (`app/openfeed/evaluation.py`) producing
  recall@k, MRR, and nDCG.
- A diagnostic endpoint that walks Invidious → player → manifest → media segment
  and names the first broken hop.
- A branch `redesign/ui-library-collections-artwork` exists with in-flight UI work.

**Absences future work must not fabricate:** there are no testimonials, no
customers, no user counts, no benchmarks against other front ends, no pricing, and
no hosted/SaaS offering. Poll results are simulated locally and must stay labelled
as such, because YouTube publishes no per-option breakdown.

## Product Principles

1. **The data stays on the machine.** Anything reaching a third party is off by
   default, read-only, and explained in Settings before it is enabled.
2. **Keep the personalisation, drop the surveillance.** The recommendation engine
   is the product; removing it to achieve privacy is a failure, not a trade-off.
3. **Ranking is inspectable and adjustable, not magic.** Signals are exposed with
   real measured coverage, and changes are validated against the evaluation
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
