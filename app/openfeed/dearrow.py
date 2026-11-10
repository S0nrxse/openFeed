"""DeArrow titles and thumbnails, read-only and privacy-first.

DeArrow is the SponsorBlock team's crowdsourced database of better titles and thumbnails:
people submit a plain description in place of a clickbait title, and a moment of the video
in place of a misleading thumbnail. It follows the SponsorBlock integration exactly: off
unless both the deployment and the user turn it on, server-side, read-only, cached, a
fixed endpoint, a timeout, and lookups by hash prefix.

The privacy mechanism is the same one too. OpenFeed sends DeArrow the **first four hex
characters of SHA-256(video id)** and gets back the branding for every video sharing that
prefix, usually a hundred or more. DeArrow never learns which video a card was for. The
response is cached per prefix, so one request answers every later card in that bucket.

Thumbnails: DeArrow publishes a *timestamp*, not an image. Its own extension fetches the
frame from DeArrow's thumbnail service by video id, which would tell a third party exactly
which video is on screen. OpenFeed never does that. When the user allows it, this server
cuts the frame itself with ffmpeg from the video stream it already plays through the
configured Invidious, caches the JPEG on disk, and serves it from its own origin. Frames
are made one at a time on a background thread, so a page never waits for one: a card
shows the community thumbnail once its frame exists, and the original until then.

Deliberately not implemented: submitting titles or thumbnails, voting, or DeArrow's
randomTime fallback (a random frame for every video that has no submission, which would
mean cutting a frame for nearly every card on every page).
"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Iterable

import httpx

from . import outbound
from .config import get_settings
from .sponsorblock import hash_prefix, valid_video_id

logger = logging.getLogger("openfeed.dearrow")

# Constant for the same reason SponsorBlock's is: a configurable endpoint would turn a
# privacy setting into a way to point viewing interest at an arbitrary host.
DEARROW_ENDPOINT = "https://sponsor.ajay.app/api/branding"
DEARROW_TIMEOUT_SECONDS = 5.0
# A failed prefix is not asked about again for this long, so an outage costs one request
# per bucket per minute rather than one per card per page.
FAILURE_BACKOFF_SECONDS = 60
MAX_LOOKUP_IDS = 120
_LOOKUP_WORKERS = 8
# A page of cards waits at most this long for uncached buckets.
LOOKUP_BUDGET_SECONDS = 6.0
_CACHE_MAX_PREFIXES = 8192

CASINGS = ("", "lowercase")


class DeArrowError(RuntimeError):
    """DeArrow could not be reached, or answered with something unusable."""


# ---------------------------------------------------------------------------
# Choosing what to show
# ---------------------------------------------------------------------------


def _trusted(item: Any) -> bool:
    """DeArrow's rule: an entry shows only if it is locked or its score is not negative.

    Anything else "is considered untrusted and is only to be shown in the voting box".
    """
    if not isinstance(item, dict):
        return False
    votes = item.get("votes")
    return bool(item.get("locked")) or (isinstance(votes, (int, float)) and votes >= 0)


def pick_branding(entry: Any) -> dict[str, Any]:
    """The title and thumbnail timestamp to show for one video, or None for each.

    The API orders both lists best first, so only the first entry is considered. An entry
    marked `original` means the community voted for the creator's own title or thumbnail,
    which is a result too: nothing is replaced.
    """
    result: dict[str, Any] = {"title": None, "thumbnail": None}
    if not isinstance(entry, dict):
        return result
    titles = entry.get("titles") if isinstance(entry.get("titles"), list) else []
    if titles and _trusted(titles[0]) and not titles[0].get("original"):
        title = str(titles[0].get("title") or "").strip()
        if title:
            result["title"] = title[:300]
    thumbnails = entry.get("thumbnails") if isinstance(entry.get("thumbnails"), list) else []
    if thumbnails and _trusted(thumbnails[0]) and not thumbnails[0].get("original"):
        stamp = thumbnails[0].get("timestamp")
        if isinstance(stamp, (int, float)) and 0 <= float(stamp) < 86400:
            result["thumbnail"] = round(float(stamp), 3)
    return result


def parse_branding(payload: Any) -> dict[str, dict[str, Any]]:
    """Every video in a hash-prefix response that has something to show.

    Most of a bucket has no submissions, and only the chosen title and timestamp are kept,
    so a cached prefix costs a few hundred bytes rather than the ~18 KB the API returns.
    """
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return found
    for video_id, entry in payload.items():
        if not valid_video_id(str(video_id)):
            continue
        chosen = pick_branding(entry)
        if chosen["title"] or chosen["thumbnail"] is not None:
            found[str(video_id)] = chosen
    return found


# ---------------------------------------------------------------------------
# Title formatting, as DeArrow's own extension does it
# ---------------------------------------------------------------------------

# DeArrow lets a submitter write ">" before a word to mean "do not auto-format this word".
# Every formatting mode removes the marker afterwards; this is DeArrow's own expression.
_KEEP_MARKER = re.compile(r"(^|\s)>(\S)")
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")


def clean_title(title: str) -> str:
    return _KEEP_MARKER.sub(r"\1\2", str(title or "")).strip()


def _lower_word(word: str, turkic: bool) -> str:
    if turkic:
        word = word.replace("I", "ı").replace("İ", "i")
    return word.lower()


def to_lowercase(title: str) -> str:
    """DeArrow's "lowercase" title format.

    Matching its `toLowerCaseTitle`: every word is lowercased, including acronyms, custom
    capitalisation and words a submitter marked with ">" (the marker is then removed). The
    one exception is a word containing Greek letters in a title that is not Greek, which
    is kept as written so symbols such as "Ω" or "Δ" survive. Text containing a dotless or
    dotted I is lowercased with Turkish rules, so "I" becomes "ı" rather than "i".
    """
    text = str(title or "")
    words = text.split(" ")
    greek_letters = len(_GREEK.findall(text))
    letters = sum(1 for ch in text if ch.isalpha()) or 1
    is_greek = greek_letters / letters > 0.5
    turkic = "ı" in text or "İ" in text
    out = [word if (not is_greek and _GREEK.search(word)) else _lower_word(word, turkic) for word in words]
    return clean_title(" ".join(out))


def format_title(title: str, casing: str = "") -> str:
    """A title as the user asked to see it. "" keeps the submitted casing."""
    if casing == "lowercase":
        return to_lowercase(title)
    return clean_title(title)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

# prefix -> (expires_at, {video_id: branding}) ; a failure is cached as None for a minute.
_cache: dict[str, tuple[float, dict[str, dict[str, Any]] | None]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(prefix: str) -> tuple[bool, dict[str, dict[str, Any]] | None]:
    with _cache_lock:
        entry = _cache.get(prefix)
    if entry and time.time() < entry[0]:
        return True, entry[1]
    return False, None


def _store(prefix: str, value: dict[str, dict[str, Any]] | None, ttl: float) -> None:
    with _cache_lock:
        _cache[prefix] = (time.time() + ttl, value)
        if len(_cache) > _CACHE_MAX_PREFIXES:
            now = time.time()
            for key, (expires, _) in list(_cache.items()):
                if expires <= now:
                    _cache.pop(key, None)
            while len(_cache) > _CACHE_MAX_PREFIXES:
                _cache.pop(next(iter(_cache)), None)


def fetch_prefix(prefix: str) -> dict[str, dict[str, Any]]:
    """One hash-prefix bucket from DeArrow. Raises DeArrowError on transport failure."""
    if not re.fullmatch(r"[0-9a-f]{4}", prefix or ""):
        return {}
    headers = {"accept": "application/json", "user-agent": "OpenFeed (+https://github.com/S0nrxse/openFeed)"}
    try:
        with outbound.client(timeout=DEARROW_TIMEOUT_SECONDS, follow_redirects=False, headers=headers) as client:
            response = client.get(f"{DEARROW_ENDPOINT}/{prefix}")
    except httpx.HTTPError as exc:
        raise DeArrowError(f"DeArrow request failed: {exc}") from exc
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        raise DeArrowError(f"DeArrow returned HTTP {response.status_code}")
    try:
        return parse_branding(response.json())
    except ValueError:
        return {}


def lookup(video_ids: Iterable[str], *, cached_only: bool = False) -> dict[str, dict[str, Any]]:
    """Branding for the given videos, keyed by video id. Videos with none are left out.

    Buckets that are not cached are fetched side by side, each under the request timeout.
    `cached_only` answers from the cache alone, for callers that must not wait.
    """
    ids = [vid for vid in dict.fromkeys(str(v or "") for v in video_ids) if valid_video_id(vid)][:MAX_LOOKUP_IDS]
    by_prefix: dict[str, list[str]] = {}
    for vid in ids:
        by_prefix.setdefault(hash_prefix(vid), []).append(vid)

    buckets: dict[str, dict[str, dict[str, Any]]] = {}
    missing: list[str] = []
    for prefix in by_prefix:
        hit, value = _cached(prefix)
        if hit:
            buckets[prefix] = value or {}
        else:
            missing.append(prefix)

    if missing and not cached_only:
        ttl = max(60, int(get_settings().dearrow_cache_seconds or 0))

        def load(prefix: str) -> dict[str, dict[str, Any]] | None:
            # Stored here rather than by the caller, so a bucket that finishes after the
            # budget below still lands in the cache for the next page.
            try:
                value: dict[str, dict[str, Any]] | None = fetch_prefix(prefix)
            except DeArrowError as exc:
                logger.info("DeArrow lookup failed for one bucket: %s", exc)
                value = None
            _store(prefix, value, ttl if value is not None else FAILURE_BACKOFF_SECONDS)
            return value

        pool = ThreadPoolExecutor(max_workers=min(_LOOKUP_WORKERS, len(missing)), thread_name_prefix="dearrow")
        futures = {pool.submit(load, prefix): prefix for prefix in missing}
        done, _ = wait(futures, timeout=LOOKUP_BUDGET_SECONDS)
        # Buckets still waiting for a worker are dropped; ones already in flight finish in
        # the background and are cached. Either way this answer does not wait for them.
        pool.shutdown(wait=False, cancel_futures=True)
        for future in done:
            buckets[futures[future]] = future.result() or {}

    found: dict[str, dict[str, Any]] = {}
    for prefix, vids in by_prefix.items():
        bucket = buckets.get(prefix) or {}
        for vid in vids:
            if vid in bucket:
                found[vid] = dict(bucket[vid])
    return found


def enabled(user_key: str = "local") -> bool:
    """Both the deployment and the user must allow it, exactly as with SponsorBlock."""
    from . import db

    if not get_settings().dearrow_enabled:
        return False
    try:
        return bool(db.get_feed_preferences(user_key).get("dearrow_enabled"))
    except Exception:
        return False


def preferences(user_key: str = "local") -> dict[str, Any]:
    from . import db

    try:
        prefs = db.get_feed_preferences(user_key)
    except Exception:
        prefs = {}
    casing = str(prefs.get("dearrow_casing") or "")
    return {
        "enabled": bool(get_settings().dearrow_enabled and prefs.get("dearrow_enabled")),
        "casing": casing if casing in CASINGS else "",
        "thumbnails": bool(prefs.get("dearrow_thumbnails", True)),
    }


def branding_for(video_ids: Iterable[str], user_key: str = "local") -> dict[str, Any]:
    """What the page needs: a display title per video and, where a frame exists, its URL."""
    prefs = preferences(user_key)
    if not prefs["enabled"]:
        return {"ok": False, "reason": "disabled", "items": {}}
    found = lookup(video_ids)
    items: dict[str, dict[str, Any]] = {}
    for vid, branding in found.items():
        item: dict[str, Any] = {}
        if branding.get("title"):
            item["title"] = format_title(branding["title"], prefs["casing"])
        stamp = branding.get("thumbnail")
        if prefs["thumbnails"] and stamp is not None:
            if frame_path(vid, stamp).exists():
                item["thumbnail"] = thumbnail_url(vid)
            else:
                request_frame(vid, stamp)
        if item:
            items[vid] = item
    return {"ok": True, "items": items, "casing": prefs["casing"]}


# ---------------------------------------------------------------------------
# Thumbnail frames, cut on this server
# ---------------------------------------------------------------------------

FRAME_WIDTH = 480
_FRAME_TIMEOUT_SECONDS = 40
_QUEUE_LIMIT = 200
_frames: "queue.Queue[tuple[str, float]]" = queue.Queue(maxsize=_QUEUE_LIMIT)
_pending: set[str] = set()
_failed: dict[str, float] = {}
_frame_lock = threading.Lock()
_worker: threading.Thread | None = None
# Preferred video-only streams, smallest that still looks right at card size first:
# 480p and 360p H.264, then the progressive 360p file.
_FRAME_ITAGS = ("135", "134", "136", "18")


def cache_dir() -> Path:
    return Path(get_settings().dearrow_cache_dir)


def thumbnail_url(video_id: str) -> str:
    return f"/api/dearrow/thumbnail/{video_id}.jpg"


def frame_path(video_id: str, timestamp: float) -> Path:
    """One file per video and timestamp, so a changed submission makes a new frame."""
    stamp = f"{float(timestamp):.3f}".replace(".", "_")
    return cache_dir() / f"{video_id}-{stamp}.jpg"


def current_frame(video_id: str) -> Path | None:
    """The newest frame cut for a video, whatever its timestamp."""
    if not valid_video_id(video_id):
        return None
    try:
        frames = sorted(cache_dir().glob(f"{video_id}-*.jpg"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return frames[0] if frames else None


def request_frame(video_id: str, timestamp: float) -> bool:
    """Queue a frame to be cut. Never blocks; a full queue simply drops the request."""
    if not valid_video_id(video_id):
        return False
    with _frame_lock:
        if video_id in _pending or time.time() < _failed.get(video_id, 0):
            return False
        try:
            _frames.put_nowait((video_id, float(timestamp)))
        except queue.Full:
            return False
        _pending.add(video_id)
    _ensure_worker()
    return True


def _ensure_worker() -> None:
    global _worker
    with _frame_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_frame_loop, name="dearrow-frames", daemon=True)
        _worker.start()


def _frame_loop() -> None:
    while True:
        video_id, timestamp = _frames.get()
        try:
            cut_frame(video_id, timestamp)
        except Exception as exc:  # noqa: BLE001 - one bad video must not stop the worker
            logger.info("DeArrow frame for %s failed: %s", video_id, exc)
            with _frame_lock:
                _failed[video_id] = time.time() + 3600
        finally:
            with _frame_lock:
                _pending.discard(video_id)


def stream_url(video_id: str) -> tuple[str, float]:
    """A small video stream for this video through the configured Invidious, and its length.

    `local=true` makes Invidious proxy the stream, so the request to YouTube's media servers
    comes from Invidious exactly as it does for playback. Only the path and query of the
    returned URL are kept; the host is always the configured Invidious, whatever the
    instance believes its public address is.
    """
    settings = get_settings()
    base = str(settings.invidious_base_url or "").rstrip("/")
    if not base:
        raise DeArrowError("Invidious is not configured")
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.get(f"{base}/api/v1/videos/{video_id}", params={"local": "true", "fields": "adaptiveFormats,formatStreams,lengthSeconds"})
    if response.status_code != 200:
        raise DeArrowError(f"Invidious returned HTTP {response.status_code}")
    data = response.json()
    formats = {str(item.get("itag")): item for item in (data.get("adaptiveFormats") or []) + (data.get("formatStreams") or []) if isinstance(item, dict)}
    for itag in _FRAME_ITAGS:
        url = str((formats.get(itag) or {}).get("url") or "")
        if url:
            parts = urllib.parse.urlsplit(url)
            if not parts.path.startswith("/videoplayback"):
                continue
            return f"{base}{parts.path}?{parts.query}", float(data.get("lengthSeconds") or 0)
    raise DeArrowError("no stream small enough to cut a frame from")


def cut_frame(video_id: str, timestamp: float) -> Path:
    """Cut one frame with ffmpeg and store it atomically. Raises on failure."""
    target = frame_path(video_id, timestamp)
    if target.exists():
        return target
    url, length = stream_url(video_id)
    if length and timestamp > length:
        raise DeArrowError("timestamp is past the end of the video")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "http,https,tcp,tls",
        "-ss", f"{max(0.0, timestamp):.3f}", "-i", url, "-frames:v", "1",
        "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", "-c:v", "mjpeg", "-f", "image2", "-y", str(partial),
    ]
    result = subprocess.run(command, capture_output=True, timeout=_FRAME_TIMEOUT_SECONDS)
    if result.returncode != 0 or not partial.exists() or partial.stat().st_size < 500:
        partial.unlink(missing_ok=True)
        raise DeArrowError(f"ffmpeg exited {result.returncode}")
    os.replace(partial, target)
    for older in target.parent.glob(f"{video_id}-*.jpg"):
        if older != target:
            older.unlink(missing_ok=True)
    _prune(target.parent)
    return target


def _prune(directory: Path) -> None:
    """Keep the cache bounded: the oldest frames go first once it passes the limit."""
    limit = max(50, int(get_settings().dearrow_cache_max_frames or 0))
    try:
        files = sorted(directory.glob("*.jpg"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return
    for path in files[: max(0, len(files) - limit)]:
        path.unlink(missing_ok=True)


__all__ = [
    "CASINGS",
    "DEARROW_ENDPOINT",
    "DeArrowError",
    "branding_for",
    "clean_title",
    "clear_cache",
    "current_frame",
    "cut_frame",
    "enabled",
    "fetch_prefix",
    "format_title",
    "frame_path",
    "lookup",
    "parse_branding",
    "pick_branding",
    "preferences",
    "request_frame",
    "thumbnail_url",
    "to_lowercase",
]
