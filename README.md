<p align="center">
  <img src="assets/openfeed-logo.png" width="120" alt="OpenFeed">
</p>

<h1 align="center">OpenFeed</h1>

OpenFeed is a privacy-focused self-hosted YouTube front end. It builds a personalised feed from your own
subscriptions and watch history, plays videos through a local Invidious instance, and
keeps every piece of your watch data on your computer.

Your browser only ever talks to your OpenFeed server: thumbnails, avatars, video and captions
are all proxied, and nothing reaches Google or anyone else unless you turn on a feature that
says so. [`PRIVACY.md`](PRIVACY.md) lists exactly what goes where; Settings → Privacy shows
it per user.

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
- Settings → Account → Your data: one JSON file with subscriptions and collections,
  playlists, Watch later, liked videos, watch history with progress, feedback and settings
  for the current profile (`GET /api/export`), the profile's subscriptions as OPML
  (`GET /api/export/subscriptions.opml`), and **Import OPML**, which subscribes the current
  profile to every feed in an OPML file from another feed reader
  (`POST /api/import/subscriptions.opml`, multipart field `file`; at most 500 feeds and
  2 MB, files declaring a DOCTYPE are refused, and the new feeds are fetched in the
  background)
- Subscriptions are per person: each account and each household profile has its own list.
  Feeds are shared and fetched once however many people follow them, and a feed nobody
  follows any more stops being polled (its videos stay). `GET /api/subscriptions` lists
  the caller's subscriptions; `POST /api/subscriptions` and `DELETE /api/subscriptions`
  (JSON `{youtube_channel_id, feed_url, title}`) subscribe and unsubscribe the caller only
- Search type-ahead: suggestions drop under the search box as you type (from Invidious'
  `/api/v1/search/suggestions`, cached ten minutes per prefix), with your own matching
  recent searches first. Arrow keys, Enter, mouse and touch pick one; Escape closes the list.
  Focusing the empty box lists your recent searches, each with a remove button
  (Shift+Delete removes the highlighted one). Recents stay in this browser and are the same
  list the phone search screen shows
- Search filters for upload date, length, sort (relevance, upload date, view count, and
  rating where Return YouTube Dislike is on), result type (videos, channels with subscribe
  buttons, playlists, Shorts) and features (live, 4K, HD, subtitles, Creative Commons, 360°,
  VR180, 3D, HDR). Every filter is in the URL, so a filtered search can be linked. YouTube
  ignores its own upload-date sort, so Upload date asks for the best matches from today,
  this week, this month and this year side by side and lists them newest first, library
  matches included. Rating is likes against RYD's dislike estimate and is only offered
  while RYD is on, because nothing else gives a real rating
- Search shows "In your playlists" first when a match is saved in one of your playlists
  (Watch later included), naming the playlist. Playlist, Watch later and Liked pages have a
  filter box for titles and channels
  Library matches follow the same filters, except features: the library does not know
  which videos are 4K or captioned, so a feature filter shows YouTube results only, and the
  page says the library was left out. A type chip keeps only the filters its type uses
  (Shorts keeps sort and date; Channels and Playlists keep none), and Clear filters shows
  whenever any filter is in the URL
- Library search matches titles, channels, authors and tags as substrings, and descriptions
  as a phrase of whole words through a Postgres full-text index (`idx_videos_text_fts`).
  The app builds that index `CONCURRENTLY` on a background thread at first start (about a
  minute on an 82k-video library, and reads and writes go on meanwhile); until it is valid,
  descriptions are matched with ILIKE, which costs about 3 s per search. A search page on
  the dev library went from about 4.5 s to 1.4–1.9 s. On a shared install the library side
  is your part of the shared catalog only (see [Shared catalog](#shared-catalog)): a video
  someone else searched for is "From YouTube" to you, never one of your library matches
- Cards on channel pages and in search show your watched check, progress bar and Mark
  watched / Mark unwatched state, like everywhere else
- Channel pages with tabs, filters, links, community posts and search within the channel.
  A channel's back catalogue is paged from Invidious by continuation token and cached for
  ten minutes, so scrolling costs one upstream request per new page and reopening a channel
  costs none
- A channel's playlists open inside OpenFeed (`/playlist?list=<YouTube id>`, read from
  Invidious page by page, nothing stored as a playlist) with Play all, Shuffle and Save to
  library, which copies the playlist into one of your own
- A channel's Live tab lists its past and current streams, on-air ones first with a LIVE
  badge. It shows only for channels whose tab list includes streams
- Channels page sorts: Recently subscribed, Most relevant (your recent watching, halving
  every 30 days) and New activity (unwatched uploads from the last 30 days), beside Name,
  Latest upload and Most videos. Select puts the list in select mode with checkboxes and
  Select all, then Unsubscribe (asks first), Add to collection, Notifications all or none,
  and Don't recommend for every selected channel
- The notification bell also works for subscribed feeds that are not one YouTube channel
  (a playlist feed), keyed per feed. A feed that never delivers YouTube videos (a GitHub
  releases feed) gets no bell and says "No YouTube videos in this feed": OpenFeed keeps only videos
- Play all and Shuffle on playlists and Liked videos: the watch page opens in list mode
  (`/watch/<id>?list=<playlist id | liked | watch-later>`, plus `&shuffle=<seed>` for a
  shuffled run), lists the queue above Up next with the current video marked and "N of M",
  and starts the next item when the video ends. Headphone and lock-screen Next follow it too
- Direct YouTube RSS polling for subscriptions, one fetch per followed feed
- History's side panel has "Clear all watch history" and "Pause watch history", per profile.
  While paused nothing is recorded from watching: no History entry, no resume point, no watch
  time, so no new recommendation signal (Like and Mark watched still work). A banner says so
- Select on History, Liked videos and playlists (Watch later included): checkboxes, Select
  all, Remove (held for an Undo toast) and Add to playlist. Esc leaves select mode
- Sort: History by newest or oldest watched, Liked by recently or oldest liked, and a
  playlist by YouTube's options (Manual, date added, date published, most popular, title,
  duration). The owner's playlist sort is saved and Play all follows it
- Reorder a playlist by dragging a row's handle, with arrow keys on the handle, or with
  Move to top / bottom / up / down in the row's menu. The order is saved
- Playlist visibility, YouTube's three worded for one install: Private (only this profile),
  Unlisted (anyone signed in to this OpenFeed with the link) and Public (also listed under
  "Public on this OpenFeed" on every profile's Playlists page). Others see it read-only
- Import a YouTube playlist by link (Playlists → Import from YouTube, or paste the link into
  New playlist): read through Invidious page by page, up to 5,000 videos, in YouTube's order
- Deleting a playlist or a collection offers Undo; a snapshot is kept for a day
- Downloads whose file is gone say "File missing" and offer Download again at the same quality
- Accounts for a household or a public instance: username and password (Argon2id), optional
  two-step verification with recovery codes, server-side sessions with a device list, invite
  links or open sign-up, and an admin Users page. No email address is asked for or stored.
  Passkeys (usernameless, with browser autofill; they count as both steps), any number of
  OIDC providers (Authentik, Pocket ID, Authelia, Keycloak, Zitadel, Google, ...) and a
  forward-auth proxy header, all linkable to one account. Authentik (OIDC) sign-in with
  group-based access still works unchanged, alone or alongside
- Household profiles under one login ("Who's watching"), with an optional kids flag
- Responsive desktop and mobile interface
- Appearance: Dark (the default), Light, or Use device theme, from "Appearance" in the
  account menu or Settings → Appearance. Saved per profile, mirrored in the browser so the
  page paints in the right theme from the first frame, and applied at once in every open
  tab. Videos, thumbnail badges and the player stay dark in both themes, as on YouTube

### Feed and recommendations

- Recommendations ranked over the whole library (your part of it on a shared install, see
  [Shared catalog](#shared-catalog)), not just the newest uploads: YouTube's
  own related-video lists link candidates to the channels you follow and to videos you
  watched at least halfway or liked (see [Ranking](#ranking))
- Three ranking modes in Settings: Graph (the default: what you watched, new uploads from related creators, and your channels' world, mixed slot by slot), Graph + popularity and Classic
- A one-line reason on every recommended card, such as "Because you watched …" or "New · Similar to …".
  Hover the metadata line, or open the card's ⋮ menu (the sheet on a phone): "Why this video" heads it
- Candidate acquisition from varied channels, trending and topic seeds — the feed can
  surface channels you have never subscribed to
- Feed customisation with per-signal controls and real measured coverage. How far to
  wander, Prioritize subscriptions and followed or muted topics act on the default Graph
  mode too
- Watch-page Up next from the video's own related list (YouTube's up next), then videos two
  links away, with a couple of slots for the creator's latest uploads
- Watch later in one tap: a clock on a card's thumbnail on hover (Save to Watch later in the
  card menu on a phone) toggles the video in the "Watch later" playlist, made on first use,
  with an Undo toast
- More like this and Less like this offer Undo, which removes the recorded feedback.
  The Not interested page (`/not-interested`, linked from History and Settings → Content)
  lists videos marked Not interested or Less like this and channels you chose Don't
  recommend for, each removable with Undo
- Pull down on Home or Subscriptions on a touch screen to reload the feed in place. Tapping
  the tab you are on scrolls to the top; on desktop a Back to top button appears on long pages
- "Don't recommend channel" in every video card's menu: mutes the creator with an Undo
  toast, hides their cards on the page, and lists them under Muted creators in Feed
  controls, where each has an Unmute button
- Catch-up shelves on Home ("While You Were Away", "Back to the Channels You Watch Most"
  and the rest) rotate to a different shelf each time one appears and move on through
  their videos, so infinite scroll does not show the same catch-up videos again
- "Hide this shelf" on Home's horizontal shelves (Continue watching, New from your
  subscriptions and the catch-up shelves) is stored per profile, offers Undo, and lists the
  shelf under Hidden shelves in Feed controls, where Show brings it back
- Google Takeout import for watch history *and* subscriptions, with the library
  backfilled from the channels it finds. Imported watches also seed recommendations, weighted
  below anything watched in OpenFeed
- Optional "Help tune recommendations" (off by default): an on-server A/B comparison of
  Graph against Graph + popularity by team-draft interleaving, with a readout on Status
- Offline leave-one-channel-out evaluation (nDCG@20, recall@100, random baseline)

### Watching

- Watch page with theater mode, resume position, chapters and a YouTube-style action row
- Keyboard shortcuts that work wherever focus is on the watch page or in the player (not in a
  text field): `k` or Space play/pause, `j`/`l` 10s, arrows 5s and volume, `m` mute, `f`
  fullscreen, `0`-`9` jump, `<`/`>` speed, `c` captions, `t` theater, Shift+N the first Up
  next video. Space, arrows and digits on the page only apply when nothing else has focus
- "Resuming from 18:58" sits under the player with Start over and a close button
- Autoplay: when a video ends, an "Up next in 8" card over the player names the first Up
  next video, with Cancel and Play now. The switch is on the player bar and in Settings →
  Playback, saved per profile. A Play all / Shuffle list plays its own next item first,
  and Shorts are never autoplayed or autoplayed into
- Share opens a dialog with the link, Copy, an OpenFeed or YouTube link, and "Start at
  m:ss" from the player's clock. `/watch/<id>?t=` honours `90`, `90s`, `1m30s` and `1:30`
- Settings → Playback → Player: Autoplay, default speed (the player's speed menu still
  changes one video), Always show captions and caption language
- Mark watched and Mark unwatched in every video menu, following the video's state.
  Unwatching clears the finished and marked flags but keeps the real resume position
- A start that buffers but never plays (no error, spinner at 0:00) is caught after 12 seconds
  of asked-for playback and offers the next source in the fallback ladder, or the failure card
  when there is none
- Threaded comments with timestamps, creator hearts, and highlighted creator replies
- Shorts shelf and dedicated player (↑/↓ or `j` to move, Space or `k` to play or pause, `m`
  mute, `c` comments). Settings → Content → Show Shorts off hides them
  everywhere: the shelf, `/shorts` (which says Shorts are off and links to the setting),
  `/api/shorts` and a channel's Shorts tab
- Each Short has a Subscribe button beside the channel name, and "Not interested" in its ⋮
  menu (and on the Home Shorts shelf cards' ⋮). It records the same feedback as on long
  videos, removes the Short and offers Undo
- Comments are one setting (Settings → Content → Show comments); the Shorts panel's button
  turns them on everywhere and says so. When Invidious cannot read a video's top comments
  (it fails on Shorts with `Missing hash key: "commentRenderer"`), the newest are shown
  instead with a line saying so
- Keyboard shortcuts: `?` lists the keys the current page answers to (`/` search, `t`
  theater mode, the player's own keys on the watch page, the Shorts keys). Menus and dialogs
  keep Tab inside while open, and Escape hands focus back to the control that opened them
- Captions and subtitles in every player OpenFeed draws, with a language menu, `c` to toggle,
  automatic captions labelled as such, and the last choice remembered. See
  [Captions](#captions)
- Lock-screen, notification and headphone controls through the Media Session API: title,
  channel and artwork, play and pause, seeking, and previous/next. See
  [Lock screen and headphones](#lock-screen-and-headphones)
- Play on a TV through the browser's own Remote Playback (Chrome cast, Safari AirPlay),
  without Google's Cast SDK. See [Casting to a TV](#casting-to-a-tv)
- Downloads at full quality via yt-dlp and FFmpeg, with a resolution picker and
  progress tracking

### Playback reliability

- Invidious as the primary backend, with a configurable fallback ladder and per-source
  health tracking. The automatic ladder is private sources only (your Invidious, yt-dlp,
  downloaded copies). The YouTube embed and public Invidious instances are off unless the
  admin allows them and the user opts in under Settings → Privacy, and even then they are
  offered only as a "Play from …" button on the failure card. See [`PRIVACY.md`](PRIVACY.md)
- yt-dlp as a fallback media backend for streams Invidious cannot produce
- A diagnostic endpoint that walks Invidious → player → manifest → media segment and
  names the first broken hop

### Optional integrations

Each is **off by default**, read-only, server-side, and contains a privacy note in
Settings. Nothing is enabled without you asking for it -- making you, the user aware of the consequences of 3rd party software.

- **SponsorBlock** — segment skipping with timeline markers and per-category actions.
  Looked up by a 4-character hash prefix, so the SponsorBlock server never learns which
  video you asked about.
- **DeArrow** — community titles and thumbnails in place of clickbait, from the
  SponsorBlock team. Settings → Integrations. Looked up by the same 4-character hash
  prefix and cached per prefix. Every replaced title keeps the original one press away
  ("Show original title" in a card's menu, a button under the watch-page title), and an
  option applies DeArrow's lowercase title format. DeArrow publishes a thumbnail as a
  timestamp: OpenFeed never asks DeArrow's image service, which takes a video ID. It cuts
  the frame itself with ffmpeg from the stream your Invidious plays, on one background
  thread, caches it (`OPENFEED_DEARROW_CACHE_DIR`, default `/tmp/openfeed-dearrow`), and
  shows it once it exists. DeArrow's random-frame fallback for videos with no submitted
  thumbnail is not implemented. `OPENFEED_DEARROW_ENABLED=false` turns it off for everyone.
- **Return YouTube Dislike** — dislike counts. Your own likes and dislikes stay local and
  are never submitted.
- **Community posts and polls** — read-only. Poll voting is simulated locally and
  labelled as such, because YouTube publishes no per-option breakdown.
- **Live chat** — a read-only chat panel on live streams and premieres, and the chat
  replay of past streams in step with the video. See [Live chat](#live-chat).

### Captions

The CC button in the player lists every caption track the video has: manual subtitles in
each language, and YouTube's automatic captions marked "Auto-generated". `c` turns captions
on and off, from the player or from anywhere on the watch page. The choice (on or off,
language, manual or automatic) is saved in your playback preferences and picked again on
the next video when it has a matching track.

The browser fetches captions from OpenFeed only (`/api/captions/{id}` and
`/api/captions/{id}/{track}.vtt`). OpenFeed lists tracks through Invidious
(`/api/v1/captions`) and reads them through Invidious companion, falling back to the URLs
yt-dlp reports when Invidious lists none. Automatic captions arrive in YouTube's karaoke
format, where every line is sent twice; OpenFeed converts them to plain WebVTT that shows
each line once. Captions are drawn by OpenFeed in its own type and colours in every
same-origin player (the Invidious embed, the yt-dlp stream and downloaded copies). The
opt-in YouTube player is cross-origin and keeps YouTube's own captions.

`GET /api/transcript/{id}/fetch?language=en&kind=prefer_manual|manual|auto&list_only=false`
returns a video's captions as timed text for scripts and assistants, with no stored row
needed and nothing stored. It reads Invidious' transcript API from the configured
`INVIDIOUS_BASE_URL` only; when that fails it reads the same track's caption file the way
the player does (the signed companion URL in Invidious' embed page) and parses it with the
player's WebVTT reader. The answer always says what happened: `ok`, `status` (`fetched`,
`tracks` or `unavailable`), and on failure a `code` such as `no_captions`,
`caption_track_unavailable` or `caption_provider_rate_limited`, with the `stage` that
failed. A requested language is matched exactly and `manual` never returns automatic
captions. Limits: four requests per video, no redirects, 2 MB per answer, 5,000 segments,
250,000 characters, 25 seconds. Successful answers are cached for 30 minutes
(`cached: true`). A malformed id, language or kind is a 422 `invalid_caption_request`.

### Lock screen and headphones

On a phone or a desktop browser with media controls, the lock screen and the notification
show the title, channel and thumbnail (served by OpenFeed's own `/vi/` proxy). Play, pause,
seek back and forward, and scrubbing all work there and from headphone buttons. Next opens
the first video in Up next (respecting the This channel filter). Previous restarts the
video, or goes back to the video you came from if this one has only just started. OpenFeed
has no queue or autoplay yet, so Up next is what next means.

This needs the same-origin player: the opt-in YouTube player cannot be reached. Verified in
Chromium; the iOS and Android lock screens themselves have not been tested on a device.

### Casting to a TV

The cast button uses the browser's built-in Remote Playback API (`video.remote.prompt()`)
and, on Safari, `webkitShowPlaybackTargetPicker`. OpenFeed never loads Google's Cast SDK, so
the page makes no request to Google to cast. The button only appears when the browser
reports a device and the current source can be handed over:

| Browser | Receivers | Notes |
| --- | --- | --- |
| Chrome and Edge on Android | Chromecast and Google TV | The TV fetches the video URL itself, so it must be able to reach your OpenFeed address, and with login on it has no session |
| Chrome and Edge on desktop (121 and later) | Chromecast and Google TV | Chrome sends the media from the browser (media remoting), so the TV does not need to reach OpenFeed |
| Safari on macOS, iPhone and iPad | AirPlay: Apple TV and AirPlay TVs | The receiver fetches the URL itself, with the same reachability caveat |
| Firefox | None | No Remote Playback support, so no button |

Only plain video URLs can be cast: the yt-dlp direct stream and downloaded copies. The
Invidious player streams DASH through Media Source Extensions, which no browser can hand to
a receiver, so on that source the button stays hidden. Use Settings → Playback to put
yt-dlp first, or download the video, when you want to cast. Casting has been checked only
to the point of the button's visibility in headless Chromium; no real receiver was tested.

### Live chat

Off by default. Turn it on in Settings → Integrations → Live chat. Invidious has no chat
API, so OpenFeed reads chat through yt-dlp, on the server, and the browser only talks to
OpenFeed:

- **Live streams and premieres:** one yt-dlp process per video follows the chat while a
  watch page is polling it, and stops within a minute of the last poll (and after four
  hours, and with the server). At most three are followed at once.
- **Past streams:** the whole replay is downloaded once and kept in memory, and the panel
  shows what had been said by the moment on screen. The first part is shown while the rest
  is still downloading.

What is sent: the video's watch URL, from your server, without cookies or an account. When
the setting is on, opening any video asks yt-dlp whether it has chat. Author photos, custom
emoji images and stickers are not loaded. Nothing is ever posted. Chat needs yt-dlp
(`OPENFEED_YTDLP_ENABLED`, on by default in the image). A premiere's waiting-room chat
before it starts cannot be read.

## Ranking

The Recommended shelves (the `recommended` and `all` lanes), the deeper Home pages (the
"Recently uploaded in your interests" and "Long-form picks" grids), the Home Shorts shelf and
the watch page's Up next use ranker v2, `app/openfeed/ranker.py`. The other lanes (Continue
watching, subscriptions, and the topic, "Recently Uploaded" and "Long-Form" filter chips)
use the original ranker unchanged.

**What it does.** Each video's stored related list (Invidious `recommendedVideos`, up to 12
ids) is a set of *contexts*. A walk spreads weight from seed videos to every video that
shares a related id with them. It takes two steps, the second at half weight, and a related
id listed by many videos counts for less. There are two walks:

- **The channels walk** starts from your subscribed channels, with equal weight per channel,
  spread over its videos.
- **The watched walk** starts from your graded events:

  | Event | Weight |
  | --- | --- |
  | Watched 50–90% | 0.5 |
  | Watched 90% or more, or finished with real watch time | 1.0 |
  | Like or more-like-this | 1.0 |
  | Watched under 50%, or page opened only | nothing |

  Each weight is multiplied by 2^(−age/60 days), floored at 0.25, so old watches count less.

  **Imported Google Takeout history** (the `takeout_watched_videos` table) adds weak
  positives to this walk: the 500 most recent imported watches found in the library, each
  0.1 × 2^(−age/180 days), floored at 0.01. Even the newest imported watch weighs less than
  the weakest in-app one (0.5 × 0.25), the whole import counts as at most five full watches
  (in seed mass and in the watched share), and a video you rated or hid here keeps that
  rating. Every imported watch is treated as watched and is not recommended again.

**Graph mode (the default) fills the shelf from three sources, slot by slot:**

| Source | Share of slots | What it holds |
| --- | --- | --- |
| Because you watched | n/(n+20), capped at 50% (n = decayed event count) | The watched walk, rotating across the videos you watched, so one video can't fill the shelf |
| New from related creators | 30% of the rest | Uploads up to 30 days old from channels the walks link most strongly (the sum of a channel's top-3 walk scores), rotating across creators |
| Your channels' world | the remainder | The channels walk, then a popularity prior (`log1p(views) − 1.5·log(age_days + 1)`) for videos no walk reaches |

- Every source is weighted by quality (0.5 + 0.5 × the video's views-for-its-age
  percentile).
- **Rotation.** A video shown on an earlier day in the last week is scaled down by 0.7 per
  such day, up to 3 days. Each visit-day therefore brings new picks, while the pages of one
  day stay consistent.

| Mode | Score |
| --- | --- |
| Graph (default) | The three-source mix above |
| Graph + popularity | 0.25 × min-max(walk) + 0.75 × min-max(prior), the sealed RC2 |
| Classic | The original topic and watch-time scorer over the newest 2,000 videos |

The default is the single constant `ranker.DEFAULT_MODE`. Choosing *Automatic* in
Settings → Content → Recommendations, or in the feed controls dialog, follows it.

**Always applied.** The Recommended shelves never show any of these:
- watched, opened, hidden, not-interested, disliked or less-like-this videos
- muted creators
- live streams, or Shorts of 90 s or less
- dead videos: fewer than 100 views and older than 7 days
- channels you follow (they have their own shelves)

If the v2 ranker can't serve, the Classic fallback is filtered by the same rules.

A creator's score is halved only after two or more different videos of theirs are hidden or
disliked. The first 50 slots are then picked greedily for variety:
0.7 × position × 0.8^(picks already from that creator) − 0.3 × topic similarity to the
picks so far.

**Feed controls in Graph mode.** The feed controls dialog (and Settings) act on the slot
mix. The defaults reproduce the measured mix exactly.

| Control | Effect in Graph |
| --- | --- |
| How far to wander (`discovery_level`, default 35%) | The "new from related creators" share of the non-watched slots runs 15% → 30% (default) → 40%. Above the default, up to 5% of slots also go to popular-and-recent videos the walks never reach |
| Prioritize subscriptions (default on) | Off moves a fifth of the channels share to the creators source |
| More of (followed topics) | × 1.25 inside every source when all of the topic's words are in the video's topic terms; it never moves a video between sources |
| Less of (muted topics) | Matching videos are left out, like muted creators (Classic gives them a heavy penalty) |

The watched share and its cap are never changed by a control. Bounds were chosen so each
control alone keeps plain LOCO within 10% of the default (dev library of 81,926 videos, 45
channels; the default equals the code before this change on the same library):

| Setting | Plain LOCO nDCG@20 | Age-matched |
| --- | --- | --- |
| Default (35%, prioritised) | 0.1077 | 0.2164 |
| How far to wander 0% | 0.1066 | 0.2209 |
| How far to wander 100% | 0.0992 (−8%) | 0.1833 |
| Subscriptions not prioritised | 0.1000 (−7%) | 0.1973 |
| Both at their exploring end | 0.0957 (−11%) | 0.1909 |

LOCO rewards closeness to the channels you follow, so a control whose purpose is to wander
from them is bound to cost there; the default is untouched. Topic controls and imported
history do not enter LOCO (it seeds subscriptions only).

**Deeper Home pages and Shorts.**
- Page 1 onward reads three v2 lanes in turn: `recommended` (ranks 60 onward),
  `recommended-recent` (published in the last 14 days) and `recommended-long` (20 minutes or
  more). All use the slot mix, the controls and the Recommended exclusions, including
  subscribed channels. They draw on one pool, so each lane is served without the rows an
  earlier Home page already took (`ranker.home_partition` follows the page schedule).
- The Shorts shelf ranks Shorts the walks reach by walk × quality × rotation × topics, then
  the rest newest first, which is what the shelf showed before. Live streams, watched,
  hidden, muted and dead Shorts are left out.
- If v2 cannot serve, the Shorts shelf falls back to newest first and the deeper pages to
  Classic with the Recommended lane rules.

**Up next on the watch page.** Three tiers, each ordered by quality × topics × (position
weight + the anchor's own walk) with the user's walk as a small tie-break:
1. the video's stored related list, in YouTube's order (position weight 1.0 → 0.5);
2. videos a walk from that one video reaches (they share related-list entries with it);
3. the user's own walk, then quality.

At most two per channel, except the video's own creator, who keeps YouTube's same-channel
follow-ups and gets their newest uploads in slots 3 and 6 (up to five in all). Exclusions are
the Recommended ones (watched, hidden, muted creators and topics, live, Shorts, dead) except
that subscribed channels are allowed. Until the index is built, for a video not indexed yet,
and in Classic mode, the previous ordering serves.

**Help tune recommendations** (Settings → Content → Recommendations, off by default). The
online comparison [research-reranking.md](docs/ranker-v2/research-reranking.md) H9 asked for:
- The Recommended shelf becomes a team-draft interleave of Graph and Graph + popularity
  (Radlinski et al. 2008). At every slot the side with fewer picks picks next, a coin seeded by
  the user and the day breaks ties, and the pick is that side's best video not yet shown.
- Both sides rank the same eligible set, so excluded videos stay excluded. The draft is
  deterministic for a day, so one day's pages agree. Reasons use the same wording for both
  sides.
- Each shown slot's side goes into `feed_impressions.metadata` (`team`, `experiment`). Opening
  a watch page credits the most recent interleaved impression of that video in the last 24
  hours, once (`clicked_at`).
- Status → Personalization shows interleaved impressions per side, opens per side, and the
  share of decided user-days that Graph + popularity won (more opened videos that day), with
  a Wilson 95% interval. Equal days are ties and are left out of the share.
- Nothing leaves the server. It applies in the Graph and Graph + popularity modes.

**Inspectable.** Each card names its reason, on the metadata line's hover title and as
"Why this video" at the top of the card's ⋮ menu (so it is readable on a phone):
- "Because you watched “…”"
- "Because you follow …"
- "New · Similar to …"
- "Popular and recent"

The feed controls dialog shows how often each signal fired. Status → Personalization shows
the mode in use and the library index (videos, related-list contexts, age). Each shown
Recommended slot, including the deeper Home pages, is logged to `feed_impressions`
(`model_version` `v2-graph`, `v2-blend`, or `v2-tdi` while tuning is on), which also feeds
rotation.

**Runtime.**
- **First build.** The index is built from Postgres on a background thread at startup. On an
  82k-video library that takes about 42 s the very first time, because the related lists sit
  inside about 17 KB of metadata per video.
- **Cache.** The related lists are then cached to disk (`OPENFEED_RANKER_CACHE`, by default
  the temp directory), so a restart rebuilds in about 1.5 s.
- **Refresh.** The index is rebuilt incrementally every 15 minutes, or when the video count
  or highest id changes.
- **Fallback.** Until the first build finishes, or if numpy is missing or anything fails, the
  filtered Classic ranker serves and the reason is logged once.
- **Caching per user.** Each user's ranking is cached for the day, until the index or that
  user's events, feedback, progress, subscriptions or preferences change.

**What was measured.** The full record is in [docs/ranker-v2](docs/ranker-v2/README.md): the
pre-registration, the deviations, the experiment reports and the sealed verification.

- **Coverage.** The walk scores all 81,847 videos of the dev library. Classic scored the
  newest 2,000.
- **Speed.**
  - A Home page served by v2 takes 0.12–0.14 s in total. Ranking one user cold takes about
    90 ms p95 (about 130 ms with a 3,000-video Takeout import).
  - Classic took 1.58 s for both Recommended calls.
  - The recent and long-form pages took 0.93–1.09 s on the Classic lanes and take
    0.12–0.16 s on v2 (median of 5 warm loads, pages 2, 3, 6, 7). Page 0 still takes about
    0.8 s, most of it the Continue watching shelf, which stays on the Classic ranker.
- **Sealed test.** Each candidate was read once, by a verifier who did not tune it. nDCG@20:

  | | Own library, 20 held-out channels | KuaiRec, 423 users, 10 events |
  | --- | --- | --- |
  | Classic | 0.004 (random 0.004) | 0.043 |
  | Graph | 0.116 | 0.307 |
  | Graph + popularity | 0.062 | 0.356 |
  | Strongest simple baseline (graph_walk) | 0.041 | 0.325 |

  Graph is the default, selected by a rule written before the sealed read (D16). Graph +
  popularity beat the strongest baseline significantly on KuaiRec (+9.6% at 10 events), but
  as the verifier read the guardrails it failed two of them. On OpenFeed's own library the
  graph alone came first in every look:

  | Look at the own library | Graph | Graph + popularity |
  | --- | --- | --- |
  | DEV | 0.082 | 0.069 |
  | Sealed | 0.116 | 0.062 |
  | Live leave-one-channel-out, 45 channels | 0.086 | 0.025 (Classic 0.005) |

  None of those own-library differences is significant, because that testbed is too small
  to detect even large changes.
- **Topic similarity.** TF-IDF topic similarity added nothing on either testbed, and v2 does
  not use it for scoring.
- **Feel audit** ([feel-preregistration.md](docs/ranker-v2/feel-preregistration.md)). The
  sealed test above was age-matched, so it could not see freshness or what the feed is like
  to scroll. A second pass looked at the real profile's feed. Its targets and selection rule
  were written down first, and an independent code audit followed.
  - **Blind judges** were given the profile and two feeds; 20 judged each pair.

    | Pair | Final preferred | Wilson 95% |
    | --- | --- | --- |
    | Final vs the pre-audit graph mode | 19/20 | [0.76, 0.99] |
    | Final vs the intermediate mix | 14/20 | [0.48, 0.85] |

  - **Relevance (LOCO):**

    | LOCO | Final | Pre-audit |
    | --- | --- | --- |
    | Plain | 0.108 | 0.092 |
    | Age-matched | 0.217 | 0.216 |

  - **The real top 24:**

    | Measure | Final | Pre-audit |
    | --- | --- | --- |
    | Distinct channels | 24 | 23 |
    | Under 1k views | 0% | 4% |
    | Uploaded in the last 30 days | 6 | 0 |

  - **Not met:** the age target (median 21 days or less). The channels source reaches popular
    evergreen videos, and making it fresher cost relevance every way it was tried.
- **Untested on OpenFeed's own signals.** The event grading and decay, the creator demotion,
  the variety step and rotation have not been measured against real use. Impressions are now
  logged so they can be.
- **Parity.** The walk matches the reference implementation to 1e-16.

To rerun the evaluation: `GET /api/recommendations/evaluate?min_videos=5&max_channels=0&classic=true`,
or `python -m openfeed.evaluation --user <key>` inside the app container.

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

Authentication is disabled by default: everyone is the one `local` user, which suits one
person on a trusted network. For anything else pick a sign-in mode (`OPENFEED_AUTH_MODE`):

- `accounts`: built-in accounts (see [Accounts and sign-in](#accounts-and-sign-in)).
- `authentik_oidc`: sign in through Authentik or another OIDC provider (or several). Configure
  the `OPENFEED_OIDC_*` values and set `OPENFEED_REQUIRE_LOGIN=true`. Access and administrator
  groups are optional comma-separated values in `OPENFEED_REQUIRED_GROUP` and
  `OPENFEED_ADMIN_GROUPS`. Existing deployments keep working unchanged; their users keep
  their data and stay signed in across the upgrade.

Persistent files are stored under `OPENFEED_DATA_DIR`, which defaults to `./data`.

### Accounts and sign-in

Set `OPENFEED_AUTH_MODE=accounts` and a real `OPENFEED_SECRET_KEY`, then open the app. The
first visit goes to **/setup**, which asks for a one-time setup code printed in the server
log (`docker compose logs openfeed-app | grep "setup code"`) and creates the first admin.
Right after, OpenFeed offers to **keep the data from before sign-in**: everything the `local`
user had (history, likes, playlists, downloads, preferences, household profiles) moves into
the admin's account in one step, or you can start fresh.

- **People:** Admin → **Users** (`/admin/users`, next to Admin → Server and Status) lists every account
  with its role, 2FA, sign-in methods, created and last active. Admins add accounts (the new
  person gets a one-time link to choose a password), make invite links (single use, 1/7/30
  days), change roles, disable and enable, make a password reset link, turn off someone's 2FA,
  sign them out everywhere, or delete an account with all of its data and downloads.
- **Registration:** closed (admins add people), invite links (default) or open sign-up for a
  public instance (rate-limited per client and per hour, no captcha, no email).
- **Account & security** (`/account`, Settings → Account & security): display name, password,
  two-step verification with an authenticator app (QR drawn on the server) and ten one-time
  recovery codes, linked sign-in methods, every signed-in device (when it signed in, "This
  device" first) with "Sign out" and "Sign out everywhere else", and deleting your own account. Sensitive changes ask for the password again
  if it was last confirmed more than 10 minutes ago.
- **Locked out?** Use a recovery code, ask an admin for a reset link, or on the server run
  `docker compose exec openfeed-app python -m openfeed.tools.accounts reset-password USERNAME`
  (also `list`, `create-admin`, `make-admin`, `reset-2fa`, `enable`, `disable`, `sign-out`).
- **Safety:** sessions live on the server (only a hash of the cookie is stored, no IP address),
  end on sign-out, password change, disabling or deletion, and expire after 7 idle days or 30
  days. The cookie is HttpOnly and SameSite=Lax, Secure over https. Every state-changing
  request with a session must come from this site (Origin check). Sign-in attempts are
  rate-limited per username and per client in Redis, keyed by a hash of the address. Wrong
  password, unknown user and disabled account all get the same message. Behind a reverse
  proxy, set `OPENFEED_TRUSTED_PROXIES` so limits apply per client.
- **Passkeys** (on by default in accounts mode when `OPENFEED_PUBLIC_URL` is https on a domain
  name, or localhost): add, rename and remove them in Account & security, then use "Sign in
  with a passkey" (no username needed) or the browser's autofill. A passkey with fingerprint,
  face or PIN counts as both steps of two-step verification. An account can be passkey-only;
  nothing lets you remove your last way to sign in. Over plain http on a LAN IP, browsers
  refuse passkeys, and the page says so. The Android app's WebView can't use them yet.
- **OIDC providers:** the single `OPENFEED_OIDC_*` provider keeps working as before. Add more
  with `OPENFEED_OIDC_PROVIDERS=authentik,pocketid` and `OPENFEED_OIDC_<NAME>_ISSUER`,
  `_CLIENT_ID`, `_CLIENT_SECRET`, `_DISPLAY_NAME`, `_SCOPES`, `_ADMIN_GROUPS` (with Docker,
  in `signin.env` next to `compose.yml`). Register `<public URL>/auth/oidc/<name>/callback`
  at each provider. Each gets a button on the sign-in page and a "Link" in Account &
  security. PKCE, state and nonce, and a signed ID token checked against the provider's keys.
  A first sign-in creates an account only when registration is open or the person has an
  invite link; accounts are never matched by email address.
- **Forward-auth proxy:** `OPENFEED_TRUSTED_HEADER=Remote-User` (plus optional groups and name
  headers) signs people in when the request comes from an address in
  `OPENFEED_TRUSTED_PROXIES`; from anywhere else the header is stripped.

`docs/accounts.md` has the model, every setting, and how to add a sign-in method.

### Running a public instance

OpenFeed can host strangers the way an Invidious instance does. Each account's data is its
own: playlists, collections, history, likes, downloads, tokens, notifications and
preferences are only ever read and changed through the signed-in account's key, and a
playlist is visible to others only when its owner makes it unlisted or public.

**What to set** (`.env`):

- `OPENFEED_AUTH_MODE=accounts`, a real `OPENFEED_SECRET_KEY`, `OPENFEED_PUBLIC_URL=https://…`
  (cookies become Secure) and `OPENFEED_TRUSTED_PROXIES` for your reverse proxy.
- Registration under Admin → **Users**: invite links, or open sign-up.
- Admin → **Server** (`/admin/server`, "Admin" in the account menu) holds everything that
  changes the server for everyone, over the `.env` defaults: the YouTube player and public
  Invidious instances (off by default), the SponsorBlock, DeArrow and Return YouTube Dislike
  switches, whether members may download (`OPENFEED_MEMBERS_MAY_DOWNLOAD`) and a per-member
  storage quota (`OPENFEED_DOWNLOAD_QUOTA_MB`, 0 = none), the per-account rate limits
  (`OPENFEED_RATE_LIMIT_<NAME>`) and the largest Takeout upload (`OPENFEED_TAKEOUT_MAX_MB`),
  the playback probes ("Test all sources", "Diagnose") and maintenance runs. Changes apply at
  once, ask for "Confirm it's you" like other admin actions, and are also at
  `GET`/`PUT /api/admin/settings` for an admin's API token.
- **Rate limits** (per account, all its profiles and tokens together; admins exempt; off with
  sign-in off): searches 120 per 10 minutes, channel/playlist/comment/watch pages 600 per 10
  minutes, playlist imports 10 per hour, Takeout imports 5 per day, downloads 30 per hour,
  OPML imports 10 per hour. Past one, the page or API answers 429 with a sentence saying what
  ran out and when it comes back (and `Retry-After`).

**Members can** use everything personal: Settings → Playback (their own source order),
Content, Appearance, Integrations and Privacy opt-ins (only where the admin allowed the
service), Import, API tokens, Profiles and Account & security; download and keep videos when
allowed, within their quota (shown on Downloads). **Members can't** open Admin, the Status
page or Settings → Services/Status, run syncs, enrichment, discovery, vector rebuilds, RYD
batches or the playback probes (403), change install-wide switches, see the server's disk or
other people's downloads, list the whole video and channel catalog (`/api/videos` and
`/api/channels` answer from their own subscriptions; an admin's from their own part of the
catalog), or move the shared source-health state from their player.

**What the admin sees about members** is kept to what running the server needs: on Users,
each account's username, display name, role, sign-in methods, 2FA on/off, number of signed-in
devices, created and last active; on Server, the bytes of downloads each account keeps (for
quotas), not which videos. Admins do not see anyone's history, likes, playlists, searches,
subscriptions or tokens, and nothing stores IP addresses (see [`PRIVACY.md`](PRIVACY.md)).

### Shared catalog

The video and channel catalog (titles, thumbnails, related-video links) is a shared cache of
public YouTube data, like Invidious' cache. What one account searches for, opens or imports
is stored there for everyone, so every listing that reads the catalog for a person keeps to
what that person has a reason to see (`app/openfeed/catalog_scope.py`):

- videos from the channels and feeds they follow;
- videos they were shown or touched themselves: search results, videos they opened,
  watched, rated, saved or downloaded, remote playlists and channel pages they opened,
  their Takeout, discovery runs seeded from their own history (`video_provenance`, one row
  per person and video);
- videos discovered as related to those (one link: a related video of a related video is
  not theirs).

That covers recommendations (Graph, Graph + popularity and Classic; the related-video walk
itself runs only over the person's own part, so somebody else's video cannot even act as a
bridge), every Home shelf, Shorts, the watch page's Up next, library search, the stored
channels a channel search lists first, the Home topic chips, `/api/videos` and
`/api/channels`. Type-ahead suggestions come from YouTube through Invidious and never read
the library. A channel page still
lists that channel's stored videos: it is the channel's public upload list, fetched from
Invidious anyway. Background work (enrichment, related discovery, the RSS poller) reads the
whole catalog; what it finds reaches a person only through the rules above.

With sign-in off and no household profiles there is one person and nothing is scoped, so a
single-user install behaves exactly as before. The first time the install hosts a second
person (a profile is added, or sign-in is turned on), the person who had it to themselves
keeps the catalog as it stood (`catalog_claims`), and scoping applies to everyone from then
on. On upgrade, everyone's own history, progress, likes, playlists, feedback, downloads and
Takeout become provenance rows once. Clearing all watch history also forgets which videos
searches and channel pages showed you.

An install that already had several people when this arrived (for example `local` plus an
Authentik account that are really the same person) claims the catalog for nobody, because
nobody can tell who built it. Give it to its owner once so their Home stays as broad as
before: `docker compose exec openfeed-app python -m openfeed.tools.accounts claim-catalog USERNAME`.

### Privacy settings

All described in [`PRIVACY.md`](PRIVACY.md):

- `OPENFEED_PLAYBACK_FALLBACK=youtube` lets users opt in to the YouTube player (default `off`).
- `OPENFEED_INVIDIOUS_FALLBACKS` offers public Invidious instances to users;
  `OPENFEED_ALLOW_PUBLIC_INSTANCES=true` lets them add their own (default off).
- `OPENFEED_OUTBOUND_PROXY` sends every server-side request to YouTube and third parties
  through an `http://`, `https://` or `socks5h://` proxy (Tor, a VPN gateway).
- `OPENFEED_LOG_CLIENT_DETAILS=true` puts client addresses and query strings back into the
  logs, for debugging only.

Before a release, run the browser privacy audit against a running server:
`python -m openfeed.tools.privacy_audit --base http://127.0.0.1:8060 --bypass-csp` (from `app/`).
It must report zero foreign requests.

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
- Tokens belong to an account. Disabling the account stops its tokens at once; deleting it
  deletes them.
- With sign-in off (`OPENFEED_AUTH_MODE=disabled` and login not required) the
  local profile can create tokens in the same pane. The API also answers without one
  then, so a token there names the profile a script acts as and suits clients that ask
  for a key. Those tokens stop working once sign-in is turned on or required.
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

### Household Profiles

One account (a signed-in account, or the single `local` user when sign-in is off) can hold
up to six profiles, like Netflix's "Who's watching". Nothing to switch on: open the
account menu and choose **Add a profile**, or go to **Settings → Profiles**
(`/?settings=profiles`) to add, rename, recolour, switch and delete.

- **Separate per profile:** subscriptions, history, continue watching, likes and
  dislikes, hidden videos, playlists, collections, feed and playback preferences
  (including playback source order), notification levels, downloads, API tokens, Takeout
  imports, feed impressions and recommendations. A new profile starts with no
  subscriptions.
- **Shared by the household:** the video catalog and server settings, though each profile
  is only offered its own part of the catalog (see [Shared catalog](#shared-catalog)): a
  video one profile searched for never turns up in another's Home or search. Feeds are shared
  rows (`feeds`, `feed_entries`), so a channel two profiles follow is fetched once, but
  who follows what is `user_subscriptions` (`user_key`, `feed_id`), and subscribing or
  unsubscribing on one profile leaves the others alone. A collection can only hold the
  profile's own subscriptions; unsubscribing takes the channel out of that profile's
  collections.
- **How it works:** each profile has its own `user_key`, the key every per-user table
  already uses, so no existing table changed. The main profile's `user_key` is the
  account's own, so adding profiles moves no data. Profiles live in `account_profiles`,
  created by the idempotent migration in `schema.sql`.
- **Choosing:** with more than one profile, pages ask "Who's watching" (`/profiles`) until
  one is chosen. The choice is an HttpOnly, SameSite=Lax cookie (`openfeed_profile`),
  signed with the session secret and bound to the account when one is set. The server
  honours it only for a profile listed under the signed-in account; anything else acts as
  the main profile. API requests without a choice act as the main profile. Signing out
  clears the choice.
- **API tokens** act as the profile they were created under (a token minted on the main
  profile acts as the account). They ignore the profile cookie, cannot switch or manage
  profiles, and are deleted with their profile. `GET /api/` reports the `profile` a
  credential acts as.
- **Deleting** a profile deletes all of its data above in one transaction, then removes
  download files no other row references. The main profile cannot be deleted. Its
  subscriptions go with it; a feed nobody else follows then stops being polled.
- **Kids profiles** hide comments and Shorts everywhere whatever their own switches say
  (enforced server-side in the feed preferences, so the comments API refuses too), close
  the Shorts viewer, and will not open a video YouTube marks as age-restricted
  (Invidious `isFamilyFriendly: false`). YouTube's "made for kids" flag is not available
  through Invidious, so videos are not filtered by audience. Kids profiles can switch
  profiles but cannot add, change or delete them. There is no PIN: a kids profile is a
  filter, not a lock.

### Upgrading: feeds and per-user subscriptions

Startup (`db.init_db()`, `app/openfeed/migrations.py`) upgrades an existing database in
one transaction, under an advisory lock so the app and the worker cannot both run it:

- The feed tables get their current names, `feeds` and `feed_entries`, with their data,
  indexes, constraints and foreign keys kept. A table is renamed only when the old name
  exists and the new one does not; when both exist nothing is touched and a warning is
  logged.
- The first start that creates `user_subscriptions` gives every existing feed to every
  `user_key` that has data on the install (the `users` rows, every household profile, and
  any key found in the per-user tables; `local` when there is none), so nobody's feed
  empties. Old install-wide entry stars become likes for those same keys.
- Every later start is a no-op. A fresh install creates the new tables directly.

API clients that subscribed through the old path must use `POST /api/subscriptions` and
`DELETE /api/subscriptions` (same JSON body). `GET /api/subscriptions` keeps its response
shape and now lists the caller's own subscriptions.

Upgrade note: installs from before 2026-11 named the feed tables `freshrss_subscriptions` and `freshrss_entries` and the subscribe endpoint `/api/subscriptions/freshrss`; the rename is automatic, and an old deployment's `FRESHRSS_*` environment variables and `files-freshrss`/`openfeed-freshrss-db` containers are unused and can be removed after backing up their volume.

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

A few tests also run against PostgreSQL when `OPENFEED_TEST_DATABASE_URL` names a database
they may use: each creates and drops its own schema there (the startup migration on an old
install, two users' subscriptions kept apart, token and collection ownership). Never point
it at a database whose data matters.

Validate Compose configuration with:

```bash
./deploy.sh config --quiet
```

## Best security practices

- Do not expose PostgreSQL, Redis, or the optional Qdrant and Typesense services publicly.
- Keep `.env`, local Compose overrides, database files, and backups outside version control.
- Use unique random values for every password and signing key.
- Put public deployments behind TLS and turn sign-in on (`accounts` or `authentik_oidc`).
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
[SponsorBlock](https://sponsor.ajay.app/), [DeArrow](https://dearrow.ajay.app/) and
[Return YouTube Dislike](https://returnyoutubedislike.com/). It is not affiliated with
YouTube or Google.
