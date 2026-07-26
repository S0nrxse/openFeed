from __future__ import annotations

import logging
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
        "view_count": data.get("viewCount"),
        "like_count": data.get("likeCount"),
        # YouTube stopped publishing dislikes in 2021, so this is almost always 0 or
        # missing. Stored anyway so the UI can show it whenever a source does report one.
        "dislike_count": data.get("dislikeCount"),
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
    }


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
