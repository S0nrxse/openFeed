# Privacy

What OpenFeed sends where, and what it keeps. The rule this follows: **your browser never
contacts Google or any other third party unless you turned on a feature that says so in plain
words**, and the server shares and keeps as little as the features need. OpenFeed aims to be at
least as private as plain Invidious.

Each user can see their own state under **Settings → Privacy**.

The rules are enforced in code (`app/openfeed/privacy.py`, `app/openfeed/outbound.py`) and
checked by tests (`app/openfeed/tests/test_privacy.py`) and by a browser audit
(`app/openfeed/tools/privacy_audit.py`, see [Checking it yourself](#checking-it-yourself)).

## What your browser loads

Every page, the player and the embedded Invidious player load only from the OpenFeed server
itself:

- **Thumbnails** go through `/vi/` and **avatars, banners and post images** through `/ggpht/`,
  both proxied by your own Invidious. Image URLs that point anywhere else are dropped before
  the page is built, and a placeholder is drawn instead.
- **Video, audio, captions, storyboards and DASH manifests** come through `/embed/`,
  `/companion/`, `/api/v1/…` and `/api/ytdlp/stream/`, all on the same origin. The server
  fetches them, so YouTube's media servers see the server, not you.
- **Scripts, stylesheets and icons** are served by OpenFeed. No web fonts, no CDN, no
  analytics, no Gravatar, no social widgets. The `/docs` API reference is drawn on the server
  instead of loading Swagger UI from a CDN.
- **No connection hints.** Pages carry no `preconnect`, `dns-prefetch` or `prefetch` to other
  hosts.

Every response carries headers that make the browser enforce this:

| Header | Value | Why |
| --- | --- | --- |
| `Content-Security-Policy` | `default-src 'self'`; `img-src 'self' data: blob:`; `media-src 'self' blob:`; `connect-src 'self'`; `font-src 'self'`; `frame-src 'self'`; `worker-src 'self'`; `manifest-src 'self'`; `script-src`/`style-src 'self' 'unsafe-inline'`; `object-src 'none'`; `base-uri 'none'`; `form-action 'self'`; `frame-ancestors 'self'` | Anything that slips through is blocked by the browser. `/embed/` also allows `data:` fonts and `blob:` workers, which video.js needs. `'unsafe-inline'` lets OpenFeed's own inline code run; it does not allow loading anything from elsewhere. |
| `Referrer-Policy` | `same-origin` | Other sites never learn which OpenFeed page a link came from. Same-origin requests keep a real `Origin` header, which the server's CSRF check relies on (`no-referrer` would turn it into `Origin: null`). |
| `Permissions-Policy` | camera, microphone, geolocation, payment, USB, Topics (`browsing-topics`), FLoC (`interest-cohort`), FLEDGE and the other ad-tech features off; autoplay, fullscreen, picture-in-picture and encrypted media for this origin only; passkeys (`publickey-credentials-get`, `-create`) for this origin only, never for a frame | Turns off what OpenFeed does not use. |
| `X-Content-Type-Options` | `nosniff` | |
| `X-Frame-Options` | `SAMEORIGIN` (`DENY` on `/api/`) | |
| `Cross-Origin-Opener-Policy` | `same-origin` | A page from another site that opens OpenFeed (or that OpenFeed opens) gets no handle on its window. |
| `Cross-Origin-Resource-Policy` | `same-origin` | Other sites cannot embed OpenFeed's thumbnails, video, downloads or JSON as their own subresources. OpenFeed's own pages, the `/embed/` player and its Range requests are unaffected. |

Links you click to leave (Open on YouTube, links in descriptions, a channel's website) are the
exception: they open the other site because you asked. They carry `rel="noreferrer noopener"`,
YouTube's `youtube.com/redirect?q=` wrapper is removed from description links, and click-tracking
parameters (`utm_*`, `fbclid`, `gclid`, YouTube's `si` and `pp`, …) are stripped. The same goes
for a "Sign in with <provider>" button: it takes you to that identity provider's own page
because you pressed it. Nothing on the sign-in page loads from a provider before you do.

## Players from other sites (off unless you turn them on)

When OpenFeed cannot play a video privately (your Invidious and yt-dlp both fail), it shows a
failure card. Two kinds of third-party player can be offered there, and **nothing ever switches
to them automatically**:

| Player | Who sees you | What they see |
| --- | --- | --- |
| **YouTube player** (`www.youtube-nocookie.com` embed) | Google | Your IP address, your browser, the video, and this site's address (the embed refuses to play without it, so it alone gets the origin, never the page path) |
| **Public Invidious instances** | Whoever runs that instance | Your IP address, your browser and the video |

Each needs three things before anything loads:

1. **The admin allows it** (Admin → Server, or in `.env`): `OPENFEED_PLAYBACK_FALLBACK=youtube` for the YouTube player;
   `OPENFEED_INVIDIOUS_FALLBACKS=` (instances the admin offers) and/or
   `OPENFEED_ALLOW_PUBLIC_INSTANCES=true` (users may add their own) for public instances.
   Both are off by default.
2. **You turn it on** under Settings → Privacy, next to the warning "Plays from YouTube: Google
   sees your IP address and what you watch." (or the public-instance equivalent).
3. **You press the button** ("Play from YouTube", "Play from yewtu.be") on a video that failed.

Until you opt in, the page's Content-Security-Policy refuses those frames. Once you do, the
player page (and only the player page) allows exactly that origin in `frame-src`, per response.

## What the server sends, and to whom

Your IP address, cookies and account never leave the server in these requests. What the other
side learns is that *this server* looked something up.

**Always, like Invidious** (Invidious makes the same requests from the same host):

| To | What | When |
| --- | --- | --- |
| Your Invidious (and companion) | Searches, video and channel data, comments, captions, thumbnails, video streams | Whenever a page needs them. This is your own server. |
| `www.youtube.com` | Channel RSS feeds (`/feeds/videos.xml?channel_id=`) | The background sync, about hourly. YouTube learns the install's combined channel list, not who follows what. |
| `www.youtube.com` | oEmbed (`/oembed?url=…watch?v=`) | Only when your Invidious cannot describe a video. |
| YouTube via yt-dlp | Video page, stream URLs, subtitles, live chat | yt-dlp playback when Invidious fails, downloads, captions Invidious lacks, live chat when you turned it on. |

**Only if you turn them on** (Settings → Privacy or Integrations; the admin can turn each off for
everyone):

| To | What it sees | When |
| --- | --- | --- |
| SponsorBlock (`sponsor.ajay.app`) | The first 4 hex characters of the SHA-256 of a video ID, a bucket shared by many videos | When you open a video |
| DeArrow (`sponsor.ajay.app`) | The same 4-character hash prefix, per video on the page. Thumbnails are cut on this server from your Invidious stream, never fetched from DeArrow by video ID | When a page with videos loads |
| Return YouTube Dislike (`returnyoutubedislikeapi.com`) | Full video IDs, mixed into batches of other library videos so the one you are watching is hidden among them. Your own likes are never sent | Background sync, and when you open a video with no estimate yet |
| Public Invidious instances (server-side) | Only the playback probe under Settings → Playback → Test all sources | When you press Test |

**Outbound proxy.** Set `OPENFEED_OUTBOUND_PROXY` to an `http://`, `https://`, `socks5://` or
`socks5h://` URL (for Tor, `socks5h://127.0.0.1:9050`) and every request above that goes to
YouTube or a third party (RSS, oEmbed, yt-dlp, captions, live chat, the yt-dlp stream proxy,
RYD, SponsorBlock, DeArrow, public-instance probes) leaves through it. Requests to your own
Invidious do not; give Invidious its own proxy if you want its traffic hidden too. The one
exception is the direct-download button's fallback, which fetches a stream URL your Invidious
signed for its own address and so has to come from the same network as Invidious.

**Never:** no telemetry, analytics, crash reporting, update checks, Gravatar or other avatar
services, outbound email, or web fonts.

## What the server keeps

- **Your library**: subscriptions, watch history and resume points, likes, playlists,
  collections, feed preferences, downloads and Takeout imports, keyed to your account. You can
  export it (Settings → Account) and delete it.
- **No IP addresses and no user agents** are stored in the database.
- **Searches are not stored.** Recent searches stay in your browser (`localStorage`). The
  server keeps type-ahead suggestions in memory for 10 minutes, keyed by the typed prefix and
  the account. A Takeout import counts your search history but does not store it. What is
  kept is which stored videos a search *showed* you (not the words you typed), as part of
  the shared catalog's provenance below, so that those videos can be offered to you and to
  nobody else.
- **Logs.** The access log records method, path and status, with no client address and no
  query string, so searches never reach it. Error logs give the length of a search, not its
  text. Set `OPENFEED_LOG_CLIENT_DETAILS=true` only while debugging to put both back.
- **Sign-in.** The account row records when you were last seen, which session handling needs.
  Sessions keep a hash of the cookie, a coarse device label ("Firefox on Android"), how you
  signed in and the groups your identity provider reported; no IP address.
- **Passkeys.** For each passkey the server keeps its credential id, its **public** key, a
  signature counter, a random per-account user handle (not your account id), the transports
  your browser reported (such as "internal" or "usb"), the authenticator model id (AAGUID)
  and whether it is a synced passkey, plus the name you gave it and when it was added and
  last used. The private key never leaves your device, so a copy of the database cannot sign
  in as you. OpenFeed asks for no attestation, so your browser does not send a certificate
  naming the device maker. The challenge for each sign-in is kept on the server for at most
  five minutes and deleted when used.
- **Identity providers (OIDC).** When you sign in with a provider, it learns that you signed
  in to this OpenFeed (the server's address and client id, the time, and whatever it logs
  about your browser at its own sign-in page, which you open yourself). It does not learn
  what you watch. OpenFeed asks for the scopes the admin configured (by default `openid email
  profile`) and keeps only the provider's subject id for you, linked to your account, plus a
  username and display name for a new account and the groups it reported for the session.
  It does not store the email address or tokens. The server fetches the provider's discovery
  document and signing keys (cached for an hour) and makes the token and userinfo requests;
  each provider sees only those requests from the server.
- **Forward-auth proxy.** With a trusted header configured, the proxy in front of OpenFeed
  already sees every request; OpenFeed stores only the name it sends, as your linked identity.
- **What an admin sees about you** (Admin → Users and Server): your username, display name,
  role, sign-in methods, whether 2FA is on, how many devices are signed in, when the account
  was made and last active, and how many bytes of downloads you keep on the server (for
  quotas). Not your history, likes, playlists, subscriptions, searches, downloads' titles or
  tokens. Rate-limit counters are keyed by a keyed hash of your account and expire with
  their window.
- **Shared catalog.** Video and channel details and related-video links are cached once
  for the whole server, like Invidious' cache, and those rows never record who looked
  something up. Type-ahead suggestions are cached per account, so one person's typing never
  warms another's. See [Shared catalog](#shared-catalog) for what this means between
  accounts.
- **Caches** of third-party answers (SponsorBlock segments, DeArrow titles, RYD counts) are keyed
  by video, not by user.

## Shared catalog

One install keeps one catalog of public YouTube data: a video somebody searched for, opened
or imported is stored once, for everyone. What one account does must never show up in
another's experience, so the server also keeps, per account (and per household profile), a
small list of *why* that person may be shown a stored video, and every page that reads the
catalog for a person keeps to it.

**Kept per person** (`video_provenance`: your `user_key`, the video, the first reason and
when): videos a search showed you; videos you opened, watched, rated, saved to a playlist or
downloaded; videos listed on remote playlists and channel pages you opened; videos in your
Takeout history; videos a discovery run seeded from your own history (or started by you)
stored. Videos from the channels and feeds you follow, and videos discovered as related to
any of those (one link), are yours without a row. On an install that one person used alone
before a second arrived, that person also keeps the catalog as it stood then
(`catalog_claims`, one id range).

This list is yours like your history: it is in your data export (`catalog_reasons`), it is
deleted with your profile or account, it moves with "Keep the data from before sign-in", no
other account (admins included) is shown it, and **Clear all watch history** also forgets the
videos searches, channel pages and watching added. Removing a single video from history
forgets that you watched it.

**What another account can see of yours: nothing.** Recommendations (all three modes; the
related-video walk itself runs over your part only, so another person's video is not even
a bridge between two of yours), every Home shelf and its topic chips, Shorts, the watch
page's Up next, library search results, the stored channels a channel search lists first,
`/api/videos` and `/api/channels` read only your part. A video another account searched for
is "From YouTube" to you when YouTube returns it for your own search, exactly as if nobody
here had seen it.

**Still shared, on purpose:**

- **Channel pages** list the channel's stored videos to anyone who opens them, and a
  channel's row in channel search says how many are stored. That is the channel's own
  public upload list, which the page fetches from Invidious anyway.
- **Opening a video by its address** works for anyone: the watch page, like Invidious',
  serves any public video.
- **Background work** (enrichment, related-video discovery, the RSS poller, trending) reads
  the whole catalog. What it finds reaches a person only through the rules above.
- **Playlists someone made public**, which they chose to share.

**One person, no scoping.** With sign-in off and no household profiles there is nobody to
keep apart, and every page reads the whole catalog as before. Adding a profile or turning
sign-in on switches scoping on for good.

## Checking it yourself

With the server running and no third-party features on:

```bash
cd app
python -m openfeed.tools.privacy_audit --base http://127.0.0.1:8060 --out /tmp/privacy-audit --bypass-csp --screenshots
```

It opens every page type (Home, Subscriptions, Shorts, search, channel tabs and posts, library
pages, Settings, the player, the Invidious embed, error pages, and the sign-in, setup, sign-up,
two-step, "Confirm it's you", invite, reset, Account & security and Users pages) on a desktop
and a phone viewport,
records every request the browser makes, including from iframes and workers, and lists any that
go to another origin. `--bypass-csp` repeats the run with the policy switched off, to show what
the markup itself would load. The report is in `report.txt`; exit status 1 means something left.
Pass `--cookie openfeed_session=<value>` (from a signed-in browser) to audit the pages signed
in, and `--only account,admin-users,...` to pick pages.

`python -m pytest openfeed/tests/test_privacy.py` renders the same page types from hostile test
data and fails on any attribute or stylesheet that points elsewhere.
