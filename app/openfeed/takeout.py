"""Google Takeout import: turn a YouTube export into personalization signal.

A new OpenFeed install knows nothing about you, so its recommendations start from a
handful of subscriptions and get better only as you watch things. A Takeout archive
already contains years of that evidence — what you watched, how often, whose channels,
what you searched for — and importing it skips the cold start entirely.

Everything here runs locally. The archive is parsed in this process, the derived counts
go into this database, and nothing is sent anywhere. Watch history is about as sensitive
as personal data gets, so the parser also refuses to hoard it: only the fields that feed
ranking are kept, and the archive itself is never written to disk.

Takeout's layout has moved around over the years and is localised, so files are located
by suffix rather than by an exact path.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger("openfeed.takeout")

# Caps exist so a decade-long history cannot exhaust memory or fill the database with
# rows that add nothing — the most recent entries carry nearly all the signal.
MAX_WATCH_ENTRIES = 20000
MAX_SEARCH_ENTRIES = 5000
MAX_SUBSCRIPTIONS = 5000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_MEMBER_BYTES = 512 * 1024 * 1024

_VIDEO_ID = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
_SHORTS_ID = re.compile(r"/shorts/([A-Za-z0-9_-]{11})")
_CHANNEL_ID = re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})")


class TakeoutError(RuntimeError):
    """The upload was not a usable YouTube Takeout archive."""


def _video_id_from(url: str) -> str:
    match = _VIDEO_ID.search(url or "") or _SHORTS_ID.search(url or "")
    return match.group(1) if match else ""


def _channel_id_from(url: str) -> str:
    match = _CHANNEL_ID.search(url or "")
    return match.group(1) if match else ""


def _parse_time(raw: str) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        # Takeout writes RFC 3339 with a trailing Z that fromisoformat rejects on older
        # Pythons; normalising it is cheaper than a dateutil dependency.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [item for item in archive.infolist() if not item.is_dir()]


def _find(archive: zipfile.ZipFile, *suffixes: str) -> zipfile.ZipInfo | None:
    """Locate a file by path suffix.

    Takeout paths carry a localised folder name ("YouTube and YouTube Music", but
    translated), and the layout has changed between exports, so matching on the tail of
    the path is the only thing that holds across archives.
    """
    for item in _members(archive):
        lowered = item.filename.lower().replace("\\", "/")
        if any(lowered.endswith(suffix) for suffix in suffixes):
            return item
    return None


def _read(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    if member.file_size > MAX_MEMBER_BYTES:
        raise TakeoutError(f"{member.filename} is {member.file_size // (1024 * 1024)} MB, which is larger than this import supports")
    with archive.open(member) as handle:
        return handle.read()


def parse_subscriptions(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    """Channels from subscriptions.csv."""
    member = _find(archive, "subscriptions.csv")
    if not member:
        return []
    text = _read(archive, member).decode("utf-8-sig", errors="replace")
    rows: list[dict[str, str]] = []
    for row in csv.DictReader(io.StringIO(text)):
        # Header names are localised, so columns are identified by shape: the channel id
        # is the only field that looks like one.
        values = [str(value or "").strip() for value in row.values()]
        channel_id = next((value for value in values if value.startswith("UC") and len(value) == 24), "")
        if not channel_id:
            channel_id = next((_channel_id_from(value) for value in values if _channel_id_from(value)), "")
        if not channel_id:
            continue
        title = next((value for value in values if value and not value.startswith(("UC", "http"))), channel_id)
        rows.append({"youtube_channel_id": channel_id, "title": title})
        if len(rows) >= MAX_SUBSCRIPTIONS:
            break
    return rows


def _history_entries(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        return (item for item in payload if isinstance(item, dict))
    return iter(())


def parse_watch_history(archive: zipfile.ZipFile) -> dict[str, Any]:
    """Watched videos and per-channel counts, newest first."""
    member = _find(archive, "watch-history.json", "watch history.json")
    if not member:
        if _find(archive, "watch-history.html", "watch history.html"):
            raise TakeoutError(
                "Your watch history is in HTML format. Re-export from Takeout and choose "
                "JSON for the history files — HTML cannot be imported reliably."
            )
        return {"videos": [], "channels": [], "total": 0}

    try:
        payload = json.loads(_read(archive, member).decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise TakeoutError("watch-history.json could not be parsed") from exc

    videos: list[dict[str, Any]] = []
    channel_counts: Counter[str] = Counter()
    channel_titles: dict[str, str] = {}
    channel_last: dict[str, datetime] = {}
    seen: set[str] = set()
    total = 0

    for entry in _history_entries(payload):
        total += 1
        video_id = _video_id_from(str(entry.get("titleUrl") or ""))
        if not video_id:
            continue
        watched_at = _parse_time(str(entry.get("time") or ""))

        # Channel comes from the first subtitle; a removed video has none.
        channel_id = ""
        channel_title = ""
        subtitles = entry.get("subtitles")
        if isinstance(subtitles, list) and subtitles and isinstance(subtitles[0], dict):
            channel_title = str(subtitles[0].get("name") or "").strip()
            channel_id = _channel_id_from(str(subtitles[0].get("url") or ""))

        if channel_id:
            channel_counts[channel_id] += 1
            if channel_title:
                channel_titles.setdefault(channel_id, channel_title)
            if watched_at and (channel_id not in channel_last or watched_at > channel_last[channel_id]):
                channel_last[channel_id] = watched_at

        if video_id not in seen and len(videos) < MAX_WATCH_ENTRIES:
            seen.add(video_id)
            videos.append({
                "youtube_video_id": video_id,
                "watched_at": watched_at,
                "youtube_channel_id": channel_id,
                "channel_title": channel_title,
            })

    channels = [
        {
            "youtube_channel_id": channel_id,
            "title": channel_titles.get(channel_id, ""),
            "watch_count": count,
            "last_watched_at": channel_last.get(channel_id),
        }
        for channel_id, count in channel_counts.most_common()
    ]
    return {"videos": videos, "channels": channels, "total": total}


def parse_search_history(archive: zipfile.ZipFile) -> list[str]:
    """Search terms, most frequent first — a direct read on what you look for."""
    member = _find(archive, "search-history.json", "search history.json")
    if not member:
        return []
    try:
        payload = json.loads(_read(archive, member).decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []

    terms: Counter[str] = Counter()
    for entry in _history_entries(payload):
        title = str(entry.get("title") or "")
        # Localised prefix ("Searched for x"), so the query is taken as the tail.
        query = title.split(" ", 2)[-1] if title.count(" ") >= 2 else title
        query = query.strip().lower()
        if 2 < len(query) <= 80 and not query.startswith("http"):
            terms[query] += 1
        if len(terms) >= MAX_SEARCH_ENTRIES:
            break
    return [term for term, _ in terms.most_common(200)]


def inspect(data: bytes) -> dict[str, Any]:
    """Parse an uploaded archive into everything the importer needs.

    Raises TakeoutError with a message aimed at the person who exported the archive,
    because "not a zip file" is not an actionable thing to tell somebody.
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise TakeoutError("That archive is larger than 2 GB. Export only YouTube data from Takeout, not your whole account.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TakeoutError("That file is not a .zip archive. Upload the archive Takeout gave you, unmodified.") from exc

    with archive:
        subscriptions = parse_subscriptions(archive)
        watch = parse_watch_history(archive)
        searches = parse_search_history(archive)

        if not subscriptions and not watch["videos"] and not searches:
            names = [item.filename for item in _members(archive)][:5]
            raise TakeoutError(
                "No YouTube data found in that archive. Make sure you selected YouTube "
                f"in Takeout and chose JSON for history. Saw: {', '.join(names) or 'an empty archive'}"
            )

    return {
        "subscriptions": subscriptions,
        "watched_videos": watch["videos"],
        "watched_channels": watch["channels"],
        "search_terms": searches,
        "watch_entries_seen": watch["total"],
    }


def summarize(parsed: dict[str, Any]) -> dict[str, Any]:
    channels = parsed.get("watched_channels") or []
    return {
        "subscriptions": len(parsed.get("subscriptions") or []),
        "watched_videos": len(parsed.get("watched_videos") or []),
        "watched_channels": len(channels),
        "search_terms": len(parsed.get("search_terms") or []),
        "watch_entries_seen": int(parsed.get("watch_entries_seen") or 0),
        "top_channels": [
            {"title": item.get("title") or item.get("youtube_channel_id"), "watch_count": item.get("watch_count")}
            for item in channels[:10]
        ],
    }
