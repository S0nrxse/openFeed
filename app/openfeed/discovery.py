from __future__ import annotations

import logging
from typing import Any

import httpx

from . import db
from .config import get_settings

logger = logging.getLogger(__name__)


class DiscoveryError(RuntimeError):
    pass


def _thumbnail(items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    sorted_items = sorted(items, key=lambda item: int(item.get("width") or 0), reverse=True)
    return sorted_items[0].get("url")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_related(item: dict[str, Any]) -> dict[str, Any] | None:
    video_id = item.get("videoId") or item.get("video_id")
    if not video_id:
        return None
    author_id = item.get("authorId") or item.get("ucid")
    return {
        "youtube_video_id": video_id,
        "title": item.get("title") or video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "author": item.get("author"),
        "channel_title": item.get("author"),
        "youtube_channel_id": author_id,
        "duration_seconds": _int_or_none(item.get("lengthSeconds")),
        "view_count": _int_or_none(item.get("viewCount")),
        "summary": item.get("description"),
        "published_at": None,
    }


def fetch_related(youtube_video_id: str) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.invidious_base_url:
        raise DiscoveryError("Invidious is not configured")
    timeout = max(3, settings.enrichment_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(f"{settings.invidious_base_url}/api/v1/videos/{youtube_video_id}")
        response.raise_for_status()
        data = response.json()
    raw_items = data.get("recommendedVideos") or data.get("relatedVideos") or []
    normalized = []
    for item in raw_items:
        video = _normalize_related(item)
        if video and video["youtube_video_id"] != youtube_video_id:
            normalized.append(video)
    return normalized


def discover_related_videos(seed_limit: int = 3, per_seed: int = 6) -> dict[str, Any]:
    seeds = db.list_discovery_seed_videos(max(1, min(seed_limit, 20)))
    results: list[dict[str, Any]] = []
    created = 0
    errors = 0
    for seed in seeds:
        source_video_id = seed.get("id")
        youtube_video_id = seed.get("youtube_video_id")
        try:
            related = fetch_related(youtube_video_id)[: max(1, min(per_seed, 20))]
            stored_for_seed = 0
            for item in related:
                stored_id = db.upsert_discovered_video(item, source_video_id, "invidious_related", f"Related to {youtube_video_id}")
                if stored_id:
                    stored_for_seed += 1
            db.record_retrieval_attempt(source_video_id, "invidious_related", "success", f"stored {stored_for_seed} related videos")
            created += stored_for_seed
            results.append({"ok": True, "youtube_video_id": youtube_video_id, "stored_count": stored_for_seed})
        except Exception as exc:
            errors += 1
            message = str(exc)
            db.record_retrieval_attempt(source_video_id, "invidious_related", "error", message)
            logger.warning("related discovery failed for %s: %s", youtube_video_id, message)
            results.append({"ok": False, "youtube_video_id": youtube_video_id, "message": message})
    return {
        "ok": errors == 0,
        "seed_count": len(seeds),
        "stored_count": created,
        "error_count": errors,
        "items": results,
    }
