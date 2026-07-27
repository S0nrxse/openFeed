"""YouTube community posts, read-only.

Channels post text, images, polls and links to videos alongside their uploads. Invidious
exposes these through its community endpoints, which this module normalises into one shape
the rest of the app can render.

Two things about the upstream data are worth knowing, because they shape the whole feature:

* Invidious returns posts under a key called ``comments``, and the post id arrives as
  ``commentId`` while ``postId`` is null. Neither is a mistake to work around — it is just
  what the API returns — but reading the parser without knowing that is confusing.
* **Polls carry no per-option vote counts.** The API gives the option text and a single
  ``totalVotes`` for the whole poll. YouTube does not publish the split, so no amount of
  parsing will produce real percentages. That is why voting here is explicitly simulated:
  the bars a user sees are their own local tally, never YouTube's, and the UI has to say so
  rather than implying otherwise.

Nothing here writes to YouTube. There is no posting, no commenting, and no vote submission,
all of which would require an authenticated account.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .config import get_settings

logger = logging.getLogger("openfeed.posts")

_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_POST_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Attachment types this app knows how to draw. Anything else is kept as a post with a note
# rather than dropped, because a text body with an unrenderable attachment is still worth
# reading.
SUPPORTED_ATTACHMENTS = frozenset({"video", "image", "multiImage", "playlist", "poll"})


class PostsError(RuntimeError):
    """Community posts could not be fetched."""


def valid_channel_id(value: str) -> bool:
    return bool(_CHANNEL_ID.match(str(value or "")))


def valid_post_id(value: str) -> bool:
    return bool(_POST_ID.match(str(value or "")))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _published(value: Any) -> datetime | None:
    raw = _int_or_none(value)
    if not raw or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _best_thumbnail(items: Any, preferred_width: int = 640) -> str:
    """Pick a reasonable image, preferring one near the requested width."""
    if not isinstance(items, list):
        return ""
    scored: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        if url.startswith("//"):
            url = f"https:{url}"
        scored.append((abs(int(item.get("width") or 0) - preferred_width), url))
    if not scored:
        return ""
    scored.sort(key=lambda pair: pair[0])
    return scored[0][1]


def normalize_attachment(raw: Any) -> dict[str, Any] | None:
    """Turn one attachment into something renderable, or mark it unsupported.

    An unknown type is reported rather than discarded so the UI can say "this post has an
    attachment OpenFeed cannot show yet" instead of silently rendering a post that looks
    like it is missing something.
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip()
    if not kind:
        return None
    if kind not in SUPPORTED_ATTACHMENTS:
        return {"type": "unsupported", "raw_type": kind}

    if kind == "video":
        video_id = str(raw.get("videoId") or "")
        if not video_id:
            return {"type": "unsupported", "raw_type": kind}
        return {
            "type": "video",
            "youtube_video_id": video_id,
            "title": str(raw.get("title") or video_id),
            "author": str(raw.get("author") or ""),
            "duration_seconds": _int_or_none(raw.get("lengthSeconds")),
            "view_count": _int_or_none(raw.get("viewCount")),
            "published_at": _published(raw.get("published")),
        }

    if kind in {"image", "multiImage"}:
        # multiImage nests a list of thumbnail lists; a single image is one list.
        groups = raw.get("images") if kind == "multiImage" else [raw.get("imageThumbnails")]
        urls = [_best_thumbnail(group) for group in (groups or []) if group]
        urls = [url for url in urls if url]
        if not urls:
            return {"type": "unsupported", "raw_type": kind}
        return {"type": "image", "images": urls}

    if kind == "playlist":
        playlist_id = str(raw.get("playlistId") or "")
        if not playlist_id:
            return {"type": "unsupported", "raw_type": kind}
        return {
            "type": "playlist",
            "playlist_id": playlist_id,
            "title": str(raw.get("title") or playlist_id),
            "author": str(raw.get("author") or ""),
            "video_count": _int_or_none(raw.get("videoCount")),
        }

    # poll
    choices = [
        str(choice.get("text") or "").strip()
        for choice in (raw.get("choices") or [])
        if isinstance(choice, dict) and str(choice.get("text") or "").strip()
    ]
    if not choices:
        return {"type": "unsupported", "raw_type": kind}
    return {
        "type": "poll",
        "choices": choices,
        # The only real number YouTube publishes. There is no per-option breakdown, which
        # is precisely why the vote UI is simulated.
        "total_votes": _int_or_none(raw.get("totalVotes")) or 0,
    }


def normalize_post(raw: Any, channel_id: str = "") -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    # Invidious calls it commentId here; postId is present but null.
    post_id = str(raw.get("commentId") or raw.get("postId") or "").strip()
    if not post_id:
        return None
    published = _published(raw.get("published"))
    return {
        "post_id": post_id,
        "youtube_channel_id": str(raw.get("authorId") or channel_id or ""),
        "author": str(raw.get("author") or ""),
        "author_thumbnail": _best_thumbnail(raw.get("authorThumbnails"), 88),
        "author_is_owner": bool(raw.get("authorIsChannelOwner")),
        "content": str(raw.get("content") or ""),
        "content_html": str(raw.get("contentHtml") or ""),
        "published_at": published,
        "published_text": str(raw.get("publishedText") or ""),
        "like_count": _int_or_none(raw.get("likeCount")) or 0,
        "reply_count": _int_or_none(raw.get("replyCount")) or 0,
        "is_edited": bool(raw.get("isEdited")),
        "attachment": normalize_attachment(raw.get("attachment")),
    }


# Posts change rarely and a channel page hits this on every open, so it is cached like
# the other read-only third-party lookups in the app.
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key: str) -> dict[str, Any] | None:
    ttl = max(0, get_settings().posts_cache_seconds)
    if not ttl:
        return None
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry[0]:
            return entry[1]
    return None


def _store(key: str, payload: dict[str, Any]) -> None:
    ttl = max(0, get_settings().posts_cache_seconds)
    if not ttl:
        return
    with _cache_lock:
        _cache[key] = (time.time() + ttl, payload)
        if len(_cache) > 256:
            now = time.time()
            for cached_key, (expires, _) in list(_cache.items()):
                if expires <= now:
                    _cache.pop(cached_key, None)


def _get(path: str, params: dict[str, str] | None = None) -> Any:
    settings = get_settings()
    if not settings.invidious_base_url:
        raise PostsError("Invidious is not configured")
    try:
        with httpx.Client(timeout=max(5, settings.enrichment_timeout_seconds), follow_redirects=True) as client:
            response = client.get(f"{settings.invidious_base_url}{path}", params=params or None)
    except httpx.HTTPError as exc:
        raise PostsError(f"Invidious request failed: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise PostsError(f"Invidious returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise PostsError("Invidious returned output that was not JSON") from exc


def channel_posts(channel_id: str, continuation: str = "") -> dict[str, Any]:
    """One page of a channel's community posts, plus a token for the next."""
    if not valid_channel_id(channel_id):
        return {"ok": False, "reason": "bad_channel", "posts": [], "continuation": ""}
    key = f"{channel_id}:{continuation}"
    cached = _cached(key)
    if cached is not None:
        return cached

    payload = _get(f"/api/v1/channels/{channel_id}/community", {"continuation": continuation} if continuation else None)
    if payload is None:
        # A channel with community posts disabled 404s, which is not an error worth
        # surfacing as one — it just has no posts.
        result = {"ok": True, "posts": [], "continuation": "", "reason": "none"}
        _store(key, result)
        return result

    raw_posts = payload.get("comments") if isinstance(payload, dict) else None
    posts = [post for post in (normalize_post(item, channel_id) for item in (raw_posts or [])) if post]
    result = {
        "ok": True,
        "posts": posts,
        "continuation": str((payload or {}).get("continuation") or "") if isinstance(payload, dict) else "",
    }
    _store(key, result)
    return result


def single_post(post_id: str, channel_id: str = "") -> dict[str, Any] | None:
    """One post by id.

    Invidious needs the channel to resolve a post, so when it is not supplied this falls
    back to scanning the channel's first pages — which is why the detail page always links
    with both ids.
    """
    if not valid_post_id(post_id) or not valid_channel_id(channel_id):
        return None
    key = f"post:{channel_id}:{post_id}"
    cached = _cached(key)
    if cached is not None:
        return cached.get("post")

    payload = _get(f"/api/v1/post/{post_id}", {"ucid": channel_id})
    post = normalize_post(payload, channel_id) if isinstance(payload, dict) else None
    if post is None:
        # Older Invidious builds have no /post route; fall back to the channel listing.
        continuation = ""
        for _ in range(3):
            page = channel_posts(channel_id, continuation)
            for candidate in page.get("posts") or []:
                if candidate["post_id"] == post_id:
                    post = candidate
                    break
            if post or not page.get("continuation"):
                break
            continuation = page["continuation"]
    if post:
        _store(key, {"post": post})
    return post


def post_comments(post_id: str, channel_id: str, continuation: str = "") -> dict[str, Any]:
    """Replies to a post. Read-only: there is no path here that writes a comment."""
    if not valid_post_id(post_id) or not valid_channel_id(channel_id):
        return {"ok": False, "comments": [], "continuation": ""}
    params = {"ucid": channel_id}
    if continuation:
        params["continuation"] = continuation
    try:
        payload = _get(f"/api/v1/post/{post_id}/comments", params)
    except PostsError as exc:
        logger.info("post comments failed for %s: %s", post_id, exc)
        return {"ok": False, "comments": [], "continuation": ""}
    if not isinstance(payload, dict):
        return {"ok": True, "comments": [], "continuation": ""}
    return {
        "ok": True,
        "comments": payload.get("comments") or [],
        "continuation": str(payload.get("continuation") or ""),
    }


def subscription_posts(channel_ids: Iterable[str], per_channel: int = 3, limit: int = 40) -> list[dict[str, Any]]:
    """Recent posts across several channels, newest first.

    Used by the feeds. Bounded per channel so one prolific poster cannot fill the page,
    and bounded overall so a large subscription list does not turn one page render into
    seventy upstream requests.
    """
    collected: list[dict[str, Any]] = []
    for channel_id in list(channel_ids)[:limit]:
        try:
            page = channel_posts(channel_id)
        except PostsError as exc:
            logger.info("subscription posts failed for %s: %s", channel_id, exc)
            continue
        collected.extend((page.get("posts") or [])[:per_channel])
    collected.sort(key=lambda post: post.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return collected


__all__ = [
    "PostsError",
    "SUPPORTED_ATTACHMENTS",
    "channel_posts",
    "clear_cache",
    "normalize_attachment",
    "normalize_post",
    "post_comments",
    "single_post",
    "subscription_posts",
    "valid_channel_id",
    "valid_post_id",
]
