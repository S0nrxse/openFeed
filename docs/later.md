# Later changes

Ideas and findings worked out but not built yet. Each entry says what is fact (checked in
the code) and what is a proposal. Newest first.

## Sign-in phase 2: what is left (2026-11-09)

**Done (signin2 track, 2026-11-09):** passkeys (add, name, rename, remove; usernameless,
after a username, and browser autofill; UV counts as both factors; passkey-only accounts;
"Confirm it's you" with a passkey), any number of OIDC providers (PKCE, state, nonce,
signed ID token against JWKS, per-provider admin and required groups, linking, invite and
registration rules), and the trusted-header method for forward-auth proxies.
docs/accounts.md has the details. Proposals not built:

- **Sign up with a passkey only.** Today a passkey-only account is made by adding a passkey
  and then removing the password. Open sign-up and invite links could offer "Create with a
  passkey" (username + passkey, no password).
- **Passkeys in the Android app.** The Capacitor WebView has no working WebAuthn. Options:
  androidx.webkit's `WebSettingsCompat.setWebAuthenticationSupport` (needs a recent
  WebView and Digital Asset Links on the server, `/.well-known/assetlinks.json` naming the
  app's signing key), or a native Credential Manager plugin. iOS needs associated domains.
- **OIDC in the app through the system browser.** Today the provider page loads in the
  WebView only if its host is listed under the app's Sign-in hosts; otherwise it opens the
  browser and the session lands there. A deep link back (`openfeed-app://auth?...`) with a
  one-time code exchanged by the app would fix that.
- **Passkey origins for more than one address.** RP ID and origin come from
  `OPENFEED_PUBLIC_URL` only. An install reached at several names would need a list.
- **Admin view of methods.** The Users page shows each account's methods by name; it could
  also list passkey counts and offer "remove this person's passkeys" for lost devices (today:
  `reset-password --link` gives them a way back in, then they remove old passkeys).
- **Back-channel logout** from OIDC providers (end sessions when the provider signs someone
  out) is not implemented; sessions expire on their own idle/absolute timers.

## Accounts QA follow-ups (2026-11-09) — DONE

Fixed after the five-tester QA round on the merged build (accounts mode and sign-in off):
passkey options are generous per address (page loads no longer lock a household out; strict
limits stay on verify, per account and per client; a limited autofill request is silent);
pages drawn as a POST answer move the address bar to their GET address (recovery codes, admin
one-time links, refused forms) without ever putting a secret in a URL; recovery codes in two
aligned monospace columns; a wrong current password re-draws Account & security with the
error at the field; shared playlists show the owner's profile or account name, never a user
key, and `/api/playlists/<id>/items` reads what the page shows (read-only) while every write
to someone else's playlist is 404; the 403 page talks about IdP groups only for IdP
failures; with sign-in off the sign-in steps go to `next` or Home, and an unset
`OPENFEED_PUBLIC_URL` no longer redirects sign-in to the default address; the Privacy pane
says who can allow what is off; notices after "Keep it in my account", "Start fresh" and a
set-password link; COOP and CORP `same-origin`; Devices rows show when they signed in, a
session tag and "This device" first. Not done:

- **The shared-playlist empty state** still says "Choose Save to playlist… Browse
  subscriptions" to someone who cannot add to it; it should read as a viewer's empty state.
- **A strict CSP** (no `'unsafe-inline'` in `script-src`/`style-src`) needs the app shell's
  inline scripts and styles moved to files or nonced; the new account scripts are files
  already (`assets/auth/replace-url.js`).

## Shared catalog and per-account recommendations (2026-11-09) — DONE

Done (catalog track): `video_provenance` (user_key, video_id, source, first_seen; one row per
person and video, in `profiles.USER_KEY_TABLES`) records search results shown, videos opened,
watched, rated, saved and downloaded (database triggers on the history tables), remote
playlists and channel pages opened, Takeout and discovery runs a person started.
`openfeed_video_scope(user_key)` (schema.sql) is a person's part of the catalog: provenance,
followed channels and feeds, and videos discovered from those (one link); `catalog_claims`
gives the single person who built a catalog that catalog when a second person arrives. Scoped:
ranker v2 (the walk runs on the person's subgraph, candidates and percentiles are cut to it,
the cache key includes it), the classic ranker, every Home shelf and the topic chips, Shorts,
Up next, library search (`db.search_videos`, after its UNION), the stored channels in channel
search, later search pages' "already shown" check, `/api/videos` and `/api/channels` for
admins too. Single person (sign-in off, no profiles): unscoped, unchanged. Upgrade backfills
everyone's history, progress, likes, playlists, feedback, downloads and Takeout once. Clearing
all watch history forgets search, channel and watch provenance. Code: `catalog_scope.py`;
tests: `tests/test_catalog_scope.py`; rules: PRIVACY.md "Shared catalog", README "Shared
catalog".

Left as is: library search's substring branch (titles, channels, authors, tags with ILIKE)
still scans the table, about 0.5 s on 84k videos before and after this change; a trigram
index would fix it. An install that already had several accounts before this upgrade cannot
tell who built its catalog, so nobody claims it: each account starts from its own history
and subscriptions.

Before:

Fact: videos, channels and related-video links are one catalog for the whole install (a
cache of public YouTube data, like Invidious'). Rows never record who caused them, and the
member-facing listings (`/api/videos`, `/api/channels`, Downloads, the bell) are scoped to
the caller. But the ranker's candidate pool is the whole catalog, and search results and
discovery runs add to it, so a video one account searched for can be ranked into another
account's Home when it relates to their own history. Nobody can tell where it came from,
but on a small household install it is a weak signal.

Proposal: record which account brought a video into the catalog (search, discovery seed,
Takeout, playlist import) in a small `(user_key, video_id)` table, and limit recommendation
candidates to the caller's subscriptions, their own provenance rows and the trending/
discovery runs the server does for everyone. Also: library-side search results
(`db.search_videos`) could be scoped the same way.

## Install-wide settings, isolation, quotas and rate limits (2026-11-09) — DONE

Done (isolation track): Admin → Server for everything install-wide (players, server switches,
downloads, quotas, rate limits, Takeout ceiling, probes, maintenance), 403 for members on every
install-wide route, per-account rate limits on the expensive routes, per-member download rights
and storage quota with identical files stored once, a two-account isolation test suite
(`tests/test_isolation.py`), and `feed_entries.is_read` / `is_starred` dropped (below). Holes
found and fixed: `/feeds/recommended.xml` served the shared `local` user's recommendations to
everyone; `/api/videos` and `/api/channels` listed the whole catalog (everyone's subscriptions)
to members; Downloads showed the server's total download size and free disk; `/api/videos/{id}`
returned the server path of a download; an admin's API token kept admin rights after the admin
was demoted; members could run syncs, enrichment, discovery, vector rebuilds, RYD batches,
evaluations and playback probes, and put a source into cooldown for everyone from their player;
a member's feed refresh grew the library and rebuilt vectors for the whole install; Settings
showed members the Services and Status panes and the server's sign-in configuration; Liked
videos mixed in the old reader's install-wide stars; type-ahead suggestions were cached across
accounts.

## Per-user subscriptions: what is still shared (2026-11-09)

**Update (isolation track):** done. `is_read` and `is_starred` are dropped at startup
(`migrations._drop_shared_read_state`, after the stars-to-likes step); the catch-up shelf
uses each user's own watch progress, and Liked videos no longer reads `feed_entries`.

Facts (before):
- `feed_entries.is_read` / `is_starred` are per feed entry, not per person: they came from
  the old reader, nothing sets them any more, and new entries arrive unread. The upgrade
  turned existing stars into likes for every existing user and cleared them, so Liked
  videos is per person. `is_read` still nudges the "catch up" shelf (prefers entries marked
  unread) the same way for everyone.
- A subscriber's title (a page's `data-title`, an OPML outline) only names a feed or
  channel row that does not exist yet; the poller renames the shared feed after the
  channel's own name on each fetch.

Proposal: drop `is_read` / `is_starred` (and the "unread first" rule) or move read state
into a per-user table if a "mark as read" feature is ever wanted.

## Faster new uploads: WebSub or adaptive polling (2026-11-09)

**Today (fact):** `rss.py` polls each followed feed's YouTube RSS feed about hourly,
straight from the server, once per feed however many people follow it (feeds nobody
follows are not polled). `feeds` already has `etag` and `last_modified` columns.

**Option A, WebSub (proposal):** YouTube publishes channel feeds to Google's hub
(`pubsubhubbub.appspot.com`).
- Subscribe once per channel with `hub.topic` set to the channel's feed URL, a
  `hub.callback` URL on OpenFeed, a lease and a `hub.secret`.
- The hub verifies with a GET carrying `hub.challenge`, which OpenFeed echoes back.
- Afterwards it POSTs an Atom entry to the callback on uploads and title or
  description edits. Deletions arrive as `at:deleted-entry`.
- Leases expire after a few days, so a timer must renew them. Check Google's current
  default before relying on a number.
- New uploads show up in minutes instead of up to an hour.

Rules if built:
- Verify `X-Hub-Signature` (HMAC with `hub.secret`) on every POST.
- Treat a notification as a hint only: re-fetch the channel feed, or the video through
  Invidious, and store that. A forged POST then can't inject anything.
- Only one callback path. Accept only topics we subscribed to, answer verification only
  for pending subscriptions, and rate-limit it.
- Keep polling as a backstop (every few hours), because delivery isn't guaranteed.
- On noahbox, the callback needs a Cloudflare Access bypass for that one path, which
  makes it public.

Privacy: no worse than polling. Google already learns the install's channel list and the
server's IP from the polls. WebSub adds a stable callback URL and an inbound public
endpoint. Neither exposes individual users, only the install's combined list.

**Option B, adaptive polling (proposal, no public endpoint):**
- Poll often-uploading channels every 10–15 min and quiet ones every 6–12 h, from each
  channel's upload history.
- Send `If-None-Match` / `If-Modified-Since` so unchanged feeds cost a 304.
- Gets most of the speed with zero inbound exposure, at the cost of more requests to YouTube.

Priority: next, now that the old feed reader is gone and subscriptions are per user.

## Retire the old feed reader (2026-11-09) — DONE in the repo, deploy pending

Done (feeds track, 2026-11-09):
- No trace of the old reader is left in code, SQL, tests or docs, except the startup
  migration (`app/openfeed/migrations.py`) and the one-line upgrade note in README
  (Upgrading: feeds and per-user subscriptions), which also names what to remove from an
  old deployment.
- Tables are `feeds` and `feed_entries`, renamed at startup in one transaction with data,
  indexes, constraints and foreign keys kept; a second start is a no-op. Checked on a copy
  of the dev data and in `tests/test_feeds_migration.py` (PostgreSQL, opt-in).
- The subscribe endpoint is `POST`/`DELETE /api/subscriptions`. No alias for the old path
  was kept (the old name would have stayed in the tree).
- The production-only transcript fetch (`GET /api/transcript/{id}/fetch`) is ported as
  `transcripts.py`: same contract, the configured Invidious URL instead of a hard-coded
  host, and the caption-file fallback now goes through the player's signed companion route
  and WebVTT reader (captions.py), which works while YouTube rate-limits Invidious' direct
  caption fetches (seen on the dev Invidious: transcript API 500, direct caption file a
  Google "Sorry" page, companion file fine).

Still to do (deploy, proposal):
1. The rest of the production-source diff (`/opt/NOAH/UserApps/Media/OpenFeed` against the
   repo); only the transcript module and its endpoint were ported here.
2. NOAH's `myyt_subscribe` / `myyt_unsubscribe` actions call the old subscribe path; point
   them at `POST`/`DELETE /api/subscriptions` (same JSON body) in the same change as the
   deploy. `myyt_subscriptions` (`GET /api/subscriptions`) keeps working and now lists the
   token owner's own subscriptions. The `noah-openfeed` skill says "at most three requests"
   for a transcript fetch; it is now at most four (track list, transcript, embed page,
   caption file).
3. Deploy, then remove the old containers and env vars after backing up their volume.

## Many users and pluggable sign-in (2026-11-09)

**Status (2026-11-09): phase 1 built** (docs/accounts.md). Done: `accounts` with roles and a
disabled flag, `account_identities` (one account, several methods), Argon2id passwords, TOTP
with single-use codes and recovery codes, server-side sessions with a device list and sign
out everywhere, CSRF and re-authentication, Redis rate limits keyed by a hash of the client,
the `accounts` mode beside `disabled` and `authentik_oidc` (Authentik users keep their
user_keys and are signed in across the upgrade), first-run /setup with a logged setup code,
"keep the data from before sign-in", closed / invite / open registration, the admin Users
page, Account & security, and `python -m openfeed.tools.accounts`. Admin rights: role in the
database, plus IdP admin groups for OIDC sessions.
Phase 2 methods (passkeys, several OIDC providers, trusted header), per-user subscriptions,
admin-only install-wide settings (Admin → Server) and per-user download quotas are DONE
(2026-11-09). The plan as written before the build follows.


Facts:
- Sign-in is `OPENFEED_AUTH_MODE=disabled` or `authentik_oidc`. That mode is generic OIDC
  with one provider.
- `users` is keyed by `authentik_sub`. About 16 tables key their rows by `user_key`, and
  profiles (`local:p:*`) sit under each account.
- Admin and access rights come only from identity-provider groups
  (`OPENFEED_REQUIRED_GROUP`, `OPENFEED_ADMIN_GROUPS`).
- Sessions are stateless signed cookies, so one session can't be revoked.
- Subscriptions are per user since 2026-11-09 (`user_subscriptions`, keyed by `user_key`,
  so per profile); this was the main blocker.

Proposal (hybrid):
- **Accounts:** `users` (id, `user_key`, role, disabled) plus `user_identities` (method
  `password` / `passkey` / `oidc:<name>`, subject), so one account can link several methods.
- **Credentials:** Argon2 passwords (`argon2-cffi`), passkeys (`webauthn`), TOTP
  (`pyotp`) with recovery codes.
- **Sessions:** stored server-side, giving a device list and "sign out everywhere".
- **Methods:** each is a small begin/finish module. Login runs first factor, then 2FA when
  enrolled. A passkey counts as both factors.
- **Providers:** several OIDC providers (Authentik, Pocket ID, Authelia, Keycloak).
  Optionally a trusted-header method for a forward-auth proxy, accepted only from the
  proxy's IPs.
- **Setup:** a first-run "create admin" step, invite links (open sign-up off) and login
  rate limiting in Redis.
- **Existing data:** `user_key` stays the join key. The first admin takes over the `local`
  data, and `authentik_sub` users map straight across.
- **Needed in any case:**
  - per-user subscriptions (shared `feeds` and `feed_entries`, plus `user_subscriptions`) —
    DONE 2026-11-09, with OPML import and export per user;
  - install-wide settings made admin-only, with roles in the database — DONE 2026-11-09
    (Admin → Server);
  - a decision on per-user download quotas and visibility — DONE 2026-11-09 (each account
    sees only its own downloads; admin-set quota and download right for members).
- **Risks to check:**
  - Passkeys need HTTPS on a fixed domain.
  - In the Capacitor app, passkeys probably need a native plugin on Android and associated
    domains on iOS.
  - OIDC in the app needs the system browser plus a deep link.

## Privacy gaps compared with Invidious (2026-11-09)

**Done (2026-11-09, privacy track).** Everything below is fixed; [`PRIVACY.md`](../PRIVACY.md)
is now the statement of what goes where. In short: the preconnect and dns-prefetch hints are
gone; the YouTube embed and public instances are off by default, need an admin switch plus a
per-user opt-in under Settings → Privacy, are only ever offered as a "Play from …" button, and
widen `frame-src` by exactly their origin on that one response; every page has a strict CSP
(`img-src`/`media-src 'self' data: blob:`), `Referrer-Policy: same-origin` and a
Permissions-Policy; image URLs from the database or Invidious are proxied or dropped
(`privacy.same_origin_image`); access logs carry no client address or query string;
`OPENFEED_OUTBOUND_PROXY` covers every server-side call to YouTube and third parties; `/docs`
no longer loads Swagger UI from a CDN. Checked by `tests/test_privacy.py` and the browser audit
`app/openfeed/tools/privacy_audit.py` (zero foreign requests on every page type, desktop and
phone). Still open:

- RSS polling stays on youtube.com: Invidious' `/feed/channel/` only has relative publish
  times, so switching would lose precision. It can go through `OPENFEED_OUTBOUND_PROXY`.
- The direct-download fallback fetches a googlevideo URL that Invidious signed for its own
  address, so it cannot use the outbound proxy.
- CSRF: a POST with `Origin: null` is still accepted by `harden_http`. `Referrer-Policy:
  same-origin` keeps a real Origin on OpenFeed's own POSTs, so rejecting `null` for
  cookie-authenticated writes is now possible.

The original findings:

Browser-side leaks: things that send a viewer's browser, and so their IP, to Google or a
third party. Plain Invidious doesn't do these.

- `_asset_preconnect()` puts `<link rel="preconnect" href="https://i.ytimg.com">` on
  every page. Thumbnails are proxied through `/vi/`, so this hint is useless, and it opens
  a connection from the viewer's browser to Google on every page. Remove it.
- The watch page adds preconnects to `youtube-nocookie.com` and `i.ytimg.com` and a
  dns-prefetch for `googlevideo.com` whenever the YouTube fallback is enabled. That is the
  default (`OPENFEED_PLAYBACK_FALLBACK=youtube`), even though CSP `frame-src 'self'` blocks
  the frame anyway. Remove them, or emit them only when the fallback is actually allowed
  and about to be used.
- Decide the YouTube fallback default: off, or ask the first time. When it plays, the
  viewer's browser talks to Google directly.
- Public Invidious instances as a playback source send the viewer's browser to that
  instance's operator. This is opt-in (`OPENFEED_INVIDIOUS_FALLBACKS`), so it only needs a
  clear label.
- CSP allows `img-src *` and `media-src *`. The app's own images are proxied, but any image
  URL that slips through (post or description content) would load from its origin. Tighten
  this to `'self' data: blob:` once nothing needs more.

Server-side contacts: the viewer's IP isn't exposed, but the install's activity is.

- Same as Invidious (it does these too, from the same host):
  - YouTube RSS polling (`rss.py`);
  - yt-dlp extraction and downloads;
  - oEmbed enrichment (`enrichment.py`);
  - captions and live chat.
- Beyond Invidious (third parties see which videos the install looks at):
  - Return YouTube Dislike: the full video id goes to `returnyoutubedislikeapi.com`.
    It's per-user opt-in on top of a server switch.
  - SponsorBlock and DeArrow: only a 4-character SHA-256 prefix of the id goes to
    `sponsor.ajay.app`, so it's k-anonymous. Both are per-user opt-in.
