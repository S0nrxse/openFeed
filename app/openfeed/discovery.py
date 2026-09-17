from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

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


# Invidious's list endpoints do not carry a real publish timestamp. They carry the
# relative text YouTube renders ("2 days ago") and decode it to an absolute time at
# request time — so the value is only ever as precise as that text, and when the text is
# missing or unrecognised the decoder yields *now*. Stored unchecked, that dates a video
# to the moment we happened to fetch it: an episode from January arrives as the newest
# thing on the channel, and the feed's recency boost promotes it on top of that.
#
# Anything claiming to be published within this window of the fetch is treated as unknown.
# A list endpoint has no business reporting a video this new, and leaving the date empty
# is recoverable — enrichment reads the real timestamp from the single-video endpoint,
# which does report one — whereas a fabricated date silently outranks everything.
_FABRICATED_DATE_WINDOW = timedelta(minutes=5)


def _published_at(item: dict[str, Any]) -> datetime | None:
    """Invidious reports publish time as unix seconds; the column wants a timestamp.

    Returned as a datetime rather than an int because upsert_discovered_video binds this
    straight into a TIMESTAMPTZ, and an integer there is a type error rather than a date.
    """
    raw = _int_or_none(item.get("published"))
    if not raw or raw <= 0:
        return None
    try:
        published = datetime.fromtimestamp(raw, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if published >= datetime.now(tz=timezone.utc) - _FABRICATED_DATE_WINDOW:
        return None
    return published


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
        # Same trap as the publish date, and the same endpoints: a list entry reports 0
        # views when YouTube did not render a view count in that row, not when nobody has
        # watched it. Stored as 0 it renders as "0 views" on a video with 371K, and no
        # later pass can tell the difference between a real zero and a missing one.
        "view_count": _int_or_none(item.get("viewCount")) or None,
        "summary": item.get("description"),
        # Invidious reports this as unix seconds. It used to be discarded, which meant
        # every video acquired this way arrived undated — and a feed sorted by date cannot
        # place an undated video anywhere sensible, so whole channels sank to the bottom.
        "published_at": _published_at(item),
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


def search_youtube(query: str, page: int = 1, sort: str = "relevance") -> list[dict[str, Any]]:
    """Search YouTube through Invidious.

    The local library only ever contained what had already been synced or discovered, so
    searching it answered "which of the videos I already have match this" — not what
    anyone means by search. This asks the backend the real question.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return []
    settings = get_settings()
    if not settings.invidious_base_url:
        raise DiscoveryError("Invidious is not configured")
    timeout = max(4, settings.enrichment_timeout_seconds)
    params = {
        "q": cleaned,
        "page": max(1, min(int(page or 1), 5)),
        "type": "video",
        "sort_by": sort if sort in {"relevance", "rating", "upload_date", "view_count"} else "relevance",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(f"{settings.invidious_base_url}/api/v1/search", params=params)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, list):
        return []
    results: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") not in (None, "video"):
            continue
        video = _normalize_related(item)
        if video:
            # Keep the date the backend reported (already filtered for fabricated
            # "just now" values). Dropping it made every search result undated,
            # which sank it in ranking and hid recency from the UI.
            results.append(video)
    return results


def fetch_channel_uploads(youtube_channel_id: str, page: int = 1, sort_by: str = "newest") -> list[dict[str, Any]]:
    """A page of a channel's uploads from Invidious.

    The RSS feed a subscription is built on only carries the most recent 15 videos, which
    is why a channel page otherwise shows a fraction of what the channel has published.
    """
    if not youtube_channel_id:
        return []
    settings = get_settings()
    if not settings.invidious_base_url:
        raise DiscoveryError("Invidious is not configured")
    timeout = max(4, settings.enrichment_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(
            f"{settings.invidious_base_url}/api/v1/channels/{youtube_channel_id}/videos",
            params={"page": max(1, min(int(page or 1), 20)), "sort_by": sort_by},
        )
        response.raise_for_status()
        payload = response.json()
    items = payload.get("videos") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video = _normalize_related(item)
        if video:
            video.setdefault("youtube_channel_id", youtube_channel_id)
            results.append(video)
    return results


def seed_from_takeout(user_key: str = "local", channels: int = 40, per_channel: int = 1) -> dict[str, Any]:
    """Pull recent uploads from the channels an imported history says you watch.

    Importing a watch history tells the ranker *what* you like but hands it nothing new to
    rank: the library only ever contained videos reached through your FreshRSS
    subscriptions and discovery from those. So a history full of tech and comedy would
    still produce a feed of whatever the six original subscriptions happened to be about,
    and it would look like the import did nothing.

    Channels are taken in recency-weighted affinity order rather than raw watch count, so
    what you watch *now* seeds the library before what you watched last winter. Skipping
    channels already well represented keeps repeat runs cheap.
    """
    from . import db

    ranked = db.takeout_channels_by_affinity(user_key, channels * 3)
    if not ranked:
        return {"ok": False, "reason": "no_import", "channels": 0, "stored": 0}

    existing = db.channel_video_counts([str(row["youtube_channel_id"]) for row in ranked])
    stored = 0
    touched = 0
    errors = 0
    for row in ranked:
        if touched >= channels:
            break
        channel_id = str(row.get("youtube_channel_id") or "")
        # Already stocked: nothing to gain from asking again.
        if not channel_id or existing.get(channel_id.lower(), 0) >= 5:
            continue
        touched += 1
        try:
            items = fetch_channel_uploads(channel_id, page=1)
        except Exception as exc:  # noqa: BLE001 - one dead channel must not stop the pass
            logger.info("takeout seed failed for %s: %s", row.get("title") or channel_id, exc)
            errors += 1
            continue
        for item in (items[: max(1, per_channel)] if per_channel else items):
            if db.upsert_discovered_video(item, None, "takeout", "imported watch history"):
                stored += 1
    return {"ok": True, "channels": touched, "stored": stored, "errors": errors}


def fetch_trending(region: str = "US", category: str = "") -> list[dict[str, Any]]:
    """What is broadly popular right now, optionally within one category."""
    settings = get_settings()
    if not settings.invidious_base_url:
        raise DiscoveryError("Invidious is not configured")
    params: dict[str, str] = {"region": region}
    if category:
        params["type"] = category
    with httpx.Client(timeout=max(4, settings.enrichment_timeout_seconds)) as client:
        response = client.get(f"{settings.invidious_base_url}/api/v1/trending", params=params)
        response.raise_for_status()
        payload = response.json()
    items = payload if isinstance(payload, list) else []
    return [video for video in (_normalize_related(item) for item in items if isinstance(item, dict)) if video]


def seed_from_topics(user_key: str = "local", terms: int = 6, per_term: int = 8) -> dict[str, Any]:
    """Search out videos on the subjects this user watches, from anywhere.

    This is the source that breaks the closed loop. Subscriptions and related-video
    discovery can only ever return neighbours of what the library already holds, so a
    feed built from them narrows over time no matter how good the ranking is. Searching
    the user's own top interest terms reaches channels they have never seen.

    Two sort orders per term on purpose: `relevance` returns the well-known videos on a
    subject, `upload_date` returns the ones published this week that nobody has found yet.
    Popular and niche are different queries, not different rankings of one query.
    """
    from . import db, topics

    profile = topics.profile_vector(user_key)
    if not profile:
        return {"ok": False, "reason": "no_profile", "queries": 0, "stored": 0}
    # Single words make vague queries; pairing the strongest terms gives the search
    # something specific to match without narrowing it to one video.
    ranked = [term for term, _ in sorted(profile.items(), key=lambda item: item[1], reverse=True)]
    queries: list[str] = ranked[:terms]
    queries += [f"{ranked[i]} {ranked[i + 1]}" for i in range(0, min(len(ranked) - 1, terms), 2)]

    stored = 0
    used = 0
    for query in queries:
        for sort_by in ("relevance", "upload_date"):
            try:
                results = search_youtube(query, sort=sort_by)
            except Exception as exc:  # noqa: BLE001 - a failed query must not stop the pass
                logger.info("topic seed failed for %r: %s", query, exc)
                continue
            used += 1
            for item in results[:per_term]:
                if db.upsert_discovered_video(item, None, "topic-search", f"topic: {query}"):
                    stored += 1
    return {"ok": True, "queries": used, "stored": stored}


def seed_trending(region: str = "US", categories: Iterable[str] = ("", "Music", "Gaming")) -> dict[str, Any]:
    """A little of what everyone is watching, so the feed is not purely a mirror.

    Deliberately small. Trending is the least personalised thing here and exists to keep
    the door open to subjects the profile has no way to ask for yet.
    """
    from . import db

    stored = 0
    used = 0
    for category in categories:
        try:
            items = fetch_trending(region, category)
        except Exception as exc:  # noqa: BLE001
            logger.info("trending seed failed for %r: %s", category or "all", exc)
            continue
        used += 1
        for item in items[:10]:
            if db.upsert_discovered_video(item, None, "trending", f"trending: {category or 'all'}"):
                stored += 1
    return {"ok": True, "categories": used, "stored": stored}


def grow_library(user_key: str = "local", include_trending: bool = True) -> dict[str, Any]:
    """Run every acquisition source once, with a budget on each.

    The point is breadth: each source reaches somewhere the others cannot. Subscriptions
    bring what you follow, takeout brings the channels you actually watch, topic search
    brings strangers working on the same subjects, and trending brings the rest.
    """
    report: dict[str, Any] = {}
    for name, run in (
        ("takeout_channels", lambda: seed_from_takeout(user_key)),
        ("topic_search", lambda: seed_from_topics(user_key)),
        ("related", lambda: discover_related_videos(4, 6)),
    ):
        try:
            report[name] = run()
        except Exception as exc:  # noqa: BLE001
            report[name] = {"ok": False, "error": str(exc)[:160]}
    if include_trending:
        try:
            report["trending"] = seed_trending()
        except Exception as exc:  # noqa: BLE001
            report["trending"] = {"ok": False, "error": str(exc)[:160]}
    report["stored_total"] = sum(int((r or {}).get("stored") or 0) for r in report.values() if isinstance(r, dict))
    return report


def backfill_subscription_uploads(limit: int = 200, per_channel_pages: int = 2) -> dict[str, Any]:
    """Fetch recent uploads for every subscribed channel.

    The subscription feed is ordered by publish date, which only works if the videos are
    actually there. FreshRSS feeds carry roughly the last fifteen uploads per channel and
    only from the moment you subscribe, so a freshly imported subscription list produces a
    feed with most of its channels missing entirely — which reads as "the order is wrong"
    rather than "half the data is absent".
    """
    from . import db

    subscriptions = db.list_subscriptions(limit)
    stored = 0
    touched = 0
    errors = 0
    for subscription in subscriptions:
        channel_id = str(subscription.get("youtube_channel_id") or "").strip()
        if not channel_id:
            continue
        touched += 1
        for page in range(1, max(1, per_channel_pages) + 1):
            try:
                items = fetch_channel_uploads(channel_id, page=page)
            except Exception as exc:  # noqa: BLE001 - one dead channel must not stop the pass
                logger.info("subscription backfill failed for %s: %s", channel_id, exc)
                errors += 1
                break
            if not items:
                break
            added = sum(1 for item in items if db.upsert_discovered_video(item, None, "subscription-backfill", "subscription feed"))
            stored += added
            if not added:
                break
    return {"ok": True, "channels": touched, "stored": stored, "errors": errors}
