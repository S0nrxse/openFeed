"""SponsorBlock segments, read-only and privacy-first.

SponsorBlock is a crowdsourced database of which parts of a video are sponsor reads,
self-promotion, intros and so on. Using it is a small privacy trade — another party learns
something about what this deployment is looking at — so it follows the same shape as the
RYD integration: off unless asked, read-only, server-side, cached, fixed endpoint.

The one thing that makes this better than RYD: SponsorBlock publishes a genuinely
privacy-preserving lookup. You send the **first four characters of the SHA-256 of the video
id**, and it returns segments for every video whose hash starts with those characters —
typically a few dozen. The server never learns which one you actually wanted, and we filter
locally. That is why this module never calls the plain `?videoID=` endpoint even though it
would be simpler.

Deliberately not implemented, and not by omission: submitting segments, voting on them, or
any moderation action. Those all require identifying a user to SponsorBlock, and a read-only
client has nothing to leak.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Any, Iterable

import httpx

from .config import get_settings

logger = logging.getLogger("openfeed.sponsorblock")

# A constant, for the same reason RYD's is: making it configurable would turn a privacy
# setting into a way to point viewing interest at an arbitrary host.
SPONSORBLOCK_ENDPOINT = "https://sponsor.ajay.app/api/skipSegments"
SPONSORBLOCK_TIMEOUT_SECONDS = 6.0
# Four hex characters is what the API expects, and the anonymity set it produces
# (roughly 1 in 65,536 of the database) is the entire point of using it.
HASH_PREFIX_LENGTH = 4

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# The categories OpenFeed exposes. `preview` is upstream's "Preview/Recap/Hook" — one
# category covering both, not two, so the UI must not imply they can be set separately.
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("sponsor", "Sponsors", "Paid promotion, paid referrals and direct advertisements."),
    ("selfpromo", "Self-promotion", "Unpaid plugs for the creator's own merch, Patreon or other channels."),
    ("interaction", "Interaction reminders", "“Like and subscribe” asks that interrupt the video."),
    ("intro", "Intros", "Intermission and intro animations with no content."),
    ("outro", "Outros", "Endcards and credits after the content ends."),
    ("preview", "Previews and hooks", "Recaps of earlier videos, and hooks previewing what is coming up."),
    ("filler", "Filler", "Tangential jokes and asides that are not needed to follow the video."),
)
CATEGORY_KEYS = tuple(key for key, _, _ in CATEGORIES)

# Only sponsors and self-promotion skip on their own. The rest default to doing nothing,
# because silently cutting an intro or a joke is the sort of thing that feels like a bug
# until you know the feature exists.
DEFAULT_ACTIONS: dict[str, str] = {key: "skip" if key in {"sponsor", "selfpromo"} else "off" for key in CATEGORY_KEYS}
ACTIONS = ("skip", "manual", "off")


class SponsorBlockError(RuntimeError):
    """SponsorBlock could not be reached, or answered with something unusable."""


def valid_video_id(video_id: str) -> bool:
    return bool(_VIDEO_ID.match(str(video_id or "")))


def hash_prefix(video_id: str) -> str:
    """The first four hex characters of SHA-256(video id).

    This is the whole privacy mechanism: it is what gets sent instead of the video id.
    """
    return hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:HASH_PREFIX_LENGTH]


def normalize_actions(raw: Any) -> dict[str, str]:
    """Coerce stored preferences into a complete, valid action per category."""
    actions = dict(DEFAULT_ACTIONS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in CATEGORY_KEYS and str(value) in ACTIONS:
                actions[key] = str(value)
    return actions


def enabled(user_key: str = "local") -> bool:
    """Both the deployment and the user must allow it, exactly as with RYD."""
    from . import db

    if not get_settings().sponsorblock_enabled:
        return False
    try:
        return bool(db.get_feed_preferences(user_key).get("sponsorblock_enabled"))
    except Exception:
        return False


def active_categories(user_key: str = "local") -> list[str]:
    """Categories the user wants something to happen for.

    A category set to "do nothing" is not requested at all, so it never reaches the
    outbound query and never gets stored.
    """
    from . import db

    try:
        actions = normalize_actions(db.get_feed_preferences(user_key).get("sponsorblock_actions"))
    except Exception:
        actions = dict(DEFAULT_ACTIONS)
    return [key for key, action in actions.items() if action in {"skip", "manual"}]


def parse_segments(payload: Any, video_id: str, categories: Iterable[str]) -> list[dict[str, Any]]:
    """Pull this video's segments out of a hash-prefix response.

    The response covers every video sharing the hash prefix, so the first job is to discard
    the ones that are not ours — the API is trusted to answer, not to answer only about us.
    """
    wanted = {key for key in categories if key in CATEGORY_KEYS}
    if not isinstance(payload, list):
        return []
    segments: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict) or str(entry.get("videoID") or "") != video_id:
            continue
        for item in entry.get("segments") or []:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "")
            if category not in wanted:
                continue
            bounds = item.get("segment")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                continue
            try:
                start = float(bounds[0])
                end = float(bounds[1])
            except (TypeError, ValueError):
                continue
            # A zero-length or reversed segment would make the player seek to nowhere.
            if not (0 <= start < end) or (end - start) > 86400:
                continue
            segments.append({
                "category": category,
                "start": round(start, 3),
                "end": round(end, 3),
                "uuid": str(item.get("UUID") or "")[:64],
                "locked": bool(item.get("locked")),
                "votes": int(item.get("votes") or 0) if isinstance(item.get("votes"), (int, float)) else 0,
            })
    segments.sort(key=lambda item: item["start"])
    return _merge_overlaps(segments)


def _merge_overlaps(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse overlapping segments of the same category into one.

    Contributors submit slightly different bounds for the same sponsor read, and skipping
    into the middle of an overlapping pair produces a visible stutter.
    """
    merged: list[dict[str, Any]] = []
    for segment in segments:
        previous = merged[-1] if merged else None
        if previous and segment["category"] == previous["category"] and segment["start"] <= previous["end"] + 0.5:
            previous["end"] = max(previous["end"], segment["end"])
            continue
        merged.append(dict(segment))
    return merged


# Segments barely change, so this cache is generous compared with the RYD one - but it is
# still bounded, because a video can be re-submitted and corrected.
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _cache_key(video_id: str, categories: Iterable[str]) -> str:
    return f"{video_id}:{','.join(sorted(categories))}"


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def fetch_segments(video_id: str, categories: Iterable[str], *, force: bool = False) -> list[dict[str, Any]]:
    """Segments for one video, looked up by hash prefix.

    Raises SponsorBlockError on transport failures; an empty list means the lookup worked
    and this video simply has no segments, which is the common case.
    """
    categories = [key for key in categories if key in CATEGORY_KEYS]
    if not valid_video_id(video_id) or not categories:
        return []

    key = _cache_key(video_id, categories)
    ttl = max(0, get_settings().sponsorblock_cache_seconds)
    if not force and ttl:
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.time() < entry[0]:
                return entry[1]

    url = f"{SPONSORBLOCK_ENDPOINT}/{hash_prefix(video_id)}"
    headers = {"accept": "application/json", "user-agent": "OpenFeed (+https://github.com/S0nrxse/openFeed)"}
    try:
        with httpx.Client(timeout=SPONSORBLOCK_TIMEOUT_SECONDS, follow_redirects=False, headers=headers) as client:
            response = client.get(url, params={"categories": _json_list(categories)})
    except httpx.HTTPError as exc:
        raise SponsorBlockError(f"SponsorBlock request failed: {exc}") from exc

    if response.status_code == 404:
        segments: list[dict[str, Any]] = []
    elif response.status_code != 200:
        raise SponsorBlockError(f"SponsorBlock returned HTTP {response.status_code}")
    else:
        try:
            segments = parse_segments(response.json(), video_id, categories)
        except ValueError:
            segments = []

    if ttl:
        with _cache_lock:
            _cache[key] = (time.time() + ttl, segments)
            if len(_cache) > 512:
                now = time.time()
                for cached_key, (expires, _) in list(_cache.items()):
                    if expires <= now:
                        _cache.pop(cached_key, None)
                while len(_cache) > 512:
                    _cache.pop(next(iter(_cache)), None)
    return segments


def _json_list(values: Iterable[str]) -> str:
    """The API wants its categories as a JSON array in a query parameter."""
    import json

    return json.dumps(list(values))


def segments_for(video_id: str, user_key: str = "local") -> dict[str, Any]:
    """What the player needs: the segments, and what to do with each."""
    from . import db

    if not enabled(user_key):
        return {"ok": False, "reason": "disabled", "segments": [], "actions": {}}
    categories = active_categories(user_key)
    if not categories:
        return {"ok": True, "segments": [], "actions": {}, "reason": "no_categories"}
    try:
        segments = fetch_segments(video_id, categories)
    except SponsorBlockError as exc:
        logger.info("SponsorBlock lookup failed for %s: %s", video_id, exc)
        return {"ok": False, "reason": "unavailable", "segments": [], "actions": {}}
    actions = normalize_actions(db.get_feed_preferences(user_key).get("sponsorblock_actions"))
    labels = {key: label for key, label, _ in CATEGORIES}
    for segment in segments:
        segment["action"] = actions.get(segment["category"], "off")
        segment["label"] = labels.get(segment["category"], segment["category"])
    return {"ok": True, "segments": segments, "actions": actions}


__all__ = [
    "ACTIONS",
    "CATEGORIES",
    "CATEGORY_KEYS",
    "DEFAULT_ACTIONS",
    "SPONSORBLOCK_ENDPOINT",
    "SponsorBlockError",
    "active_categories",
    "clear_cache",
    "enabled",
    "fetch_segments",
    "hash_prefix",
    "normalize_actions",
    "parse_segments",
    "segments_for",
    "valid_video_id",
]
