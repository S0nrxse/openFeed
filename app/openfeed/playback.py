from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings


class PlaybackError(RuntimeError):
    pass


def fetch_invidious_video(youtube_video_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.invidious_base_url:
        raise PlaybackError("Invidious is not configured")
    timeout = max(3, settings.enrichment_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(f"{settings.invidious_base_url}/api/v1/videos/{youtube_video_id}")
        response.raise_for_status()
        return response.json()


def _stream_score(stream: dict[str, Any]) -> tuple[int, int]:
    label = str(stream.get("qualityLabel") or stream.get("quality") or "")
    height = 0
    if label.endswith("p") and label[:-1].isdigit():
        height = int(label[:-1])
    if height > 1080:
        height = 0
    container_score = 1 if "mp4" in str(stream.get("type") or stream.get("container") or "").lower() else 0
    return (container_score, height)


def select_stream(video: dict[str, Any]) -> dict[str, Any] | None:
    streams = [item for item in video.get("formatStreams") or [] if item.get("url")]
    if not streams:
        return None
    return sorted(streams, key=_stream_score, reverse=True)[0]


def playback_for_video(youtube_video_id: str) -> dict[str, Any]:
    video = fetch_invidious_video(youtube_video_id)
    stream = select_stream(video)
    if not stream:
        raise PlaybackError("Invidious did not return a playable muxed stream")
    settings = get_settings()
    public = settings.invidious_public_url
    return {
        "youtube_video_id": youtube_video_id,
        "title": video.get("title"),
        "author": video.get("author"),
        "stream_url": stream.get("url"),
        "stream_type": stream.get("type") or "video/mp4",
        "quality": stream.get("qualityLabel") or stream.get("quality"),
        "invidious_watch_url": f"{public}/watch?v={youtube_video_id}" if public else None,
        "invidious_embed_url": f"{public}/embed/{youtube_video_id}" if public else None,
    }
