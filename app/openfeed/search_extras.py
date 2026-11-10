"""Search type-ahead, the real search sorts, Watch later and the Not interested list.

Kept out of main.py so the page code only calls in. Everything here is server side: the
type-ahead asks this deployment's own Invidious, and every list is the caller's own rows.
"""

from __future__ import annotations

import html
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx

from . import db, discovery, privacy
from .config import get_settings

logger = logging.getLogger("openfeed.search_extras")


# ---------------------------------------------------------------------------
# Type-ahead suggestions
# ---------------------------------------------------------------------------

# A prefix is typed by everybody who searches for anything that starts with it, so a short
# cache answers most keystrokes. Kept in memory: losing it on a restart costs one request.
SUGGEST_TTL_SECONDS = 600.0
SUGGEST_CACHE_MAX = 512
SUGGEST_LIMIT = 10
SUGGEST_QUERY_MAX = 100
_suggest_cache: dict[str, tuple[float, list[str]]] = {}
_suggest_lock = threading.Lock()


def _suggest_key(query: str) -> str:
    return " ".join(str(query or "").split())[:SUGGEST_QUERY_MAX]


def _suggest_cached(key: str) -> list[str] | None:
    with _suggest_lock:
        hit = _suggest_cache.get(key.lower())
        if not hit or time.monotonic() - hit[0] > SUGGEST_TTL_SECONDS:
            return None
        return list(hit[1])


def _remember_suggestions(key: str, items: list[str]) -> None:
    with _suggest_lock:
        if len(_suggest_cache) >= SUGGEST_CACHE_MAX:
            for stale in sorted(_suggest_cache, key=lambda k: _suggest_cache[k][0])[: SUGGEST_CACHE_MAX // 4]:
                _suggest_cache.pop(stale, None)
        _suggest_cache[key.lower()] = (time.monotonic(), list(items))


def clean_suggestions(payload: Any, query: str) -> list[str]:
    """The strings out of Invidious' answer, unescaped, deduplicated, at most SUGGEST_LIMIT."""
    raw = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(html.unescape(item).split())[:SUGGEST_QUERY_MAX]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            found.append(text)
    return found[:SUGGEST_LIMIT]


def suggestions(query: str, scope: str = "") -> list[str]:
    """What YouTube suggests for a prefix, through Invidious. [] when it cannot say.

    `scope` keeps one account's cache apart from another's on a shared install: a shared
    cache would answer instantly for a prefix someone else typed, which tells the next
    person what was searched for. With sign-in off there is one user and one scope."""
    key = _suggest_key(query)
    if not key:
        return []
    cache_key = f"{scope}\x00{key}" if scope else key
    cached = _suggest_cached(cache_key)
    if cached is not None:
        return cached
    base = str(get_settings().invidious_base_url or "").rstrip("/")
    if not base:
        return []
    try:
        with httpx.Client(timeout=3.0, follow_redirects=True) as client:
            response = client.get(f"{base}/api/v1/search/suggestions", params={"q": key})
            response.raise_for_status()
            found = clean_suggestions(response.json(), key)
    except Exception as exc:  # noqa: BLE001 - a type-ahead that fails just shows recents
        logger.info("search suggestions unavailable for %s: %s", privacy.loggable(key), exc)
        return []
    _remember_suggestions(cache_key, found)
    return found


# ---------------------------------------------------------------------------
# Upload date: newest first, for real
# ---------------------------------------------------------------------------

# YouTube ignores sort=date (checked against Invidious on 2026-11-09: the order matches
# relevance), but its upload-date *filters* work. Asking each window for its most relevant
# matches and merging them by date puts what was uploaded today first, then this week,
# and so on, which is what "Upload date" promises. A chosen Date filter caps the windows.
_WINDOWS_FOR_DATE: dict[str, tuple[str, ...]] = {
    "hour": ("hour",),
    "today": ("hour", "today"),
    "week": ("today", "week"),
    "month": ("today", "week", "month"),
    "year": ("today", "week", "month", "year"),
    "": ("today", "week", "month", "year", ""),
}


def date_windows(date: str) -> tuple[str, ...]:
    return _WINDOWS_FOR_DATE.get(date or "", _WINDOWS_FOR_DATE[""])


def published_key(video: dict[str, Any]) -> float:
    value = video.get("published_at")
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    return float("-inf")


def search_newest_first(query: str, page: int, date: str = "", duration: str = "", features: str = "") -> list[dict[str, Any]]:
    """One page of YouTube results, newest first, gathered across the date windows.

    The windows are asked side by side, so a page costs about one request's time. A window
    that fails is left out; if every window fails, the first error is raised.
    """
    windows = date_windows(date)
    with ThreadPoolExecutor(max_workers=len(windows)) as pool:
        futures = [
            pool.submit(discovery.search_youtube, query, page=page, sort="relevance", date=window, duration=duration, features=features)
            for window in windows
        ]
    merged: dict[str, dict[str, Any]] = {}
    errors: list[BaseException] = []
    for future in futures:
        try:
            items = future.result()
        except Exception as exc:  # noqa: BLE001 - one window down still sorts the rest
            errors.append(exc)
            continue
        for item in items:
            video_id = str(item.get("youtube_video_id") or "")
            if video_id and video_id not in merged:
                merged[video_id] = item
    if errors and len(errors) == len(futures):
        raise errors[0]
    return sorted(merged.values(), key=published_key, reverse=True)


# ---------------------------------------------------------------------------
# Rating: only where a real one exists
# ---------------------------------------------------------------------------

# Below this many votes a ratio says nothing (one like and no dislikes is 100%).
RATING_MIN_VOTES = 20


def rating_available(user_key: str) -> bool:
    """A rating needs dislike counts, which only Return YouTube Dislike supplies."""
    from . import ryd

    try:
        return ryd.enabled(user_key)
    except Exception:  # noqa: BLE001
        return False


def rating_of(likes: Any, dislikes: Any) -> float | None:
    try:
        up = int(likes)
        down = int(dislikes)
    except (TypeError, ValueError):
        return None
    if up < 0 or down < 0 or up + down < RATING_MIN_VOTES:
        return None
    return up / (up + down)


def stored_ratings(video_ids: list[str]) -> dict[str, float]:
    """Like ratio for the given videos that have a Return YouTube Dislike count."""
    ids = sorted({str(v) for v in video_ids if v})
    if not ids:
        return {}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT youtube_video_id, like_count, dislike_count
                FROM videos
                WHERE youtube_video_id = ANY(%s) AND dislike_source = 'ryd'
                """,
                (ids,),
            )
            rows = cur.fetchall()
    found: dict[str, float] = {}
    for row in rows:
        value = rating_of(row.get("like_count"), row.get("dislike_count"))
        if value is not None:
            found[str(row["youtube_video_id"])] = value
    return found


def order_by_rating(videos: list[dict[str, Any]], ratings: dict[str, float]) -> list[dict[str, Any]]:
    """Rated videos best first; unrated ones keep their order after them."""
    rated = [v for v in videos if str(v.get("youtube_video_id") or "") in ratings]
    unrated = [v for v in videos if str(v.get("youtube_video_id") or "") not in ratings]
    rated.sort(key=lambda v: ratings[str(v.get("youtube_video_id") or "")], reverse=True)
    return rated + unrated


# ---------------------------------------------------------------------------
# Search inside playlists
# ---------------------------------------------------------------------------

def playlist_matches(query: str, user_key: str, limit: int = 4) -> list[dict[str, Any]]:
    """Videos in the caller's playlists whose title or channel matches, newest save first.

    Reads only the caller's playlist items (a few hundred rows at most), so it costs
    nothing next to the library search it sits beside.
    """
    cleaned = query.strip()
    if not cleaned:
        return []
    pattern = f"%{db.escape_like_pattern(cleaned)}%"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       array_agg(DISTINCT p.name) AS playlist_names,
                       MAX(pi.added_at) AS added_at
                FROM playlists p
                JOIN playlist_items pi ON pi.playlist_id = p.id
                JOIN videos v ON v.id = pi.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE p.user_key = %s
                  AND (v.title ILIKE %s OR c.title ILIKE %s OR v.author ILIKE %s)
                GROUP BY v.id, c.id
                ORDER BY MAX(pi.added_at) DESC
                LIMIT %s
                """,
                (user_key, pattern, pattern, pattern, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def playlist_note(names: Any) -> str:
    items = sorted({str(name).strip() for name in (names or []) if str(name or "").strip()}, key=str.lower)
    if not items:
        return "In your playlists"
    if len(items) > 2:
        return f"In {items[0]}, {items[1]} and {len(items) - 2} more"
    return "In " + " and ".join(items)


# ---------------------------------------------------------------------------
# Watch later
# ---------------------------------------------------------------------------

WATCH_LATER_NAME = "Watch later"


def watch_later_playlist(user_key: str, create: bool = False) -> dict[str, Any] | None:
    """The caller's Watch later playlist, by name, made on first use when `create`.

    It is an ordinary playlist named "Watch later", the same one ?list=watch-later plays.
    The advisory lock keeps two quick presses from making two of them.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            if create:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"openfeed-watch-later:{user_key}",))
            cur.execute(
                """
                SELECT id, name FROM playlists
                WHERE user_key = %s AND lower(btrim(name)) = 'watch later'
                ORDER BY created_at ASC LIMIT 1
                """,
                (user_key,),
            )
            row = cur.fetchone()
            if not row and create:
                cur.execute(
                    "INSERT INTO playlists (user_key, name) VALUES (%s, %s) RETURNING id, name",
                    (user_key, WATCH_LATER_NAME),
                )
                row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def watch_later_ids(user_key: str) -> list[str]:
    playlist = watch_later_playlist(user_key)
    if not playlist:
        return []
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT youtube_video_id FROM playlist_items WHERE playlist_id = %s ORDER BY position", (playlist["id"],))
            return [str(row["youtube_video_id"]) for row in cur.fetchall()]


def set_watch_later(youtube_video_id: str, saved: bool, user_key: str) -> dict[str, Any] | None:
    """Put a video in Watch later or take it out. None when the video is unknown."""
    if saved:
        playlist = watch_later_playlist(user_key, create=True)
        if not playlist:
            return None
        item = db.add_video_to_playlist(str(playlist["id"]), youtube_video_id, user_key)
        if not item:
            return None
        return {"saved": True, "playlist_id": str(playlist["id"])}
    playlist = watch_later_playlist(user_key)
    if playlist:
        db.remove_video_from_playlist(str(playlist["id"]), youtube_video_id, user_key)
    return {"saved": False, "playlist_id": str(playlist["id"]) if playlist else ""}


# ---------------------------------------------------------------------------
# Undoing feedback, and the Not interested list
# ---------------------------------------------------------------------------

UNDOABLE_EVENTS = ("more_like_this", "less_like_this", "hide", "not_interested")
NEGATIVE_EVENTS = ("hide", "not_interested", "less_like_this")


def feedback_snapshot(row: dict[str, Any] | None) -> dict[str, Any]:
    """The parts of a feedback row an Undo puts back."""
    row = row or {}
    reaction = row.get("reaction")
    return {
        "reaction": reaction if reaction in {"like", "dislike"} else None,
        "not_interested": bool(row.get("not_interested")),
        "not_interested_reason": (str(row.get("not_interested_reason"))[:500] if row.get("not_interested_reason") else None),
    }


def undo_feedback(youtube_video_id: str, event_id: int, previous: dict[str, Any], user_key: str) -> bool:
    """Take back one More like this, Less like this or Not interested.

    The event row goes, because the rankers count events, and the feedback row returns to
    what it was before the press. Returns False when there was nothing of the caller's to undo.
    """
    state = feedback_snapshot(previous)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_video_events e
                USING videos v
                WHERE e.id = %s AND e.user_key = %s AND e.video_id = v.id
                  AND v.youtube_video_id = %s AND e.event_type = ANY(%s)
                RETURNING e.video_id
                """,
                (int(event_id), user_key, youtube_video_id, list(UNDOABLE_EVENTS)),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False
            cur.execute(
                """
                UPDATE user_video_feedback
                SET reaction = %s, not_interested = %s, not_interested_reason = %s,
                    last_event_type = 'undo', updated_at = now()
                WHERE user_key = %s AND video_id = %s
                """,
                (state["reaction"], state["not_interested"], state["not_interested_reason"], user_key, row["video_id"]),
            )
        conn.commit()
    return True


def list_not_interested_videos(user_key: str, limit: int = 300) -> list[dict[str, Any]]:
    """Videos marked Not interested, or Less like this and still disliked, latest first."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       f.not_interested, f.reaction, f.updated_at AS marked_at,
                       EXISTS (
                         SELECT 1 FROM user_video_events e
                         WHERE e.user_key = f.user_key AND e.video_id = f.video_id AND e.event_type = 'less_like_this'
                       ) AS less_like_this
                FROM user_video_feedback f
                JOIN videos v ON v.id = f.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE f.user_key = %s
                  AND (f.not_interested
                       OR (f.reaction = 'dislike' AND EXISTS (
                             SELECT 1 FROM user_video_events e
                             WHERE e.user_key = f.user_key AND e.video_id = f.video_id AND e.event_type = 'less_like_this')))
                ORDER BY f.updated_at DESC
                LIMIT %s
                """,
                (user_key, limit),
            )
            return [dict(row) for row in cur.fetchall()]


def clear_not_interested(youtube_video_id: str, user_key: str) -> int:
    """Let a video back into recommendations: clears Not interested and Less like this."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_video_feedback f
                SET not_interested = false, not_interested_reason = NULL,
                    reaction = CASE
                      WHEN f.reaction = 'dislike' AND EXISTS (
                        SELECT 1 FROM user_video_events e
                        WHERE e.user_key = f.user_key AND e.video_id = f.video_id AND e.event_type = 'less_like_this')
                      THEN NULL ELSE f.reaction END,
                    last_event_type = 'undo_not_interested', updated_at = now()
                FROM videos v
                WHERE v.id = f.video_id AND v.youtube_video_id = %s AND f.user_key = %s
                  AND (f.not_interested OR f.reaction = 'dislike')
                RETURNING f.video_id
                """,
                (youtube_video_id, user_key),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return 0
            cur.execute(
                "DELETE FROM user_video_events WHERE user_key = %s AND video_id = %s AND event_type = ANY(%s)",
                (user_key, row["video_id"], list(NEGATIVE_EVENTS)),
            )
        conn.commit()
    return 1


def muted_channel_rows(user_key: str) -> list[dict[str, Any]]:
    """The muted creators, newest mute first, with names where the library has them."""
    keys = [str(key).strip().lower() for key in (db.get_feed_preferences(user_key).get("muted_channels") or []) if str(key).strip()]
    if not keys:
        return []
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT youtube_channel_id, title, avatar_url, handle
                FROM channels
                WHERE lower(youtube_channel_id) = ANY(%s) OR lower(title) = ANY(%s)
                """,
                (keys, keys),
            )
            rows = cur.fetchall()
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        for candidate in (str(row.get("youtube_channel_id") or "").lower(), str(row.get("title") or "").lower()):
            if candidate and candidate not in by_key:
                by_key[candidate] = dict(row)
    return [{"key": key, **by_key.get(key, {})} for key in reversed(keys)]
