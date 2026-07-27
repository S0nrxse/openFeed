from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import quote

import httpx

from . import db
from .config import get_settings

logger = logging.getLogger(__name__)


class EnrichmentError(RuntimeError):
    pass


def _best_invidious_thumbnail(items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    sorted_items = sorted(items, key=lambda item: int(item.get("width") or 0), reverse=True)
    return sorted_items[0].get("url")


def _normalize_invidious(data: dict[str, Any]) -> dict[str, Any]:
    captions = data.get("captions") or []
    return {
        "title": data.get("title"),
        "author": data.get("author"),
        "youtube_channel_id": data.get("authorId") or data.get("ucid"),
        "subscriber_count": data.get("subCount") or data.get("authorSubCount"),
        "thumbnail_url": _best_invidious_thumbnail(data.get("videoThumbnails") or []),
        "summary": data.get("description"),
        "description": data.get("description"),
        "duration_seconds": data.get("lengthSeconds"),
        # Only videos that arrived through the RSS feed carry a publish date; discovered
        # ones have none, which left most of the library with no age to show. Invidious
        # reports it as unix seconds, so this is where the gap gets filled.
        "published_at": data.get("published"),
        "view_count": data.get("viewCount"),
        "like_count": data.get("likeCount"),
        # YouTube stopped publishing dislikes in 2021 and now returns 0 for everything,
        # so a zero here means "not published", not "nobody disliked it". Storing that 0
        # made the video look like it already had a count: it rendered as no count at all
        # and, worse, excluded the video from the RYD backfill permanently.
        "dislike_count": data.get("dislikeCount") or None,
        "allow_ratings": data.get("allowRatings"),
        "captions_status": "available" if captions else "unknown",
        "raw": data,
    }


def _normalize_oembed(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": data.get("title"),
        "author": data.get("author_name"),
        "thumbnail_url": data.get("thumbnail_url"),
        "summary": None,
        "description": None,
        "duration_seconds": None,
        "view_count": None,
        "like_count": None,
        "dislike_count": None,
        "allow_ratings": None,
        "captions_status": "unknown",
        "raw": data,
    }


def fetch_video_metadata(youtube_video_id: str) -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    timeout = max(3, settings.enrichment_timeout_seconds)
    errors: list[str] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        if settings.invidious_base_url:
            try:
                response = client.get(f"{settings.invidious_base_url}/api/v1/videos/{youtube_video_id}")
                response.raise_for_status()
                return "invidious", _normalize_invidious(response.json())
            except Exception as exc:
                errors.append(f"invidious: {exc}")

        try:
            watch_url = quote(f"https://www.youtube.com/watch?v={youtube_video_id}", safe="")
            response = client.get(f"https://www.youtube.com/oembed?url={watch_url}&format=json")
            response.raise_for_status()
            return "youtube_oembed", _normalize_oembed(response.json())
        except Exception as exc:
            errors.append(f"youtube_oembed: {exc}")

    raise EnrichmentError("; ".join(errors) or "No enrichment source configured")


def enrich_video(youtube_video_id: str, video_pk: int | None = None) -> dict[str, Any]:
    try:
        source, metadata = fetch_video_metadata(youtube_video_id)
        stored_id = db.update_video_metadata(youtube_video_id, metadata, source)
        if stored_id:
            db.record_retrieval_attempt(stored_id, source, "success", None)
        return {"ok": True, "youtube_video_id": youtube_video_id, "source": source, "video_id": stored_id}
    except Exception as exc:
        message = str(exc)
        stored_id = db.mark_video_metadata_error(youtube_video_id, message)
        db.record_retrieval_attempt(stored_id or video_pk, "metadata", "error", message)
        logger.warning("metadata enrichment failed for %s: %s", youtube_video_id, message)
        return {"ok": False, "youtube_video_id": youtube_video_id, "status": "error", "message": message}


def _proxied_ggpht(url: str | None) -> str | None:
    """Rewrite a googleusercontent avatar/banner URL onto our own /ggpht/ proxy.

    Keeps image requests inside the stack instead of hitting Google from the browser,
    the same way video thumbnails already go through /vi/.
    """
    if not url:
        return None
    text = str(url)
    for marker in ("/ggpht/", "googleusercontent.com/"):
        index = text.find(marker)
        if index != -1:
            return "/ggpht/" + text[index + len(marker):]
    return text


def _pick_image(items: Any, preferred_width: int) -> str | None:
    if not isinstance(items, list) or not items:
        return None
    usable = [item for item in items if isinstance(item, dict) and item.get("url")]
    if not usable:
        return None
    at_least = [item for item in usable if int(item.get("width") or 0) >= preferred_width]
    if at_least:
        return min(at_least, key=lambda item: int(item.get("width") or 0)).get("url")
    return max(usable, key=lambda item: int(item.get("width") or 0)).get("url")


def fetch_channel_metadata(youtube_channel_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.invidious_base_url:
        raise EnrichmentError("Invidious is not configured")
    timeout = max(3, settings.enrichment_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(f"{settings.invidious_base_url}/api/v1/channels/{youtube_channel_id}")
        response.raise_for_status()
        data = response.json()
    return {
        "title": data.get("author"),
        "avatar_url": _proxied_ggpht(_pick_image(data.get("authorThumbnails"), 176)),
        # Prefer YouTube's full-width desktop crop (2560x423) so it stays sharp on wide screens.
        "banner_url": _proxied_ggpht(_pick_image(data.get("authorBanners"), 2560)),
        "subscriber_count": data.get("subCount"),
        "description": data.get("description"),
        # Invidious exposes no channelHandle field, so this is derived from authorUrl when
        # YouTube happens to have given the handle form. Absent for most channels.
        "handle": _handle_from(data.get("authorUrl")),
        "verified": bool(data.get("authorVerified")),
        # Which tabs the channel actually has, so we do not offer an empty Shorts tab.
        "available_tabs": [str(tab) for tab in (data.get("tabs") or []) if str(tab).strip()],
    }


def _handle_from(author_url: Any) -> str | None:
    text = str(author_url or "")
    marker = "/@"
    index = text.find(marker)
    if index == -1:
        return None
    handle = text[index + 1:].split("/")[0].strip()
    return handle or None


def enrich_channel(youtube_channel_id: str) -> dict[str, Any]:
    try:
        metadata = fetch_channel_metadata(youtube_channel_id)
        updated = db.update_channel_media(youtube_channel_id, metadata)
        return {
            "ok": bool(updated),
            "youtube_channel_id": youtube_channel_id,
            "avatar": bool(metadata.get("avatar_url")),
            "banner": bool(metadata.get("banner_url")),
        }
    except Exception as exc:
        message = str(exc)
        logger.warning("channel enrichment failed for %s: %s", youtube_channel_id, message)
        return {"ok": False, "youtube_channel_id": youtube_channel_id, "status": "error", "message": message}


def enrich_channels(limit: int = 10, refresh: bool = False) -> dict[str, Any]:
    candidates = db.list_channels_needing_media(max(1, min(limit, 100)), refresh)
    results = [enrich_channel(item["youtube_channel_id"]) for item in candidates]
    return {
        "ok": all(item.get("ok") for item in results) if results else True,
        "candidates_count": len(candidates),
        "success_count": sum(1 for item in results if item.get("ok")),
        "error_count": sum(1 for item in results if not item.get("ok")),
        "items": results,
    }


def enrich_recent_videos(limit: int = 5) -> dict[str, Any]:
    candidates = db.list_videos_needing_enrichment(max(1, min(limit, 50)))
    results = [enrich_video(item["youtube_video_id"], item["id"]) for item in candidates]
    return {
        "ok": all(item.get("ok") for item in results) if results else True,
        "candidates_count": len(candidates),
        "success_count": sum(1 for item in results if item.get("ok")),
        "error_count": sum(1 for item in results if not item.get("ok")),
        "items": results,
    }


# Channel art is fetched one channel at a time from Invidious, so a bulk subscribe has to
# be paced or it turns into a burst of a hundred requests. These bounds keep a background
# top-up polite; the worker calls it repeatedly rather than asking for everything at once.
CHANNEL_MEDIA_BATCH = 12
CHANNEL_MEDIA_GAP_SECONDS = 0.35

_subscription_art_lock = threading.Lock()


def backfill_subscription_art(max_channels: int = 400) -> dict[str, Any]:
    """Fill in avatars for every subscribed channel that has none, in one pass.

    `enrich_channels_paced` tops up twelve channels per worker cycle across the whole
    library. With ten discovered channels for every subscribed one that took most of a day
    to reach the sidebar, which is why a freshly imported subscription list stayed blank.
    This walks only the subscribed ones and keeps going until they are done.

    Guarded by a lock rather than a queue: two concurrent passes would fetch the same
    channels twice, and there is nothing here worth a job system.
    """
    import time as _time

    if not _subscription_art_lock.acquire(blocking=False):
        return {"ok": True, "skipped": "already running", "updated": 0}
    try:
        updated = 0
        attempted = 0
        while attempted < max_channels:
            batch = db.list_subscription_channels_needing_media(CHANNEL_MEDIA_BATCH)
            if not batch:
                break
            for index, item in enumerate(batch):
                if index or attempted:
                    _time.sleep(CHANNEL_MEDIA_GAP_SECONDS)
                attempted += 1
                result = enrich_channel(item["youtube_channel_id"])
                updated += 1 if result.get("ok") else 0
            if len(batch) < CHANNEL_MEDIA_BATCH:
                break
            # A channel Invidious cannot resolve keeps coming back in the next batch, so
            # stop once a full pass changed nothing rather than looping on it forever.
            if not updated:
                break
        remaining = db.count_subscription_channels_missing_media()
        logger.info("subscription art backfill: attempted=%s updated=%s remaining=%s", attempted, updated, remaining)
        return {"ok": True, "attempted": attempted, "updated": updated, "remaining": remaining}
    finally:
        _subscription_art_lock.release()


def enrich_channels_paced(limit: int = CHANNEL_MEDIA_BATCH, refresh: bool = False) -> dict[str, Any]:
    """Fill in avatars and banners for channels that have none, slowly.

    Subscribing to 70 channels at once leaves 70 blank avatars, which is what makes an
    imported subscription list look broken. This fixes that without hammering Invidious:
    a small batch, spaced out, resumable on the next pass.
    """
    import time as _time

    candidates = db.list_channels_needing_media(max(1, min(limit, 40)), refresh)
    ok = 0
    for index, item in enumerate(candidates):
        if index:
            _time.sleep(CHANNEL_MEDIA_GAP_SECONDS)
        result = enrich_channel(item["youtube_channel_id"])
        ok += 1 if result.get("ok") else 0
    return {"ok": True, "candidates": len(candidates), "updated": ok, "remaining_hint": len(candidates) == limit}
