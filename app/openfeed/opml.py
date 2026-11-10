"""Reading an OPML subscription list, the format every feed reader exports.

Import is the other half of Settings, Your data, Subscriptions as OPML (dataexport.py).
It subscribes only the person importing (user_subscriptions is per user_key), so one
profile importing a list leaves the others alone.

The parser is the stdlib's, guarded the way the RSS poller guards it: a document that
declares a DOCTYPE or entities is refused outright (entity expansion is how XML bombs
work), and both the byte size and the number of feeds are capped.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from .youtube_ids import CHANNEL_ID_STRICT_RE, youtube_channel_id_from_text

MAX_BYTES = 2 * 1024 * 1024
MAX_FEEDS = 500


class OpmlError(ValueError):
    """The file is not an OPML list this importer accepts. The message is shown as is."""


def _http_url(value: str) -> str:
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) and len(text) <= 2048 else ""


def parse(data: bytes) -> list[dict[str, Any]]:
    """Every feed in an OPML document: {feed_url, title, html_url, youtube_channel_id}.

    Outlines without an http(s) xmlUrl are skipped (folders, comments). Duplicates (the
    same channel, or the same feed address) are listed once.
    """
    if not data:
        raise OpmlError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise OpmlError("That file is larger than an OPML list should be (2 MB).")
    head = data[:65536].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise OpmlError("That file declares a DOCTYPE, which OpenFeed does not accept.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OpmlError("That is not a readable OPML file.") from exc
    if str(root.tag).lower() != "opml":
        raise OpmlError("That is not an OPML file.")
    feeds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for outline in root.iter():
        if str(outline.tag).lower() != "outline":
            continue
        attrs = {str(key).lower(): str(value) for key, value in outline.attrib.items()}
        feed_url = _http_url(attrs.get("xmlurl", ""))
        if not feed_url:
            continue
        channel_id = youtube_channel_id_from_text(feed_url) or ""
        if channel_id and not CHANNEL_ID_STRICT_RE.match(channel_id):
            channel_id = ""
        key = channel_id.lower() or feed_url.lower()
        if key in seen:
            continue
        seen.add(key)
        title = " ".join((attrs.get("title") or attrs.get("text") or "").split())[:200]
        feeds.append({
            "feed_url": feed_url,
            "title": title,
            "html_url": _http_url(attrs.get("htmlurl", "")),
            "youtube_channel_id": channel_id or None,
        })
        if len(feeds) > MAX_FEEDS:
            raise OpmlError(f"That list has more than {MAX_FEEDS} feeds. Split it and import the parts.")
    if not feeds:
        raise OpmlError("No feeds were found in that file.")
    return feeds
