from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"(UC[A-Za-z0-9_-]{20,})")


def youtube_video_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(unquote(url))
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)
    if "v" in query and query["v"] and VIDEO_ID_RE.match(query["v"][0]):
        return query["v"][0]
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith("youtu.be") and parts and VIDEO_ID_RE.match(parts[0]):
        return parts[0]
    if parts:
        for marker in ("shorts", "embed", "live"):
            if marker in parts:
                idx = parts.index(marker)
                if idx + 1 < len(parts) and VIDEO_ID_RE.match(parts[idx + 1]):
                    return parts[idx + 1]
    return None


def youtube_channel_id_from_text(value: str | None) -> str | None:
    if not value:
        return None
    value = unquote(value)
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if "channel_id" in query and query["channel_id"]:
        match = CHANNEL_ID_RE.search(query["channel_id"][0])
        if match:
            return match.group(1)
    match = CHANNEL_ID_RE.search(value)
    return match.group(1) if match else None
