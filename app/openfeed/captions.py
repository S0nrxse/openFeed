"""Caption tracks for the player, served as WebVTT from OpenFeed's own origin.

The browser never fetches a caption from YouTube or Invidious. The player asks
`/api/captions/{id}` for the list and `/api/captions/{id}/{track}.vtt` for a track, and
this module does the fetching server-side.

Where the tracks come from, in order:

* **Invidious.** `/api/v1/captions/{id}` lists the tracks. The text itself is read through
  Invidious companion, the same signed `/companion/api/v1/captions/...` URL Invidious puts
  in its own embed's `<track>` elements, because Invidious's direct caption fetch is the
  hop YouTube rate-limits first. The direct URL stays as the second try.
* **yt-dlp**, when Invidious lists nothing or is unreachable. Manual subtitles are offered
  in every language the video has. Of YouTube's automatic captions only the original
  language is offered: the other hundred-odd are machine translations of it.

YouTube's automatic captions arrive in a karaoke format: every line is sent twice, once
being "typed" word by word with inline timestamps and once as a 10 ms cue that scrolls it
up. Rendered as is, each line shows twice. `clean_vtt` reduces that to plain cues that
show each line once.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import Any

import httpx

from . import netguard, outbound, ytdlp
from .config import get_settings

logger = logging.getLogger("openfeed.captions")

FETCH_TIMEOUT = 12.0
LIST_TTL = 1800.0
EMPTY_LIST_TTL = 120.0
VTT_TTL = 6 * 3600.0
MAX_VTT_BYTES = 4_000_000
_CACHE_SIZE = 96

AUTO_SUFFIX = " (auto-generated)"


class CaptionError(RuntimeError):
    """No source produced usable caption text."""


class _TTLCache:
    """A small LRU with per-entry expiry. Caption lists and texts are cheap to refetch."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            entry = self._items.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: Any, value: Any, ttl: float) -> None:
        with self._lock:
            self._items[key] = (time.time() + ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_lists = _TTLCache(_CACHE_SIZE)
_texts = _TTLCache(_CACHE_SIZE)


def clear_cache() -> None:
    _lists.clear()
    _texts.clear()


# --- Listing -----------------------------------------------------------------------------

_TRACK_TAG = re.compile(r"<track\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r'([a-zA-Z_:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def embed_track_sources(page: str) -> dict[str, str]:
    """label -> src for every caption <track> in an Invidious embed page."""
    sources: dict[str, str] = {}
    for tag in _TRACK_TAG.findall(page or ""):
        attributes = {
            match.group(1).lower(): html.unescape(match.group(2) if match.group(2) is not None else match.group(3))
            for match in _ATTRIBUTE.finditer(tag)
        }
        label = attributes.get("label", "").strip()
        src = attributes.get("src", "").strip()
        if label and src and attributes.get("kind", "captions") in {"captions", "subtitles"}:
            sources.setdefault(label, src)
    return sources


def _browser_encode(path: str) -> str:
    """Encode a URL the way a browser does with an attribute value: spaces and non-ASCII
    are escaped, and everything that already means something in a URL is left alone. The
    companion's signed check value can hold '+' and '=', which must arrive untouched."""
    return urllib.parse.quote(path, safe="/?&=+%:@!$'()*,;~-._")


def split_label(label: str) -> tuple[str, bool]:
    """'English (auto-generated)' -> ('English', True)."""
    text = str(label or "").strip()
    if text.lower().endswith(AUTO_SUFFIX):
        return text[: -len(AUTO_SUFFIX)].strip() or text, True
    return text, False


def _invidious_tracks(video_id: str) -> list[dict[str, Any]]:
    base = get_settings().invidious_base_url.rstrip("/")
    quoted = urllib.parse.quote(video_id, safe="")
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
        listing = client.get(f"{base}/api/v1/captions/{quoted}")
        listing.raise_for_status()
        captions = (listing.json() or {}).get("captions") or []
        if not captions:
            return []
        embed_sources: dict[str, str] = {}
        try:
            page = client.get(f"{base}/embed/{quoted}")
            if page.status_code == 200:
                embed_sources = embed_track_sources(page.text)
        except httpx.HTTPError as exc:
            logger.info("embed page for %s unavailable for caption signing: %s", video_id, exc)
    tracks = []
    for caption in captions:
        raw_label = str(caption.get("label") or "").strip()
        if not raw_label:
            continue
        name, auto = split_label(raw_label)
        upstream = []
        signed = embed_sources.get(raw_label)
        if signed:
            upstream.append(base + _browser_encode(signed if signed.startswith("/") else f"/{signed}"))
        direct = str(caption.get("url") or "")
        if direct.startswith("/"):
            upstream.append(base + direct)
        if not upstream:
            continue
        tracks.append({
            "label": name,
            "language": str(caption.get("languageCode") or caption.get("language_code") or "").strip(),
            "auto": auto,
            "source": "invidious",
            "upstream": upstream,
        })
    return tracks


def _vtt_url(formats: list[dict[str, Any]]) -> str:
    for fmt in formats or []:
        if str(fmt.get("ext") or "") == "vtt" and fmt.get("url"):
            return str(fmt["url"])
    return ""


def _format_name(formats: list[dict[str, Any]], fallback: str) -> str:
    return next((str(fmt["name"]) for fmt in formats or [] if fmt.get("name")), fallback)


def _youtube_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "youtube.com" or host.endswith(".youtube.com")


def ytdlp_tracks_from_info(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Tracks from yt-dlp's metadata: every manual subtitle, and the auto track only in
    the video's own language (the rest are YouTube's machine translations of it)."""
    tracks = []
    for language, formats in (info.get("subtitles") or {}).items():
        if language == "live_chat":
            continue
        url = _vtt_url(formats)
        if not url or not _youtube_host(url):
            continue
        tracks.append({"label": _format_name(formats, language), "language": language, "auto": False, "source": "ytdlp", "upstream": [url]})
    automatic = info.get("automatic_captions") or {}
    keys = [key for key in automatic if key.endswith("-orig")]
    if not keys and info.get("language") in automatic:
        keys = [info["language"]]
    for key in keys:
        formats = automatic[key]
        url = _vtt_url(formats)
        if not url or not _youtube_host(url):
            continue
        language = key[: -len("-orig")] if key.endswith("-orig") else key
        name = re.sub(r"\s*\((original|auto-generated)\)\s*$", "", _format_name(formats, language), flags=re.IGNORECASE) or language
        tracks.append({"label": name, "language": language, "auto": True, "source": "ytdlp", "upstream": [url]})
    return tracks


def _assign_ids(tracks: list[dict[str, Any]]) -> None:
    seen: dict[str, int] = {}
    for track in tracks:
        base = re.sub(r"[^A-Za-z0-9-]+", "-", f"{track.get('language') or 'und'}{'-auto' if track.get('auto') else ''}").strip("-") or "track"
        count = seen.get(base, 0) + 1
        seen[base] = count
        track["id"] = base if count == 1 else f"{base}-{count}"


def _sort(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Manual before automatic, then by name, so the menu reads in a stable order."""
    return sorted(tracks, key=lambda track: (bool(track.get("auto")), str(track.get("label") or "").lower()))


def _internal_tracks(video_id: str) -> list[dict[str, Any]]:
    cached = _lists.get(video_id)
    if cached is not None:
        return cached
    tracks: list[dict[str, Any]] = []
    if get_settings().invidious_base_url:
        try:
            tracks = _invidious_tracks(video_id)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Invidious caption list for %s failed: %s", video_id, exc)
    if not tracks and ytdlp.available():
        try:
            tracks = ytdlp_tracks_from_info(ytdlp.probe(video_id))
        except ytdlp.YtdlpError as exc:
            logger.info("yt-dlp caption list for %s failed: %s", video_id, exc)
    tracks = _sort(tracks)
    _assign_ids(tracks)
    _lists.put(video_id, tracks, LIST_TTL if tracks else EMPTY_LIST_TTL)
    return tracks


def list_tracks(video_id: str) -> list[dict[str, Any]]:
    """The tracks the player can offer, each with an OpenFeed URL. Never raises."""
    quoted = urllib.parse.quote(video_id, safe="")
    return [
        {
            "id": track["id"],
            "label": track["label"],
            "language": track["language"],
            "auto": bool(track["auto"]),
            "source": track["source"],
            "src": f"/api/captions/{quoted}/{track['id']}.vtt",
        }
        for track in _internal_tracks(video_id)
    ]


# --- Fetching and cleaning -----------------------------------------------------------------

def _fetch(url: str, source: str) -> str:
    if source == "ytdlp":
        # Signed URLs straight from yt-dlp. Checked against the host allowlist when listed,
        # and here against an internal address, redirects included.
        with outbound.client(timeout=FETCH_TIMEOUT) as client:
            response = netguard.safe_get(client, url)
    else:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
    response.raise_for_status()
    if len(response.content) > MAX_VTT_BYTES:
        raise CaptionError("caption file is larger than OpenFeed accepts")
    return response.text


def fetch_vtt(video_id: str, track_id: str) -> str:
    """The cleaned WebVTT for one track. LookupError for an unknown track."""
    cached = _texts.get((video_id, track_id))
    if cached is not None:
        return cached
    track = next((item for item in _internal_tracks(video_id) if item["id"] == track_id), None)
    if not track:
        raise LookupError(track_id)
    problems = []
    for url in track["upstream"]:
        try:
            text = _fetch(url, track["source"])
        except (httpx.HTTPError, netguard.UnsafeExternalUrl, CaptionError) as exc:
            problems.append(str(exc) or exc.__class__.__name__)
            continue
        if "-->" not in text:
            problems.append("the source answered with no cues")
            continue
        cleaned = clean_vtt(text, auto=bool(track["auto"]))
        _texts.put((video_id, track_id), cleaned, VTT_TTL)
        return cleaned
    raise CaptionError("; ".join(problems) or "no source for this track")


_TIMING = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})(.*)$")
_INLINE_TIME = re.compile(r"<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}>")
_ANY_TAG = re.compile(r"</?([a-zA-Z]+)(?:[.\s][^>]*)?>")
_KEEP_TAGS = {"i", "b", "u"}


def _seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _stamp(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _clean_text(line: str) -> str:
    line = _INLINE_TIME.sub("", line)
    line = _ANY_TAG.sub(lambda match: match.group(0) if match.group(1).lower() in _KEEP_TAGS else "", line)
    return re.sub(r"\s+", " ", line).strip()


def parse_cues(text: str) -> list[tuple[float, float, str, list[str]]]:
    """(start, end, settings, lines) for every cue in a WebVTT document."""
    cues = []
    # Only a truly empty line ends a cue. YouTube's automatic captions put a line holding a
    # single space inside cues, and treating that as a separator loses the cue's text.
    blocks = re.split(r"\n{2,}", str(text or "").replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = block.split("\n")
        for index, line in enumerate(lines):
            match = _TIMING.match(line)
            if match:
                body = [_clean_text(item) for item in lines[index + 1:]]
                cues.append((_seconds(match.group(1)), _seconds(match.group(2)), match.group(3).strip(), [item for item in body if item]))
                break
    return cues


def clean_vtt(text: str, auto: bool = False) -> str:
    """Plain WebVTT: no inline word timings, no styling blocks, and for automatic captions
    each line shown once rather than typed out and then scrolled up as a duplicate."""
    output = []
    previous: list[str] = []
    for start, end, settings, lines in parse_cues(text):
        if end <= start:
            continue
        if auto:
            # The 10 ms cues only exist to scroll the previous line up.
            if end - start < 0.05:
                continue
            # A rolling cue repeats the line before it as its first line.
            while lines and previous and lines[0] == previous[-1]:
                lines = lines[1:]
            settings = ""
        if not lines:
            continue
        previous = lines
        output.append(f"{_stamp(start)} --> {_stamp(end)}{(' ' + settings) if settings else ''}\n" + "\n".join(lines))
    return "WEBVTT\n\n" + "\n\n".join(output) + ("\n" if output else "")


# --- Choosing a track --------------------------------------------------------------------

def pick_track(tracks: list[dict[str, Any]], language: str, auto: bool) -> dict[str, Any] | None:
    """The track matching a remembered choice: same language and kind first, then the same
    base language (en for en-GB), manual before automatic. None rather than a guess at an
    unrelated language."""
    wanted = str(language or "").strip().lower()
    if not wanted:
        return None
    base = wanted.split("-")[0]
    exact = [track for track in tracks if str(track.get("language") or "").lower() == wanted]
    for track in exact:
        if bool(track.get("auto")) == bool(auto):
            return track
    if exact:
        return exact[0]
    related = [track for track in tracks if str(track.get("language") or "").lower().split("-")[0] == base]
    related.sort(key=lambda track: bool(track.get("auto")))
    return related[0] if related else None
