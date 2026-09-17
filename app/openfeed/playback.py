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


def stream_height(stream: dict[str, Any]) -> int:
    """Vertical resolution in pixels, or 0 when the label is missing or unparseable."""
    label = str(stream.get("qualityLabel") or stream.get("quality") or "")
    digits = label[:-1] if label.endswith("p") else label
    return int(digits) if digits.isdigit() else 0


def _stream_score(stream: dict[str, Any]) -> tuple[int, int]:
    height = stream_height(stream)
    if height > 1080:
        height = 0
    container_score = 1 if "mp4" in str(stream.get("type") or stream.get("container") or "").lower() else 0
    return (container_score, height)


def select_stream(video: dict[str, Any], itag: str = "") -> dict[str, Any] | None:
    streams = [item for item in video.get("formatStreams") or [] if item.get("url")]
    if not streams:
        return None
    if itag:
        for stream in streams:
            if str(stream.get("itag") or "") == str(itag):
                return stream
        # An itag that is no longer on offer means a stale picker, not a fatal error -
        # fall through and hand back the best rendition rather than failing the download.
    return sorted(streams, key=_stream_score, reverse=True)[0]


# Hover previews should start fast and sip bandwidth: a 360p muxed rendition is
# plenty on a card-sized player, and it is usually the first byte range the
# stream host can serve.
PREVIEW_MAX_HEIGHT = 360


def select_preview_stream(video: dict[str, Any]) -> dict[str, Any] | None:
    """The lightest muxed rendition worth previewing with.

    Prefers the best mp4 at or under 360p (usually itag 18) over the
    top-quality rendition select_stream() hands to downloads. Falls back to
    the lowest rendition on offer when nothing that small exists, and to
    non-mp4 containers only when no mp4 is published at all.
    """
    streams = [item for item in video.get("formatStreams") or [] if item.get("url")]
    if not streams:
        return None
    mp4s = [
        stream
        for stream in streams
        if "mp4" in str(stream.get("type") or stream.get("container") or "").lower()
    ]
    pool = mp4s or streams
    light = [stream for stream in pool if 0 < stream_height(stream) <= PREVIEW_MAX_HEIGHT]
    if light:
        return sorted(light, key=_stream_score, reverse=True)[0]
    return sorted(pool, key=lambda stream: stream_height(stream) or 10**9)[0]


def download_options(youtube_video_id: str) -> dict[str, Any]:
    """Every rendition the download button can actually deliver, best first.

    Only muxed `formatStreams` qualify. The DASH `adaptiveFormats` carry the 1440p and
    4K renditions but split audio and video into separate files, so offering them would
    hand back something silent or unplayable without remuxing. The highest adaptive
    height comes back alongside so the UI can say plainly why the list stops where it
    does instead of looking broken.
    """
    video = fetch_invidious_video(youtube_video_id)
    formats = []
    for stream in video.get("formatStreams") or []:
        if not stream.get("url"):
            continue
        stream_type = str(stream.get("type") or "video/mp4")
        size = stream.get("clen") or stream.get("contentLength")
        formats.append({
            "itag": str(stream.get("itag") or ""),
            "quality": str(stream.get("qualityLabel") or stream.get("quality") or "unknown"),
            "height": stream_height(stream),
            "container": "webm" if "webm" in stream_type.lower() else "mp4",
            "size_bytes": int(size) if str(size or "").isdigit() else None,
        })
    formats.sort(key=lambda item: item["height"], reverse=True)

    adaptive_heights = [
        stream_height(stream)
        for stream in video.get("adaptiveFormats") or []
        if str(stream.get("type") or "").startswith("video/")
    ]
    return {
        "title": video.get("title") or youtube_video_id,
        "formats": formats,
        "max_adaptive_height": max(adaptive_heights or [0]),
    }


def playback_for_video(youtube_video_id: str, itag: str = "") -> dict[str, Any]:
    video = fetch_invidious_video(youtube_video_id)
    stream = select_stream(video, itag)
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
        "itag": str(stream.get("itag") or ""),
        "invidious_watch_url": f"{public}/watch?v={youtube_video_id}" if public else None,
        "invidious_embed_url": f"{public}/embed/{youtube_video_id}" if public else None,
    }
