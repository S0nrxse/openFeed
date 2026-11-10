"""Channel tools: remote playlists in the app, a channel's Live tab, the Channels page's
extra sorts and bulk actions, and the notification bell for feeds that are not YouTube
channels.

Everything here that talks to Invidious caches briefly in process, the same way the
channel upload pages do (discovery.fetch_channel_uploads): a playlist or a Live tab
costs one upstream request per page, and reopening it within the window costs none.
SQL lives here rather than in db.py so this feature merges as one module.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import db, discovery
from .config import get_settings

logger = logging.getLogger(__name__)

# --- remote playlists -------------------------------------------------------------------

# YouTube playlist ids: PL (user playlists), UU/UULF/UUSH (a channel's uploads), OLAK5uy_
# (albums), FL/LL (favourites, likes) and RD (mixes). Local playlists are UUIDs, and the
# other list keys are "liked" and "watch-later", none of which start with these.
REMOTE_PLAYLIST_RE = re.compile(r"^(PL|UU|OL|FL|LL|RD)[A-Za-z0-9_-]{8,64}$")

_PAGE_TTL_SECONDS = 600.0
_CACHE_MAX = 200
_playlist_pages: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_stream_pages: dict[tuple[str, str], tuple[float, list[dict[str, Any]], str]] = {}

# How many playlist pages one walk may request. Invidious pages overlap (page 2 starts at
# index 50, page 3 at 150), so each page past the first adds about 100 videos.
MAX_PLAYLIST_PAGES = 12


class RemotePlaylistError(RuntimeError):
    pass


def is_remote_playlist_id(value: Any) -> bool:
    return bool(REMOTE_PLAYLIST_RE.match(str(value or "")))


def _trim(cache: dict[Any, Any]) -> None:
    if len(cache) >= _CACHE_MAX:
        for stale in sorted(cache, key=lambda key: cache[key][0])[: _CACHE_MAX // 4]:
            cache.pop(stale, None)


def _invidious() -> tuple[str, float]:
    settings = get_settings()
    if not settings.invidious_base_url:
        raise discovery.DiscoveryError("Invidious is not configured")
    return str(settings.invidious_base_url).rstrip("/"), float(max(4, settings.enrichment_timeout_seconds))


def _cover_video_id(thumbnail_url: Any) -> str:
    match = re.search(r"/vi/([\w-]+)/", str(thumbnail_url or ""))
    return match.group(1) if match else ""


def normalize_playlist_page(payload: dict[str, Any]) -> dict[str, Any]:
    """One /api/v1/playlists/<id> page as the app's own shape.

    Videos keep Invidious's `index` (their position in the playlist), which is what
    de-duplicates the overlapping pages. Rows without a video id (deleted or private
    entries) are dropped, and so are blank ones: Invidious lists private and deleted
    videos with an empty title and author, which would otherwise be stored under their id.
    """
    videos = []
    for item in payload.get("videos") or []:
        if not isinstance(item, dict):
            continue
        if not str(item.get("title") or "").strip():
            continue
        video = discovery._normalize_related(item)
        if not video:
            continue
        try:
            video["index"] = int(item.get("index"))
        except (TypeError, ValueError):
            video["index"] = None
        videos.append(video)
    updated = discovery._int_or_none(payload.get("updated"))
    return {
        "playlist_id": str(payload.get("playlistId") or ""),
        "title": str(payload.get("title") or "Playlist"),
        "author": str(payload.get("author") or ""),
        "author_id": str(payload.get("authorId") or ""),
        "description": str(payload.get("description") or ""),
        "video_count": discovery._int_or_none(payload.get("videoCount")),
        "view_count": discovery._int_or_none(payload.get("viewCount")) or None,
        "updated_at": datetime.fromtimestamp(updated, tz=timezone.utc) if updated and updated > 0 else None,
        "cover_video_id": _cover_video_id(payload.get("playlistThumbnail")),
        "videos": videos,
    }


def fetch_invidious_playlist(playlist_id: str, page: int = 1) -> dict[str, Any]:
    """One page of a YouTube playlist from Invidious `/api/v1/playlists/<id>?page=N`.

    The other "import a playlist by URL" work may carry its own copy of this; the two are
    meant to be merged into one.
    """
    if not is_remote_playlist_id(playlist_id):
        raise RemotePlaylistError("Not a YouTube playlist id")
    page = max(1, min(int(page or 1), MAX_PLAYLIST_PAGES))
    key = (playlist_id, page)
    hit = _playlist_pages.get(key)
    if hit and time.monotonic() - hit[0] <= _PAGE_TTL_SECONDS:
        return hit[1]
    base, timeout = _invidious()
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(f"{base}/api/v1/playlists/{playlist_id}", params={"page": page})
        if response.status_code in (400, 404):
            raise RemotePlaylistError("This playlist does not exist or is private")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("error"):
        raise RemotePlaylistError(str((payload or {}).get("error") or "Unexpected answer from Invidious"))
    result = normalize_playlist_page(payload)
    _trim(_playlist_pages)
    _playlist_pages[key] = (time.monotonic(), result)
    return result


def new_videos_on_page(page_videos: list[dict[str, Any]], seen: set[int]) -> list[dict[str, Any]]:
    """The videos of one page not already listed, by playlist index. Updates `seen`."""
    fresh = []
    for video in page_videos:
        index = video.get("index")
        if index is None or index in seen:
            continue
        seen.add(index)
        fresh.append(video)
    return fresh


def remote_playlist(playlist_id: str, limit: int = 200, store: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A playlist's details and its first `limit` videos, in playlist order.

    `store` writes videos that are not in the library yet, because a watch page, a save
    and a list-mode queue all need a stored row.
    """
    meta = fetch_invidious_playlist(playlist_id, 1)
    seen: set[int] = set()
    videos = new_videos_on_page(meta["videos"], seen)
    page = 1
    while len(videos) < limit and page < MAX_PLAYLIST_PAGES:
        total = meta.get("video_count")
        if total is not None and len(seen) >= total:
            break
        page += 1
        fresh = new_videos_on_page(fetch_invidious_playlist(playlist_id, page)["videos"], seen)
        if not fresh:
            break
        videos.extend(fresh)
    videos.sort(key=lambda video: video.get("index") or 0)
    videos = videos[:limit]
    fill_missing_channels(videos)
    if store:
        store_videos(videos, "playlist")
    return meta, videos


def fill_missing_channels(videos: list[dict[str, Any]]) -> None:
    """Fill the channel of rows Invidious left blank from the library.

    YouTube's playlist pages often omit the uploader, so Invidious hands back rows with
    an empty author, which rendered as "Unknown channel" (QA, 2026-11-09). The library
    usually knows these videos already. Failure leaves the rows as they are.
    """
    missing = [video for video in videos if not video.get("channel_title") and video.get("youtube_video_id")]
    if not missing:
        return
    try:
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT v.youtube_video_id, COALESCE(NULLIF(c.title, ''), v.author) AS channel_title,
                       c.youtube_channel_id
                FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
                WHERE v.youtube_video_id = ANY(%s)
                """,
                ([video["youtube_video_id"] for video in missing],),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.info("filling playlist channels failed: %s", exc)
        return
    known = {str(row["youtube_video_id"]): row for row in rows if row.get("channel_title")}
    for video in missing:
        row = known.get(str(video["youtube_video_id"]))
        if not row:
            continue
        video["channel_title"] = video["author"] = row["channel_title"]
        if row.get("youtube_channel_id") and not video.get("youtube_channel_id"):
            video["youtube_channel_id"] = row["youtube_channel_id"]


def remote_playlist_slice(playlist_id: str, offset: int, limit: int = 100) -> tuple[list[dict[str, Any]], bool]:
    """Videos `offset`..`offset+limit` for "Show more". (videos, whether more remain)."""
    _, videos = remote_playlist(playlist_id, offset + limit + 1, store=False)
    chunk = videos[offset: offset + limit]
    store_videos(chunk, "playlist")
    return chunk, len(videos) > offset + limit


def store_videos(videos: list[dict[str, Any]], source: str) -> int:
    """Write the ones not stored yet. Failure is logged: the page still renders."""
    try:
        known = db.video_ids_in_library([video.get("youtube_video_id") for video in videos])
        fresh = [video for video in videos if video.get("youtube_video_id") not in known]
        return db.upsert_discovered_videos(fresh, source) if fresh else 0
    except Exception as exc:  # noqa: BLE001
        logger.info("storing %s videos failed: %s", source, exc)
        return 0


def copy_into_new_playlist(name: str, description: str, youtube_video_ids: list[str], user_key: str = "local") -> dict[str, Any]:
    """A new local playlist holding these videos in this order. Unstored ids are skipped."""
    playlist = db.create_playlist(name[:200] or "Saved playlist", description[:2000] or None, user_key)
    ids = [str(video_id) for video_id in youtube_video_ids if video_id]
    added = 0
    if ids:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO playlist_items (playlist_id, video_id, youtube_video_id, position)
                    SELECT %s, v.id, v.youtube_video_id, MIN(wanted.position)
                    FROM unnest(%s::text[]) WITH ORDINALITY AS wanted(youtube_video_id, position)
                    JOIN videos v ON v.youtube_video_id = wanted.youtube_video_id
                    GROUP BY v.id, v.youtube_video_id
                    ON CONFLICT (playlist_id, video_id) DO NOTHING
                    """,
                    (playlist["id"], ids),
                )
                added = int(getattr(cur, "rowcount", 0) or 0)
            conn.commit()
    return {**playlist, "added": added}


# --- the Live tab -----------------------------------------------------------------------


def is_live_now(item: dict[str, Any]) -> bool:
    """Whether a /streams row is on air.

    Invidious reports `liveNow` false for every row of this list. A finished stream always
    has a length and an upcoming one says so, so a row with neither is on air now; the
    library uses the same rule (live rows are stored with a duration of exactly 0).
    """
    if item.get("liveNow"):
        return True
    if item.get("isUpcoming"):
        return False
    return discovery._int_or_none(item.get("lengthSeconds")) == 0


def normalize_stream(item: dict[str, Any], channel_id: str) -> dict[str, Any] | None:
    video = discovery._normalize_related(item)
    if not video:
        return None
    video.setdefault("youtube_channel_id", channel_id)
    if not video.get("youtube_channel_id"):
        video["youtube_channel_id"] = channel_id
    video["live_now"] = is_live_now(item)
    video["upcoming"] = bool(item.get("isUpcoming"))
    video["stream_kind"] = "live" if video["live_now"] else "upcoming" if video["upcoming"] else "past"
    if video["live_now"] or video["upcoming"]:
        # "0 views" on an on-air row is YouTube not rendering a count, not nobody watching.
        video["view_count"] = None
    return video


def fetch_channel_streams(channel_id: str, continuation: str = "") -> tuple[list[dict[str, Any]], str]:
    """One page of a channel's past and current live streams. (videos, next continuation)."""
    if not channel_id:
        return [], ""
    key = (channel_id, continuation)
    hit = _stream_pages.get(key)
    if hit and time.monotonic() - hit[0] <= _PAGE_TTL_SECONDS:
        return hit[1], hit[2]
    base, timeout = _invidious()
    params = {"continuation": continuation} if continuation else {}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(f"{base}/api/v1/channels/{channel_id}/streams", params=params)
        response.raise_for_status()
        payload = response.json()
    raw = payload.get("videos") if isinstance(payload, dict) else payload
    items = [
        video for video in (normalize_stream(item, channel_id) for item in (raw if isinstance(raw, list) else []) if isinstance(item, dict))
        if video
    ]
    # Live rows first, the way YouTube's Live tab leads with what is on air.
    items.sort(key=lambda video: (not video["live_now"], not video["upcoming"]))
    next_token = str(payload.get("continuation") or "") if isinstance(payload, dict) else ""
    _trim(_stream_pages)
    _stream_pages[key] = (time.monotonic(), items, next_token)
    return items, next_token


def channel_has_live_tab(available_tabs: Any) -> bool:
    """Invidious lists "streams" among a channel's tabs only when it has streamed."""
    return "streams" in {str(tab).lower() for tab in (available_tabs or [])}


# --- Channels page: sorts ---------------------------------------------------------------

EXTRA_CHANNEL_SORTS = [
    ("recent", "Recently subscribed"),
    ("relevant", "Most relevant"),
    ("activity", "New activity"),
]

# "New activity" counts uploads from this many days back that you have not watched.
NEW_ACTIVITY_DAYS = 30
# "Most relevant" weighs each watched video by how recently, halving every this many days.
RELEVANCE_HALF_LIFE_DAYS = 30

SUBSCRIPTION_STATS_SQL = f"""
    WITH watch AS (
      SELECT c.youtube_channel_id,
             SUM(
               (CASE WHEN pp.completed OR pp.manually_marked_watched THEN 1.0
                     ELSE LEAST(1.0, GREATEST(pp.percent_completed, 0.1)) END)
               * power(0.5, EXTRACT(EPOCH FROM (now() - COALESCE(pp.last_watched_at, pp.updated_at))) / 86400.0 / {RELEVANCE_HALF_LIFE_DAYS})
             ) AS score
      FROM playback_progress pp
      JOIN videos v ON v.id = pp.video_id
      JOIN channels c ON c.id = v.channel_id
      WHERE pp.user_key = %s AND c.youtube_channel_id IS NOT NULL
      GROUP BY c.youtube_channel_id
    ),
    fresh AS (
      SELECT c.youtube_channel_id, COUNT(*) AS unwatched
      FROM videos v
      JOIN channels c ON c.id = v.channel_id
      LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = %s
      LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
      WHERE v.published_at >= now() - interval '{NEW_ACTIVITY_DAYS} days'
        AND c.youtube_channel_id IS NOT NULL
        AND pp.id IS NULL
        AND COALESCE(uvf.not_interested, false) = false
      GROUP BY c.youtube_channel_id
    )
    SELECT fs.id AS subscription_id,
           us.created_at AS subscribed_at,
           COALESCE(watch.score, 0) AS watch_score,
           COALESCE(fresh.unwatched, 0) AS unwatched_new
    FROM feeds fs
    JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = %s
    LEFT JOIN watch ON watch.youtube_channel_id = fs.youtube_channel_id
    LEFT JOIN fresh ON fresh.youtube_channel_id = fs.youtube_channel_id
"""


def subscription_stats(user_key: str = "local") -> dict[str, dict[str, Any]]:
    """Per subscription of this user: when they added it, their watch activity and the
    new uploads they have not watched."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SUBSCRIPTION_STATS_SQL, (user_key, user_key, user_key, user_key))
            rows = list(cur.fetchall())
    return {str(row["subscription_id"]): dict(row) for row in rows}


def sort_subscriptions(items: list[dict[str, Any]], key: str, stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Order rows by one of EXTRA_CHANNEL_SORTS. Ties fall back to the name, A to Z.

    Rows sorted by a figure carry it as `sort_note`, so the row can say why it sits there.
    """
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)

    def stat(item: dict[str, Any], name: str, default: Any) -> Any:
        return (stats.get(str(item.get("id") or "")) or {}).get(name) or default

    def name(item: dict[str, Any]) -> str:
        return str(item.get("title") or "").lower()

    rows = [dict(item) for item in items]
    if key == "recent":
        rows.sort(key=name)
        rows.sort(key=lambda item: stat(item, "subscribed_at", epoch), reverse=True)
    elif key == "relevant":
        rows.sort(key=name)
        rows.sort(key=lambda item: float(stat(item, "watch_score", 0.0)), reverse=True)
    elif key == "activity":
        rows.sort(key=name)
        rows.sort(key=lambda item: int(stat(item, "unwatched_new", 0)), reverse=True)
        for item in rows:
            count = int(stat(item, "unwatched_new", 0))
            if count:
                item["sort_note"] = f"{count} new"
    return rows


# --- Channels page: bulk actions --------------------------------------------------------

BULK_ACTIONS = ("unsubscribe", "collection", "notify", "mute", "unmute")
BULK_LIMIT = 500


def subscriptions_by_ids(subscription_ids: list[str], user_key: str) -> list[dict[str, Any]]:
    """The given feeds, limited to ones this user follows: a bulk action never reaches a
    feed that only somebody else subscribed to."""
    ids = [str(value) for value in subscription_ids if str(value or "").strip()][:BULK_LIMIT]
    if not ids:
        return []
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fs.id, fs.title, fs.feed_url, fs.youtube_channel_id FROM feeds fs "
                "JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = %s "
                "WHERE fs.id = ANY(%s)",
                (user_key, ids),
            )
            return list(cur.fetchall())


def delete_subscriptions(subscription_ids: list[str], user_key: str) -> list[dict[str, Any]]:
    """Unsubscribe this user from these feeds, nothing else: the feed, its videos and
    everyone's history stay."""
    ids = [str(value) for value in subscription_ids if str(value or "").strip()][:BULK_LIMIT]
    return db.unsubscribe_feeds(user_key, ids)


def apply_bulk_action(
    action: str, subscription_ids: list[str], user_key: str = "local",
    collection_id: str = "", level: str = "",
) -> dict[str, Any]:
    """One action over many subscriptions. Returns which ids it applied to."""
    if action not in BULK_ACTIONS:
        raise ValueError("unknown action")
    if action == "notify" and level not in db.NOTIFICATION_LEVELS:
        raise ValueError("level must be all, personalized, or none")
    rows = subscriptions_by_ids(subscription_ids, user_key)
    if action == "unsubscribe":
        removed = delete_subscriptions([str(row["id"]) for row in rows], user_key)
        return {"applied": [str(row["id"]) for row in removed], "removed": removed}
    applied: list[str] = []
    skipped: list[str] = []
    if action == "collection":
        for row in rows:
            if db.add_subscription_to_collection(collection_id, str(row["id"]), user_key):
                applied.append(str(row["id"]))
            else:
                skipped.append(str(row["id"]))
        if rows and not applied:
            raise LookupError("collection not found")
    elif action == "notify":
        for row in rows:
            db.set_channel_notification(notification_key(row), level, user_key)
            applied.append(str(row["id"]))
    else:  # mute / unmute: only YouTube channels can be muted in the feed
        for row in rows:
            channel_id = str(row.get("youtube_channel_id") or "")
            if not channel_id:
                skipped.append(str(row["id"]))
                continue
            db.set_channel_muted(channel_id, action == "mute", user_key)
            applied.append(str(row["id"]))
    return {"applied": applied, "skipped": skipped}


# --- notifications for feeds that are not YouTube channels -------------------------------

FEED_KEY_PREFIX = "feed-"
FEED_KEY_RE = re.compile(r"^feed-[0-9a-f]{16}$")


def feed_notification_key(subscription_id: str) -> str:
    """The bell key for a subscription with no YouTube channel.

    channel_notifications is keyed by a text id, so such a feed gets a stable key of its
    own there. A digest rather than the raw id: feed ids contain a slash ("feed/4"),
    which cannot travel as one segment of /api/channels/<id>/notifications.
    """
    digest = hashlib.sha1(str(subscription_id).encode("utf-8")).hexdigest()[:16]
    return f"{FEED_KEY_PREFIX}{digest}"


def notification_key(subscription: dict[str, Any]) -> str:
    channel_id = str(subscription.get("youtube_channel_id") or "")
    return channel_id or feed_notification_key(str(subscription.get("id") or ""))


def can_notify(subscription: dict[str, Any]) -> bool:
    """A YouTube channel always can; a feed without one only once it has delivered videos."""
    return bool(subscription.get("youtube_channel_id")) or int(subscription.get("video_count") or 0) > 0


def is_feed_key(value: str) -> bool:
    return bool(FEED_KEY_RE.match(str(value or "")))


FEED_UPLOADS_SQL = """
    SELECT DISTINCT ON (v.id)
           v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url,
           v.published_at, v.duration_seconds, v.view_count, v.tags,
           COALESCE(c.title, fs.title) AS channel_title, c.avatar_url AS channel_avatar_url,
           c.youtube_channel_id, fs.id AS subscription_id
    FROM feeds fs
    JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = %s
    JOIN feed_entries fe ON fe.subscription_id = fs.id
    JOIN videos v ON v.id = fe.video_id
    LEFT JOIN channels c ON c.id = v.channel_id
    LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
    WHERE fs.youtube_channel_id IS NULL
      AND v.published_at IS NOT NULL
      AND COALESCE(uvf.not_interested, false) = false
      {shorts_clause}
    ORDER BY v.id, v.published_at DESC
"""


def list_feed_uploads(limit: int = 30, user_key: str = "local", include_shorts: bool = True) -> list[dict[str, Any]]:
    """Recent videos from this user's subscriptions that have no YouTube channel id (a
    playlist feed, an OPML feed of YouTube links). The channel query joins on the channel id, so these
    never reached the notification panel at all. Each row carries its `notify_key`.
    """
    shorts_clause = "" if include_shorts else f"AND NOT {db.SHORT_VIDEO_SQL}"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM ({FEED_UPLOADS_SQL.format(shorts_clause=shorts_clause)}) feed_rows "
                "ORDER BY published_at DESC LIMIT %s",
                (user_key, user_key, max(1, min(int(limit), 100))),
            )
            rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row["notify_key"] = feed_notification_key(str(row.get("subscription_id") or ""))
    return rows
