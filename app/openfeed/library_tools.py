"""Library tools: History pause and clear, bulk edits, sorting, playlist order, privacy,
YouTube playlist import and undo for deleted playlists and collections.

The SQL lives here rather than in db.py so this feature is one module; it uses db.connect
and the same dict rows. The pure helpers (sorting, moves, parsing) need no database and are
what the unit tests exercise.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import db
from .config import get_settings

logger = logging.getLogger(__name__)

# Icons this feature adds to main.ICON_PATHS (merged there, so _icon and _menu_row know them).
ICON_PATHS = {
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    "grip": '<path d="M5 9h14"/><path d="M5 15h14"/>',
    "select": '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="m8 12 3 3 5-6"/>',
    "move-top": '<path d="M5 4h14"/><path d="M12 20V9"/><path d="m7 13 5-5 5 5"/>',
    "move-bottom": '<path d="M5 20h14"/><path d="M12 4v11"/><path d="m7 11 5 5 5-5"/>',
    "arrow-up": '<path d="M12 19V5"/><path d="m6 11 6-6 6 6"/>',
    "arrow-down": '<path d="M12 5v14"/><path d="m6 13 6 6 6-6"/>',
    "import": '<path d="M4 6h10"/><path d="M4 12h7"/><path d="M4 18h7"/><path d="M18 9v10"/><path d="m14 15 4 4 4-4"/>',
}


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

# (key, label), default first. The labels follow YouTube's own Sort menus.
HISTORY_SORTS: tuple[tuple[str, str], ...] = (("newest", "Newest watched"), ("oldest", "Oldest watched"))
LIKED_SORTS: tuple[tuple[str, str], ...] = (("recent", "Recently liked"), ("oldest", "Oldest liked"))
PLAYLIST_SORTS: tuple[tuple[str, str], ...] = (
    ("manual", "Manual"),
    ("added-newest", "Date added (newest)"),
    ("added-oldest", "Date added (oldest)"),
    ("popular", "Most popular"),
    ("published-newest", "Date published (newest)"),
    ("published-oldest", "Date published (oldest)"),
    ("title", "Title (A–Z)"),
    ("duration", "Duration (shortest)"),
)


def clean_sort(value: Any, options: tuple[tuple[str, str], ...]) -> str:
    """The sort key if it is one of options, else the default (the first)."""
    key = str(value or "").strip().lower()
    return key if any(key == option for option, _ in options) else options[0][0]


def _timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def sort_playlist_videos(videos: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """A playlist's rows in the order a sort key asks for. Manual is the stored order.

    Rows missing the value sorted on (no publish date, no view count) go last whichever
    way the sort runs, so "newest" never opens on a pile of unknowns. Ties keep the
    manual order, since Python's sort is stable.
    """
    key = clean_sort(key, PLAYLIST_SORTS)
    rows = list(videos)
    if key == "manual":
        return rows

    def by(getter: Callable[[dict[str, Any]], float | None], descending: bool) -> list[dict[str, Any]]:
        known = [row for row in rows if getter(row) is not None]
        unknown = [row for row in rows if getter(row) is None]
        known.sort(key=lambda row: getter(row) or 0.0, reverse=descending)
        return known + unknown

    if key == "added-newest":
        return by(lambda row: _timestamp(row.get("added_at")), True)
    if key == "added-oldest":
        return by(lambda row: _timestamp(row.get("added_at")), False)
    if key == "published-newest":
        return by(lambda row: _timestamp(row.get("published_at")), True)
    if key == "published-oldest":
        return by(lambda row: _timestamp(row.get("published_at")), False)
    if key == "popular":
        return by(lambda row: _number(row.get("view_count")), True)
    if key == "duration":
        return by(lambda row: _number(row.get("duration_seconds")) or None, False)
    # title
    return sorted(rows, key=lambda row: str(row.get("title") or "").casefold())


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------

MOVES = ("top", "bottom", "up", "down")


def apply_move(order: list[str], video_id: str, move: str = "", index: int | None = None) -> list[str]:
    """The list after moving one video: to the top or bottom, one step, or to an index.

    index is the position the video should end up at, counted in the new list, which is
    what a drop between two rows means. An unknown video or move leaves the list as it was.
    """
    if video_id not in order:
        return list(order)
    rest = [item for item in order if item != video_id]
    current = order.index(video_id)
    if index is not None:
        target = max(0, min(int(index), len(rest)))
    elif move == "top":
        target = 0
    elif move == "bottom":
        target = len(rest)
    elif move == "up":
        target = max(0, current - 1)
    elif move == "down":
        target = min(len(rest), current + 1)
    else:
        return list(order)
    rest.insert(target, video_id)
    return rest


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

# YouTube's three settings, worded for one install: "anyone" here is anyone who can sign
# in to this OpenFeed (any account, any household profile), never the internet.
VISIBILITIES: tuple[tuple[str, str, str], ...] = (
    ("public", "Public", "Anyone signed in to this OpenFeed can find and view"),
    ("unlisted", "Unlisted", "Anyone signed in to this OpenFeed with the link can view"),
    ("private", "Private", "Only you can view"),
)
VISIBILITY_ICONS = {"public": "globe", "unlisted": "link", "private": "lock"}


def clean_visibility(value: Any, default: str = "private") -> str:
    key = str(value or "").strip().lower()
    return key if any(key == option for option, _, _ in VISIBILITIES) else default


def visibility_label(value: Any) -> str:
    key = clean_visibility(value)
    return next(label for option, label, _ in VISIBILITIES if option == key)


# ---------------------------------------------------------------------------
# YouTube playlist ids
# ---------------------------------------------------------------------------

PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
# Lists that only exist inside a signed-in Google account, which OpenFeed never has.
ACCOUNT_LISTS = {"LL", "WL", "LM", "LIKED", "WATCH_LATER"}


class ImportRefused(ValueError):
    """A link that cannot be imported. The message is shown to the person as is."""


def parse_playlist_id(text: Any) -> str:
    """The playlist id in a youtube.com link, a music.youtube.com link or a bare id.

    Raises ImportRefused with a sentence the dialog can show when there is none, or when
    the list is one only a Google sign-in can read.
    """
    raw = unquote(str(text or "").strip())
    if not raw:
        raise ImportRefused("Paste a YouTube playlist link.")
    candidate = raw
    if "/" in raw or "list=" in raw or "." in raw:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.netloc.lower().split(":")[0]
        query = parse_qs(parsed.query)
        known_host = host.endswith("youtube.com") or host.endswith("youtu.be") or host.endswith("youtube-nocookie.com")
        if not known_host and not query.get("list"):
            raise ImportRefused("That isn't a YouTube link. Paste a link from youtube.com with list= in it.")
        candidate = (query.get("list") or [""])[0].strip()
        if not candidate:
            raise ImportRefused("That link has no playlist in it. Open the playlist on YouTube and copy its link.")
    candidate = candidate.strip()
    if candidate.upper() in ACCOUNT_LISTS:
        raise ImportRefused("Liked videos and Watch later on YouTube belong to your Google account, which OpenFeed never signs in to.")
    if candidate.startswith("RD"):
        raise ImportRefused("That's a Mix. YouTube builds Mixes as you watch, so there is no fixed list to import.")
    if not PLAYLIST_ID_RE.match(candidate):
        raise ImportRefused("That doesn't look like a YouTube playlist id.")
    return candidate


# ---------------------------------------------------------------------------
# Invidious
# ---------------------------------------------------------------------------

IMPORT_MAX_VIDEOS = 5000  # YouTube's own cap on a playlist
IMPORT_MAX_PAGES = 60


def _video_from_invidious(item: dict[str, Any]) -> dict[str, Any] | None:
    from .discovery import _normalize_related

    if not isinstance(item, dict) or item.get("type", "video") != "video":
        return None
    # Deleted, private and blocked entries come back as placeholders: "[Deleted video]"
    # with no channel, or a blank title and author. Real videos can also lack the channel
    # (YouTube's playlist pages often omit it), so the test is the title, and the channel
    # is filled from the library afterwards (channel_tools.fill_missing_channels).
    title = str(item.get("title") or "").strip()
    if not title or (title.startswith("[") and title.endswith("]") and not str(item.get("authorId") or "").strip()):
        return None
    return _normalize_related(item)


def fetch_youtube_playlist(
    playlist_id: str,
    on_page: Callable[[int, int], None] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """A YouTube playlist through Invidious: title, author, description and its videos in order.

    Invidious pages overlap (page 2 starts 50 entries before page 1 ended), so entries
    are keyed by their index and the walk stops at the advertised count, at a page that
    adds nothing, or at YouTube's 5,000 cap. on_page(fetched, total) reports progress.
    """
    settings = get_settings()
    if not settings.invidious_base_url:
        raise ImportRefused("Invidious is not configured, so YouTube playlists cannot be read.")
    timeout = max(10, settings.enrichment_timeout_seconds)
    owned = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    base = f"{settings.invidious_base_url}/api/v1/playlists/{playlist_id}"
    by_index: dict[int, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    total = 0
    try:
        for page in range(1, IMPORT_MAX_PAGES + 1):
            response = client.get(base, params={"page": page} if page > 1 else None)
            if response.status_code in (400, 404, 500) and page == 1:
                detail = ""
                try:
                    detail = str(response.json().get("error") or "")
                except ValueError:
                    pass
                lowered = detail.lower()
                if "private" in lowered:
                    raise ImportRefused("That playlist is private on YouTube.")
                if response.status_code == 404 or "exist" in lowered or "not found" in lowered or "unavailable" in lowered:
                    raise ImportRefused("YouTube says that playlist doesn't exist or isn't available.")
                raise ImportRefused(f"Invidious could not read that playlist{': ' + detail[:160] if detail else '.'}")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                break
            if page == 1:
                meta = payload
                total = int(payload.get("videoCount") or 0)
            added = 0
            for position, item in enumerate(payload.get("videos") or []):
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index"))
                except (TypeError, ValueError):
                    index = (page - 1) * 100 + position
                if index in by_index:
                    continue
                by_index[index] = item
                added += 1
            if on_page:
                on_page(len(by_index), max(total, len(by_index)))
            if not added or len(by_index) >= min(total or IMPORT_MAX_VIDEOS, IMPORT_MAX_VIDEOS):
                break
    finally:
        if owned:
            client.close()
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in sorted(by_index):
        video = _video_from_invidious(by_index[index])
        if not video or video["youtube_video_id"] in seen:
            continue
        seen.add(video["youtube_video_id"])
        videos.append(video)
        if len(videos) >= IMPORT_MAX_VIDEOS:
            break
    return {
        "playlist_id": playlist_id,
        "title": str(meta.get("title") or playlist_id),
        "author": str(meta.get("author") or ""),
        "description": str(meta.get("description") or ""),
        "video_count": total,
        "listed": len(by_index),
        "videos": videos,
    }


# ---------------------------------------------------------------------------
# Import jobs
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 3600


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key not in {"user_key", "created"}}


def get_import(job_id: str, user_key: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _public_job(job) if job and job["user_key"] == user_key else None


def _update_job(job_id: str, **changes: Any) -> None:
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(changes)


def run_import(job_id: str, playlist_id: str, user_key: str, name: str = "", visibility: str = "private") -> None:
    """Fetch, store the videos, then create the playlist with them in YouTube's order.

    The playlist is created only once the videos are in hand, so a failed fetch leaves no
    empty playlist behind.
    """
    try:
        _update_job(job_id, status="fetching")
        fetched = fetch_youtube_playlist(playlist_id, lambda done, total: _update_job(job_id, fetched=done, total=total))
        videos = fetched["videos"]
        from .channel_tools import fill_missing_channels

        fill_missing_channels(videos)
        if not videos:
            raise ImportRefused("That playlist has no videos OpenFeed can read.")
        _update_job(job_id, status="saving", title=fetched["title"])
        db.upsert_discovered_videos(videos, "playlist_import")
        description = fetched["description"].strip()[:5000] or None
        playlist = create_playlist(
            (name or fetched["title"]).strip()[:120] or "Imported playlist", description, user_key,
            visibility=visibility, source_playlist_id=playlist_id,
        )
        added = add_videos_to_playlist(str(playlist["id"]), [video["youtube_video_id"] for video in videos], user_key)
        _update_job(
            job_id, status="done", playlist_id=str(playlist["id"]), name=playlist["name"],
            added=added, listed=fetched["listed"], total=max(fetched["video_count"], fetched["listed"]),
            skipped=max(0, max(fetched["video_count"], fetched["listed"]) - added),
        )
    except ImportRefused as exc:
        _update_job(job_id, status="error", error=str(exc))
    except httpx.HTTPError as exc:
        logger.warning("playlist import %s failed: %s", playlist_id, exc)
        _update_job(job_id, status="error", error="Invidious did not answer. Try again in a minute.")
    except Exception as exc:  # noqa: BLE001 - the dialog needs an answer either way
        logger.exception("playlist import %s failed", playlist_id)
        _update_job(job_id, status="error", error=f"The import stopped: {type(exc).__name__}")


def start_import(source: str, user_key: str, name: str = "", visibility: str = "private", background: bool = True) -> dict[str, Any]:
    """Validate the link and start an import job. Raises ImportRefused for a bad link."""
    playlist_id = parse_playlist_id(source)
    job_id = uuid.uuid4().hex
    now = time.time()
    with _JOBS_LOCK:
        for stale in [key for key, job in _JOBS.items() if now - job["created"] > _JOB_TTL_SECONDS]:
            _JOBS.pop(stale, None)
        running = sum(1 for job in _JOBS.values() if job["user_key"] == user_key and job["status"] in {"queued", "fetching", "saving"})
        if running >= 2:
            raise ImportRefused("Two imports are already running. Wait for one to finish.")
        _JOBS[job_id] = {
            "id": job_id, "user_key": user_key, "created": now, "status": "queued",
            "source_playlist_id": playlist_id, "fetched": 0, "total": 0, "title": "",
            "playlist_id": "", "name": "", "added": 0, "skipped": 0, "error": "",
        }
    args = (job_id, playlist_id, user_key, name, clean_visibility(visibility))
    if background:
        threading.Thread(target=run_import, args=args, name=f"playlist-import-{job_id[:8]}", daemon=True).start()
    else:
        run_import(*args)
    return get_import(job_id, user_key) or {}


# ---------------------------------------------------------------------------
# Watch history
# ---------------------------------------------------------------------------

HISTORY_EVENTS = ("watch_page_open", "mark_watched")


# Every progress report asks, so the answer is kept for a few seconds per profile.
_PAUSED_CACHE: dict[str, tuple[float, bool]] = {}
_PAUSED_TTL_SECONDS = 5.0


def history_paused(user_key: str) -> bool:
    """Whether this profile paused watch history. False when the database cannot say."""
    cached = _PAUSED_CACHE.get(user_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT paused FROM user_history_settings WHERE user_key = %s", (user_key,))
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - a page still renders without the flag
        logger.debug("history pause flag unavailable: %s", exc)
        return False
    paused = bool(row and row["paused"])
    _PAUSED_CACHE[user_key] = (time.monotonic() + _PAUSED_TTL_SECONDS, paused)
    return paused


def set_history_paused(user_key: str, paused: bool) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_history_settings (user_key, paused, paused_at, updated_at)
                VALUES (%s, %s, CASE WHEN %s THEN now() END, now())
                ON CONFLICT (user_key) DO UPDATE SET
                  paused = EXCLUDED.paused,
                  paused_at = EXCLUDED.paused_at,
                  updated_at = now()
                RETURNING paused
                """,
                (user_key, bool(paused), bool(paused)),
            )
            row = cur.fetchone()
        conn.commit()
    paused = bool(row and row["paused"])
    _PAUSED_CACHE[user_key] = (time.monotonic() + _PAUSED_TTL_SECONDS, paused)
    return paused


def forget_ranking_caches(user_key: str) -> None:
    """The in-process profiles built from history, so the next render reads the change."""
    db.clear_watch_time_cache(user_key)
    try:
        from . import ranker

        with ranker._USER_CACHE_LOCK:
            ranker._USER_CACHE.pop(user_key, None)
    except Exception:  # noqa: BLE001 - nothing cached is nothing to clear
        pass


def clear_history(user_key: str) -> dict[str, int]:
    """Clear all watch history for one profile: what was opened or marked, and every
    resume position and watch-time total with it, as YouTube's "Clear all watch history"
    does. Likes, playlists and an imported Takeout history are separate and stay."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_video_events WHERE user_key = %s AND event_type = ANY(%s)",
                (user_key, list(HISTORY_EVENTS)),
            )
            events = cur.rowcount or 0
            cur.execute("DELETE FROM playback_progress WHERE user_key = %s", (user_key,))
            progress = cur.rowcount or 0
            # And what the catalog remembers of this browsing (catalog_scope.py): videos it
            # was shown in searches and on channel pages, and what it opened. Likes,
            # playlists, downloads and Takeout are kept, as above.
            cur.execute(
                "DELETE FROM video_provenance WHERE user_key = %s AND source IN ('watch', 'event', 'search', 'channel')",
                (user_key,),
            )
        conn.commit()
    forget_ranking_caches(user_key)
    return {"events": int(events), "progress": int(progress)}


def _clean_ids(ids: list[Any], limit: int = 500) -> list[str]:
    seen: list[str] = []
    for value in ids or []:
        text = str(value or "").strip()
        if text and len(text) <= 64 and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def remove_history_videos(youtube_video_ids: list[Any], user_key: str) -> int:
    """Remove several videos from history, resume marks included (as remove_history_video)."""
    ids = _clean_ids(youtube_video_ids)
    if not ids:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_video_events e
                USING videos v
                WHERE e.video_id = v.id AND v.youtube_video_id = ANY(%s)
                  AND e.user_key = %s AND e.event_type = ANY(%s)
                """,
                (ids, user_key, list(HISTORY_EVENTS)),
            )
            removed = cur.rowcount or 0
            cur.execute("DELETE FROM playback_progress WHERE youtube_video_id = ANY(%s) AND user_key = %s", (ids, user_key))
        conn.commit()
    forget_ranking_caches(user_key)
    return int(removed)


def remove_liked_videos(youtube_video_ids: list[Any], user_key: str) -> int:
    """Take videos off Liked videos: the like itself, any "More like this" taps, and a
    legacy feed-entry star that also lists a video there (the upgrade to per-user
    subscriptions turns those into likes and clears them, so this last part is normally a
    no-op). Returns how many videos were affected."""
    ids = _clean_ids(youtube_video_ids)
    if not ids:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_video_feedback SET reaction = NULL, last_event_type = 'clear_reaction', updated_at = now()
                WHERE user_key = %s AND youtube_video_id = ANY(%s) AND reaction = 'like'
                RETURNING youtube_video_id
                """,
                (user_key, ids),
            )
            touched = {str(row["youtube_video_id"]) for row in cur.fetchall()}
            cur.execute(
                """
                DELETE FROM user_video_events e
                USING videos v
                WHERE e.video_id = v.id AND v.youtube_video_id = ANY(%s)
                  AND e.user_key = %s AND e.event_type = 'more_like_this'
                RETURNING v.youtube_video_id
                """,
                (ids, user_key),
            )
            touched |= {str(row["youtube_video_id"]) for row in cur.fetchall()}
            cur.execute(
                """
                UPDATE feed_entries fe SET is_starred = false, updated_at = now()
                FROM videos v
                WHERE fe.video_id = v.id AND v.youtube_video_id = ANY(%s) AND fe.is_starred
                RETURNING v.youtube_video_id
                """,
                (ids,),
            )
            touched |= {str(row["youtube_video_id"]) for row in cur.fetchall()}
        conn.commit()
    forget_ranking_caches(user_key)
    return len(touched)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def create_playlist(
    name: str, description: str | None, user_key: str, *, visibility: str = "private", source_playlist_id: str | None = None,
) -> dict[str, Any]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO playlists (user_key, name, description, visibility, source_playlist_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (user_key, name.strip(), description, clean_visibility(visibility), source_playlist_id),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def update_playlist_settings(playlist_id: str, user_key: str, *, visibility: str | None = None, sort_key: str | None = None) -> dict[str, Any] | None:
    """Change a playlist's visibility and/or its sort. None leaves a field alone."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE playlists
                SET visibility = COALESCE(%s, visibility),
                    sort_key = COALESCE(%s, sort_key),
                    updated_at = now()
                WHERE id = %s AND user_key = %s
                RETURNING *
                """,
                (
                    clean_visibility(visibility) if visibility is not None else None,
                    clean_sort(sort_key, PLAYLIST_SORTS) if sort_key is not None else None,
                    playlist_id, user_key,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


# The name other people see on a shared playlist: the owning profile's name, else the
# account's display name, else the name the users table holds for a sign-in that has no
# account here. Never a user key: the users table stores the key itself as the name of a
# row made for a bare key (db._ensure_local_user_id), so that value is skipped.
OWNER_NAME_SQL = (
    "COALESCE(NULLIF(BTRIM(MAX(ap.name)), ''), NULLIF(BTRIM(MAX(acct.display_name)), ''), "
    "NULLIF(NULLIF(BTRIM(MAX(u.display_name)), ''), p.user_key))"
)
OWNER_JOINS_SQL = (
    "LEFT JOIN account_profiles ap ON ap.user_key = p.user_key\n"
    "                LEFT JOIN accounts acct ON acct.user_key = p.user_key\n"
    "                LEFT JOIN users u ON u.authentik_sub = p.user_key"
)


def get_viewable_playlist(playlist_id: str, viewer_key: str) -> dict[str, Any] | None:
    """A playlist this profile owns, or one its owner made unlisted or public.

    Adds is_owner and owner_name, the owner's name for the page byline (OWNER_NAME_SQL;
    empty when nothing better than an internal key is known).
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.*, COUNT(pi.id) AS video_count, MAX(pi.added_at) AS latest_video_at,
                       COALESCE(SUM(runtime.duration_seconds), 0) AS runtime_seconds,
                       COUNT(runtime.duration_seconds) AS runtime_known,
                       (p.user_key = %s) AS is_owner,
                       {OWNER_NAME_SQL} AS owner_name
                FROM playlists p
                LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
                LEFT JOIN videos runtime ON runtime.id = pi.video_id
                {OWNER_JOINS_SQL}
                WHERE p.id = %s AND (p.user_key = %s OR p.visibility IN ('unlisted', 'public'))
                GROUP BY p.id
                """,
                (viewer_key, playlist_id, viewer_key),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def list_shared_playlists(viewer_key: str, limit: int = 60) -> list[dict[str, Any]]:
    """Public playlists from the other profiles and accounts on this install, each with
    owner_name (OWNER_NAME_SQL)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.*, COUNT(pi.id) AS video_count,
                       COALESCE(SUM(runtime.duration_seconds), 0) AS runtime_seconds,
                       COUNT(runtime.duration_seconds) AS runtime_known,
                       cover.thumbnail_url AS cover_thumbnail_url, cover.youtube_video_id AS cover_youtube_video_id,
                       {OWNER_NAME_SQL} AS owner_name
                FROM playlists p
                LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
                LEFT JOIN videos runtime ON runtime.id = pi.video_id
                {OWNER_JOINS_SQL}
                LEFT JOIN LATERAL (
                  SELECT v.thumbnail_url, v.youtube_video_id
                  FROM playlist_items x JOIN videos v ON v.id = x.video_id
                  WHERE x.playlist_id = p.id
                  ORDER BY x.position ASC, x.added_at ASC
                  LIMIT 1
                ) cover ON true
                WHERE p.visibility = 'public' AND p.user_key <> %s
                GROUP BY p.id, cover.thumbnail_url, cover.youtube_video_id
                ORDER BY p.updated_at DESC
                LIMIT %s
                """,
                (viewer_key, max(1, min(int(limit), 200))),
            )
            return list(cur.fetchall())


def playlist_order(playlist_id: str, user_key: str) -> list[str]:
    """Every video id in an owned playlist, in its stored (manual) order."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pi.youtube_video_id
                FROM playlist_items pi JOIN playlists p ON p.id = pi.playlist_id
                WHERE p.id = %s AND p.user_key = %s
                ORDER BY pi.position ASC, pi.added_at ASC, pi.id ASC
                """,
                (playlist_id, user_key),
            )
            return [str(row["youtube_video_id"]) for row in cur.fetchall()]


def set_playlist_order(playlist_id: str, ordered_ids: list[str], user_key: str) -> int:
    """Renumber an owned playlist 1..n in the given order. Returns rows updated."""
    if not ordered_ids:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE playlist_items pi SET position = o.ord
                FROM unnest(%s::text[]) WITH ORDINALITY AS o(yid, ord), playlists p
                WHERE pi.playlist_id = p.id AND p.id = %s AND p.user_key = %s AND pi.youtube_video_id = o.yid
                """,
                (list(ordered_ids), playlist_id, user_key),
            )
            updated = cur.rowcount or 0
            if updated:
                cur.execute("UPDATE playlists SET updated_at = now() WHERE id = %s AND user_key = %s", (playlist_id, user_key))
        conn.commit()
    return int(updated)


def move_playlist_item(playlist_id: str, youtube_video_id: str, user_key: str, move: str = "", index: int | None = None) -> list[str] | None:
    """Move one video and persist the whole order. None when it is not in the playlist."""
    order = playlist_order(playlist_id, user_key)
    if youtube_video_id not in order:
        return None
    moved = apply_move(order, youtube_video_id, move, index)
    if moved != order:
        set_playlist_order(playlist_id, moved, user_key)
    return moved


def add_videos_to_playlist(playlist_id: str, youtube_video_ids: list[Any], user_key: str) -> int:
    """Append videos in the given order, skipping ones already in it. Returns how many went in."""
    ids = _clean_ids(youtube_video_ids, limit=5000)
    if not ids:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO playlist_items (playlist_id, video_id, youtube_video_id, position)
                SELECT p.id, v.id, v.youtube_video_id,
                       COALESCE((SELECT MAX(position) FROM playlist_items WHERE playlist_id = p.id), 0) + o.ord
                FROM playlists p
                CROSS JOIN unnest(%s::text[]) WITH ORDINALITY AS o(yid, ord)
                JOIN videos v ON v.youtube_video_id = o.yid
                WHERE p.id = %s AND p.user_key = %s
                ON CONFLICT (playlist_id, video_id) DO NOTHING
                """,
                (ids, playlist_id, user_key),
            )
            added = cur.rowcount or 0
            if added:
                cur.execute("UPDATE playlists SET updated_at = now() WHERE id = %s AND user_key = %s", (playlist_id, user_key))
        conn.commit()
    return int(added)


def remove_playlist_items(playlist_id: str, youtube_video_ids: list[Any], user_key: str) -> int:
    ids = _clean_ids(youtube_video_ids)
    if not ids:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM playlist_items pi
                USING playlists p
                WHERE pi.playlist_id = p.id AND p.id = %s AND p.user_key = %s AND pi.youtube_video_id = ANY(%s)
                """,
                (playlist_id, user_key, ids),
            )
            removed = cur.rowcount or 0
        conn.commit()
    return int(removed)


# ---------------------------------------------------------------------------
# Undo for deletes
# ---------------------------------------------------------------------------

TRASH_KINDS = ("playlist", "collection")
TRASH_RETENTION = "1 day"


def delete_playlist(playlist_id: str, user_key: str) -> str | None:
    """Delete an owned playlist, keeping a snapshot for Undo. Returns the undo id."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO library_trash (user_key, kind, item_id, snapshot)
                SELECT p.user_key, 'playlist', p.id::text, jsonb_build_object(
                  'row', to_jsonb(p),
                  'items', COALESCE((SELECT jsonb_agg(to_jsonb(pi) ORDER BY pi.position, pi.added_at)
                                     FROM playlist_items pi WHERE pi.playlist_id = p.id), '[]'::jsonb))
                FROM playlists p WHERE p.id = %s AND p.user_key = %s
                RETURNING id
                """,
                (playlist_id, user_key),
            )
            trash = cur.fetchone()
            if not trash:
                return None
            cur.execute("DELETE FROM playlists WHERE id = %s AND user_key = %s", (playlist_id, user_key))
            cur.execute(f"DELETE FROM library_trash WHERE deleted_at < now() - interval '{TRASH_RETENTION}'")
        conn.commit()
    return str(trash["id"])


def delete_collection(collection_id: str, user_key: str) -> str | None:
    """Delete an owned collection, keeping a snapshot for Undo. Returns the undo id."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO library_trash (user_key, kind, item_id, snapshot)
                SELECT sc.user_key, 'collection', sc.id::text, jsonb_build_object(
                  'row', to_jsonb(sc),
                  'members', COALESCE((SELECT jsonb_agg(to_jsonb(m)) FROM subscription_collection_members m
                                       WHERE m.collection_id = sc.id), '[]'::jsonb))
                FROM subscription_collections sc WHERE sc.id = %s AND sc.user_key = %s
                RETURNING id
                """,
                (collection_id, user_key),
            )
            trash = cur.fetchone()
            if not trash:
                return None
            cur.execute("DELETE FROM subscription_collections WHERE id = %s AND user_key = %s", (collection_id, user_key))
            cur.execute(f"DELETE FROM library_trash WHERE deleted_at < now() - interval '{TRASH_RETENTION}'")
        conn.commit()
    return str(trash["id"])


def restore_deleted(trash_id: str, user_key: str) -> dict[str, Any] | None:
    """Put a deleted playlist or collection back with its id, items and members.

    Videos and subscriptions that disappeared since are skipped. None when there is no
    such snapshot for this profile, or when the id or name has been taken again since.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, item_id FROM library_trash WHERE id = %s AND user_key = %s FOR UPDATE",
                (trash_id, user_key),
            )
            trash = cur.fetchone()
            if not trash:
                return None
            if trash["kind"] == "playlist":
                cur.execute(
                    """
                    INSERT INTO playlists
                    SELECT r.* FROM library_trash t, jsonb_populate_record(NULL::playlists, t.snapshot->'row') r
                    WHERE t.id = %s
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (trash_id,),
                )
                if not cur.fetchone():
                    conn.rollback()
                    return None
                cur.execute(
                    """
                    INSERT INTO playlist_items (playlist_id, video_id, youtube_video_id, position, note, added_at)
                    SELECT r.playlist_id, r.video_id, r.youtube_video_id, r.position, r.note, r.added_at
                    FROM library_trash t, jsonb_populate_recordset(NULL::playlist_items, t.snapshot->'items') r
                    JOIN videos v ON v.id = r.video_id
                    WHERE t.id = %s
                    ON CONFLICT (playlist_id, video_id) DO NOTHING
                    """,
                    (trash_id,),
                )
            elif trash["kind"] == "collection":
                cur.execute(
                    """
                    INSERT INTO subscription_collections
                    SELECT r.* FROM library_trash t, jsonb_populate_record(NULL::subscription_collections, t.snapshot->'row') r
                    WHERE t.id = %s
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (trash_id,),
                )
                if not cur.fetchone():
                    conn.rollback()
                    return None
                cur.execute(
                    """
                    INSERT INTO subscription_collection_members (collection_id, subscription_id, added_at)
                    SELECT r.collection_id, r.subscription_id, r.added_at
                    FROM library_trash t, jsonb_populate_recordset(NULL::subscription_collection_members, t.snapshot->'members') r
                    JOIN feeds fs ON fs.id = r.subscription_id
                    -- Only feeds the owner still follows: a collection holds its owner's
                    -- own subscriptions, and one dropped since the delete stays out.
                    JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = t.user_key
                    WHERE t.id = %s
                    ON CONFLICT (collection_id, subscription_id) DO NOTHING
                    """,
                    (trash_id,),
                )
            else:
                return None
            cur.execute("DELETE FROM library_trash WHERE id = %s", (trash_id,))
        conn.commit()
    return {"kind": str(trash["kind"]), "id": str(trash["item_id"])}
