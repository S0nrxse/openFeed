from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import db
from .config import get_settings
from .youtube_ids import VIDEO_ID_RE, youtube_channel_id_from_text

logger = logging.getLogger(__name__)

CHANNEL_FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

_ATOM_NS = "http://www.w3.org/2005/Atom"
_YT_NS = "http://www.youtube.com/xml/schemas/2015"
_MEDIA_NS = "http://search.yahoo.com/mrss/"


def channel_feed_url(youtube_channel_id: str) -> str:
    """Direct YouTube channel-RSS feed URL for a channel id."""
    return CHANNEL_FEED_URL_TEMPLATE.format(channel_id=youtube_channel_id.strip())


@dataclass
class ChannelFeedResult:
    """Outcome of one channel-feed HTTP fetch.

    ``status`` is the HTTP status, or None when the request itself failed
    (network error, timeout, DNS, ...). ``text`` is the response body for a
    200 and None otherwise (304 carries no body). ``etag`` / ``last_modified``
    are the response validators, or None when the server did not send them.
    """

    status: int | None
    text: str | None
    etag: str | None = None
    last_modified: str | None = None


def fetch_channel_feed(
    httpx_client: httpx.Client,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 20.0,
) -> ChannelFeedResult:
    """GET one channel feed, honouring conditional-request validators.

    Sends If-None-Match / If-Modified-Since when values are available and
    never raises: every httpx failure becomes ``status=None`` so the caller
    can record it and move on to the next channel.
    """
    headers: dict[str, str] = {"User-Agent": "OpenFeed RSS poller"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        response = httpx_client.get(url, headers=headers, timeout=timeout)
    except Exception:
        return ChannelFeedResult(status=None, text=None, etag=None, last_modified=None)
    response_etag = response.headers.get("etag")
    response_last_modified = response.headers.get("last-modified")
    if response.status_code == 304:
        return ChannelFeedResult(status=304, text=None, etag=response_etag, last_modified=response_last_modified)
    if response.status_code != 200:
        return ChannelFeedResult(
            status=response.status_code, text=None, etag=response_etag, last_modified=response_last_modified
        )
    return ChannelFeedResult(
        status=200, text=response.text, etag=response_etag, last_modified=response_last_modified
    )


def _local_name(tag: str) -> str:
    """Element tag without its ``{namespace}`` prefix, for tolerant matching."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str | None:
    """Text of the first direct child whose local tag name matches."""
    for child in element:
        if _local_name(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _link_href(element: ET.Element, rel: str = "alternate") -> str | None:
    """href of the first <link> child with the given rel (or any <link> href)."""
    fallback: str | None = None
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        if (child.get("rel") or "").strip() == rel:
            return href
        if fallback is None:
            fallback = href
    return fallback


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # Suffix only: a blanket replace would corrupt offsets containing Z nowhere
    # else, but be explicit anyway.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # fromisoformat accepts at most 6 fractional-second digits; trim longer ones.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_channel_feed(xml_text: str, feed_url: str) -> list[dict[str, Any]]:
    """Parse a YouTube channel Atom feed into ``normalize_entry``-shaped dicts.

    Same entry-dict keys as ``freshrss.normalize_entry``: id,
    youtube_video_id, youtube_channel_id, subscription_id, channel_title,
    feed_url, title, url, thumbnail_url, author, summary, published_at
    (timezone-aware datetime or None), is_read, is_starred, raw.

    Fresh RSS entries start unread and unstarred; ``subscription_id`` is None
    here and filled in by the caller. Entries without a video id are skipped.
    Raises ``xml.etree.ElementTree.ParseError`` on malformed XML so the caller
    can record the channel fetch as failed.
    """
    root = ET.fromstring(xml_text)
    feed_author: str | None = None
    for child in root:
        if _local_name(child.tag) == "author":
            feed_author = _child_text(child, "name")
            if feed_author:
                break
    channel_title = feed_author or _child_text(root, "title") or "Unknown channel"
    feed_channel_id = _child_text(root, "channelId") or youtube_channel_id_from_text(feed_url)

    entries: list[dict[str, Any]] = []
    for item in root:
        if _local_name(item.tag) != "entry":
            continue
        video_id = _child_text(item, "videoId")
        if not video_id:
            # Fall back to the entry id's trailing segment (``yt:video:<id>``).
            raw_id = _child_text(item, "id")
            if raw_id and ":" in raw_id:
                video_id = raw_id.rsplit(":", 1)[-1].strip() or None
        if not video_id or not VIDEO_ID_RE.match(video_id):
            continue

        entry_channel_id = _child_text(item, "channelId") or feed_channel_id
        url = _link_href(item) or f"https://www.youtube.com/watch?v={video_id}"
        author_name: str | None = None
        for child in item:
            if _local_name(child.tag) == "author":
                author_name = _child_text(child, "name")
                if author_name:
                    break
        description: str | None = None
        thumbnail: str | None = None
        for child in item:
            if _local_name(child.tag) == "group":
                description = _child_text(child, "description")
                for grandchild in child:
                    if _local_name(grandchild.tag) == "thumbnail":
                        href = (grandchild.get("url") or "").strip()
                        if href:
                            thumbnail = href
                            break
                break
        published_at = _parse_published_at(_child_text(item, "published")) or _parse_published_at(
            _child_text(item, "updated")
        )

        entries.append(
            {
                "id": _child_text(item, "id") or f"youtube:{video_id}",
                "youtube_video_id": video_id,
                "youtube_channel_id": entry_channel_id,
                "subscription_id": None,
                "channel_title": channel_title,
                "feed_url": feed_url,
                "title": _child_text(item, "title") or video_id,
                "url": url,
                "thumbnail_url": thumbnail or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "author": author_name or channel_title,
                "summary": description,
                "published_at": published_at,
                "is_read": False,
                "is_starred": False,
                "raw": {
                    "yt:videoId": video_id,
                    "yt:channelId": entry_channel_id,
                    "feed_url": feed_url,
                    "title": _child_text(item, "title"),
                    "link": url,
                    "author": author_name,
                    "published": _child_text(item, "published"),
                    "updated": _child_text(item, "updated"),
                    "description": description,
                },
            }
        )
    return entries


def sync_rss_once(channel_limit: int | None = None) -> dict[str, Any]:
    """Poll due YouTube channel RSS feeds and store new entries.

    Mirrors ``sync_freshrss_once``: returns ``{ok, sync_run_id,
    subscriptions_count, entries_count, message}`` with ``status`` + ``message``
    on failure. ``subscriptions_count`` is channels polled, ``entries_count``
    is entries stored. One bad feed never kills the loop: per-channel failures
    are recorded via ``db.record_rss_fetch(ok=False)`` and skipped.
    """
    settings = get_settings()
    if channel_limit is None:
        channel_limit = int(getattr(settings, "rss_poll_limit", 0) or 0) or 100
    ttl_seconds = int(getattr(settings, "rss_poll_ttl_seconds", 0) or 0) or 3600
    sync_id: int | None = None
    subscriptions_count = 0
    entries_count = 0
    try:
        sync_id = db.create_sync_run("rss")
    except Exception as exc:
        logger.exception("RSS sync run could not start: %s", exc)
        return {"ok": False, "sync_run_id": None, "status": "error", "message": f"RSS sync failed: {exc}"}
    try:
        channels = db.list_rss_due_channels(channel_limit)
        if not channels:
            message = "No channels due for polling."
            try:
                db.finish_sync_run(sync_id, "success", message, 0, 0)
            except Exception:
                logger.exception("RSS sync run could not finish")
            return {
                "ok": True,
                "sync_run_id": sync_id,
                "subscriptions_count": 0,
                "entries_count": 0,
                "message": message,
            }
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "OpenFeed RSS poller"}) as client:
            for channel in channels:
                sub_id = channel.get("id")
                try:
                    youtube_channel_id = (channel.get("youtube_channel_id") or "").strip() or None
                    stored_feed_url = (channel.get("feed_url") or "").strip() or None
                    # Feed URL fallback: rows that carry a feed URL but no
                    # channel id are polled directly.
                    url = channel_feed_url(youtube_channel_id) if youtube_channel_id else stored_feed_url
                    if not url:
                        db.record_rss_fetch(sub_id, ok=False)
                        continue
                    subscriptions_count += 1
                    result = fetch_channel_feed(
                        client,
                        url,
                        etag=channel.get("etag"),
                        last_modified=channel.get("last_modified"),
                    )
                    if result.status == 304:
                        # Not modified: keep the stored validators so a bare
                        # 304 without validator headers does not wipe them.
                        db.record_rss_fetch(
                            sub_id,
                            etag=result.etag or channel.get("etag"),
                            last_modified=result.last_modified or channel.get("last_modified"),
                            ok=True,
                            ttl_seconds=ttl_seconds,
                        )
                        continue
                    if result.status != 200 or result.text is None:
                        db.record_rss_fetch(sub_id, ok=False)
                        continue
                    for entry in parse_channel_feed(result.text, url):
                        channel_id = db.upsert_channel(
                            entry["channel_title"], entry.get("youtube_channel_id"), entry.get("feed_url")
                        )
                        video_id = db.upsert_video(entry, channel_id, entry["youtube_video_id"])
                        entry_channel = entry.get("youtube_channel_id")
                        subscription_id = (
                            db.subscription_id_for_channel(entry_channel) if entry_channel else None
                        )
                        entry["subscription_id"] = subscription_id
                        db.upsert_entry(entry, video_id, subscription_id)
                        entries_count += 1
                    db.record_rss_fetch(
                        sub_id,
                        etag=result.etag or channel.get("etag"),
                        last_modified=result.last_modified or channel.get("last_modified"),
                        ok=True,
                        ttl_seconds=ttl_seconds,
                    )
                except Exception:
                    logger.exception("RSS poll failed for channel %s", sub_id)
                    try:
                        db.record_rss_fetch(sub_id, ok=False)
                    except Exception:
                        logger.exception("RSS failure bookkeeping failed for channel %s", sub_id)
                    continue

        message = f"Polled {subscriptions_count} channels and stored {entries_count} entries via RSS."
        db.finish_sync_run(sync_id, "success", message, subscriptions_count, entries_count)
        logger.info(message)
        return {
            "ok": True,
            "sync_run_id": sync_id,
            "subscriptions_count": subscriptions_count,
            "entries_count": entries_count,
            "message": message,
        }
    except Exception as exc:
        message = f"RSS sync failed: {exc}"
        try:
            db.finish_sync_run(sync_id, "error", message, subscriptions_count, entries_count)
        except Exception:
            logger.exception("RSS sync run could not finish")
        logger.exception(message)
        return {"ok": False, "sync_run_id": sync_id, "status": "error", "message": message}
