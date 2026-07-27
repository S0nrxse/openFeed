from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
RECOMMENDATION_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]{2,}")
RECOMMENDATION_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "almost",
    "before",
    "being",
    "breaking",
    "could",
    "episode",
    "every",
    "first",
    "from",
    "full",
    "have",
    "into",
    "just",
    "live",
    "more",
    "most",
    "never",
    "official",
    "only",
    "over",
    "part",
    "really",
    "same",
    "shorts",
    "that",
    "their",
    "them",
    "then",
    "this",
    "through",
    "tries",
    "video",
    "watch",
    "what",
    "when",
    "where",
    "with",
    "would",
    "your",
}


@contextmanager
def connect() -> Iterator[psycopg.Connection[Any]]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)
        conn.commit()


TAG_MAX_LENGTH = 34
TAG_MIN_LENGTH = 3


def normalize_tags(values: Any, limit: int = 25) -> list[str]:
    """Clean YouTube's raw keyword list into usable tags.

    Channel keywords are frequently stuffed with misspellings of the channel name and
    very long phrases, so drop anything implausible and de-duplicate case-insensitively.
    """
    if not isinstance(values, (list, tuple)):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(str(value or "").split()).lower()
        if not tag or tag.isdigit():
            continue
        if len(tag) < TAG_MIN_LENGTH or len(tag) > TAG_MAX_LENGTH:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def _tags_from_metadata(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else metadata
    return normalize_tags((raw or {}).get("keywords"))


# A video counts as a Short when YouTube's own feed says so (/shorts/ permalink) or it is
# vertical-length. Duration alone is unreliable: some Shorts run past 60s and some regular
# uploads are very short, so the URL is checked first.
# strpos rather than LIKE: a literal % in the SQL would collide with psycopg placeholders.
SHORT_VIDEO_SQL = "(strpos(COALESCE(v.url, ''), '/shorts/') > 0 OR (v.duration_seconds > 0 AND v.duration_seconds <= 90))"

# Videos keep their rows after an unsubscribe, so subscription surfaces have to ask
# whether the channel is *still* subscribed instead of trusting videos.source_kind.
# The first branch keeps installs that have never synced FreshRSS from showing an
# empty subscription feed.
ACTIVE_SUBSCRIPTION_SQL = """(
  NOT EXISTS (SELECT 1 FROM freshrss_subscriptions WHERE youtube_channel_id IS NOT NULL)
  OR EXISTS (
    SELECT 1 FROM freshrss_subscriptions fs
    WHERE fs.youtube_channel_id IS NOT NULL AND fs.youtube_channel_id = c.youtube_channel_id
  )
)"""


def is_short_video(row: dict[str, Any]) -> bool:
    if "/shorts/" in str(row.get("url") or ""):
        return True
    duration = row.get("duration_seconds")
    try:
        duration = int(duration or 0)
    except (TypeError, ValueError):
        duration = 0
    return 0 < duration <= 90


def list_short_videos(limit: int = 12, user_key: str = "local", offset: int = 0) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.tags,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       v.source_kind, v.discovery_source, uvf.reaction
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
                LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = %s
                WHERE {SHORT_VIDEO_SQL}
                  AND COALESCE(uvf.not_interested, false) = false
                  AND COALESCE(pp.manually_marked_watched, false) = false
                ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (user_key, user_key, max(1, min(limit, 60)), max(0, offset)),
            )
            return list(cur.fetchall())


def list_subscription_uploads(limit: int = 30, user_key: str = "local", include_shorts: bool = True) -> list[dict[str, Any]]:
    """Recent uploads from subscribed channels, newest first — the notification feed."""
    shorts_clause = "" if include_shorts else f" AND NOT {SHORT_VIDEO_SQL}"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url,
                       v.published_at, v.duration_seconds, v.tags,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id
                FROM videos v
                JOIN channels c ON c.id = v.channel_id
                JOIN freshrss_subscriptions fs ON fs.youtube_channel_id = c.youtube_channel_id
                LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
                WHERE v.published_at IS NOT NULL
                  AND COALESCE(uvf.not_interested, false) = false
                  {shorts_clause}
                ORDER BY v.published_at DESC
                LIMIT %s
                """,
                (user_key, max(1, min(limit, 100))),
            )
            return list(cur.fetchall())


NOTIFICATION_LEVELS = ("all", "personalized", "none")


def get_channel_notification(youtube_channel_id: str, user_key: str = "local") -> str:
    """The bell setting for one channel. Absent means 'all'."""
    if not youtube_channel_id:
        return "all"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT level FROM channel_notifications WHERE user_key = %s AND youtube_channel_id = %s",
                (user_key, youtube_channel_id),
            )
            row = cur.fetchone()
    level = str((row or {}).get("level") or "all")
    return level if level in NOTIFICATION_LEVELS else "all"


def set_channel_notification(youtube_channel_id: str, level: str, user_key: str = "local") -> str:
    """Store a bell setting. 'all' deletes the row so the default stays the default."""
    if not youtube_channel_id:
        raise ValueError("youtube_channel_id is required")
    level = level if level in NOTIFICATION_LEVELS else "all"
    with connect() as conn:
        with conn.cursor() as cur:
            if level == "all":
                cur.execute(
                    "DELETE FROM channel_notifications WHERE user_key = %s AND youtube_channel_id = %s",
                    (user_key, youtube_channel_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO channel_notifications (user_key, youtube_channel_id, level)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_key, youtube_channel_id)
                    DO UPDATE SET level = EXCLUDED.level, updated_at = now()
                    """,
                    (user_key, youtube_channel_id, level),
                )
        conn.commit()
    return level


def channel_notification_levels(user_key: str = "local") -> dict[str, str]:
    """Every channel that has been moved off the default, keyed by channel id."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT youtube_channel_id, level FROM channel_notifications WHERE user_key = %s",
                (user_key,),
            )
            rows = list(cur.fetchall())
    return {str(row["youtube_channel_id"]): str(row["level"]) for row in rows}


def get_notifications_seen_at(user_key: str = "local") -> Any:
    with connect() as conn:
        with conn.cursor() as cur:
            user_id = _ensure_local_user_id(cur, user_key)
            cur.execute("SELECT profile FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone() or {}
        conn.commit()
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    return profile.get("notifications_seen_at")


def set_notifications_seen_at(value: str, user_key: str = "local") -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            user_id = _ensure_local_user_id(cur, user_key)
            cur.execute("SELECT profile FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone() or {}
            profile = dict(row.get("profile")) if isinstance(row.get("profile"), dict) else {}
            profile["notifications_seen_at"] = value
            cur.execute("UPDATE users SET profile = %s::jsonb, updated_at = now() WHERE id = %s", (json.dumps(profile), user_id))
        conn.commit()


def list_popular_tags(limit: int = 8, min_channels: int = 2, pool: int = 500) -> list[dict[str, Any]]:
    """Most common real YouTube tags across recently published videos.

    Requires a tag to span at least two channels. Channel keyword lists are routinely
    stuffed with misspellings of the channel's own name ("kurzgesagt", "kurzgezagt",
    "krzgesagt", ...), and those only ever occur on that one channel — so cross-channel
    frequency filters them out far more reliably than string-matching the channel title,
    and it also matches what makes a useful feed chip: a tag that groups several creators.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH recent AS (
                  SELECT v.tags, v.channel_id
                  FROM videos v
                  WHERE array_length(v.tags, 1) > 0
                  ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC
                  LIMIT %s
                ), exploded AS (
                  SELECT lower(unnest(tags)) AS tag, channel_id FROM recent
                )
                SELECT tag,
                       COUNT(*) AS video_count,
                       COUNT(DISTINCT channel_id) AS channel_count
                FROM exploded
                GROUP BY tag
                HAVING COUNT(DISTINCT channel_id) >= %s
                ORDER BY COUNT(DISTINCT channel_id) DESC, COUNT(*) DESC, tag ASC
                LIMIT %s
                """,
                (max(50, pool), max(1, min_channels), max(1, min(limit, 40))),
            )
            return list(cur.fetchall())


def backfill_video_tags(limit: int = 1000) -> int:
    """Populate tags for videos enriched before the column existed."""
    updated = 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, metadata FROM videos
                WHERE array_length(tags, 1) IS NULL
                  AND jsonb_array_length(coalesce(metadata->'keywords', '[]'::jsonb)) > 0
                LIMIT %s
                """,
                (max(1, limit),),
            )
            rows = list(cur.fetchall())
            for row in rows:
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                tags = normalize_tags(metadata.get("keywords"))
                if not tags:
                    continue
                cur.execute("UPDATE videos SET tags = %s WHERE id = %s", (tags, row["id"]))
                updated += 1
        conn.commit()
    return updated


def _recommendation_tokens(row: dict[str, Any]) -> list[str]:
    text = f"{row.get('title') or ''} {row.get('channel_title') or row.get('author') or ''}".lower()
    tokens: list[str] = []
    # Real YouTube tags first — they describe the video far better than title words.
    for tag in (row.get("tags") or [])[:8]:
        tag = str(tag or "").strip().lower()
        if tag and tag not in tokens:
            tokens.append(tag)
    for token in RECOMMENDATION_TOKEN_RE.findall(text):
        token = token.strip("'-")
        if len(token) < 4 or token.isdigit() or token in RECOMMENDATION_STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= 16:
            break
    return tokens


def _recommendation_channel_key(row: dict[str, Any]) -> str:
    channel_id = str(row.get("youtube_channel_id") or "").strip().lower()
    if channel_id:
        return channel_id
    return str(row.get("channel_title") or row.get("author") or "").strip().lower()


def _diversify_recommendations(rows: list[dict[str, Any]], limit: int, channel_cap_override: int = 0) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    channel_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    token_counts: dict[str, int] = {}

    available_sources = {str(row.get("source_kind") or "").lower() for row in rows}
    has_mixed_sources = "subscription" in available_sources and "discovery" in available_sources
    # The recommendation lane asks for a tighter cap: ranking alone concentrates on the
    # creators you already watch most, which is correct scoring but makes for a narrow
    # page. Two per creator forces at least half the page to be somebody else.
    channel_cap = channel_cap_override or (2 if limit <= 20 else 3)
    token_cap = max(2, min(7, limit // 8 if limit >= 16 else 2))
    discovery_cap = max(4, int(limit * 0.40))
    subscription_cap = limit - max(3, int(limit * 0.25)) if has_mixed_sources else limit

    def can_add(row: dict[str, Any], strict: bool) -> bool:
        video_id = str(row.get("youtube_video_id") or row.get("id") or "")
        if not video_id or video_id in used_ids:
            return False
        if not strict:
            return True
        source = str(row.get("source_kind") or "").lower()
        if source == "discovery" and source_counts.get(source, 0) >= discovery_cap:
            return False
        if source == "subscription" and source_counts.get(source, 0) >= subscription_cap:
            return False
        channel_key = _recommendation_channel_key(row)
        if channel_key and channel_counts.get(channel_key, 0) >= channel_cap:
            return False
        tokens = _recommendation_tokens(row)
        if any(token_counts.get(token, 0) >= token_cap for token in tokens[:5]):
            return False
        return True

    def add(row: dict[str, Any]) -> None:
        video_id = str(row.get("youtube_video_id") or row.get("id") or "")
        used_ids.add(video_id)
        selected.append(row)
        source = str(row.get("source_kind") or "").lower()
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1
        channel_key = _recommendation_channel_key(row)
        if channel_key:
            channel_counts[channel_key] = channel_counts.get(channel_key, 0) + 1
        for token in _recommendation_tokens(row)[:5]:
            token_counts[token] = token_counts.get(token, 0) + 1

    def can_add_relaxed(row: dict[str, Any], channel_allowance: int, token_allowance: int) -> bool:
        """A middle ground between the strict caps and no caps at all."""
        video_id = str(row.get("youtube_video_id") or row.get("id") or "")
        if not video_id or video_id in used_ids:
            return False
        channel_key = _recommendation_channel_key(row)
        if channel_key and channel_counts.get(channel_key, 0) >= channel_allowance:
            return False
        tokens = _recommendation_tokens(row)
        return not any(token_counts.get(token, 0) >= token_allowance for token in tokens[:5])

    for row in rows:
        if can_add(row, strict=True):
            add(row)
            if len(selected) >= limit:
                return selected

    # The strict pass runs dry long before the page is full, and the old fallback then
    # dropped every cap at once — which is how a handful of creators ended up filling the
    # grid. These passes give ground a step at a time, and the last one still holds a
    # channel ceiling, so no single creator can flood the page however thin the pool is.
    for channel_allowance, token_allowance in ((channel_cap + 1, token_cap * 2), (channel_cap + 2, token_cap * 4)):
        for row in rows:
            if can_add_relaxed(row, channel_allowance, token_allowance):
                add(row)
                if len(selected) >= limit:
                    return selected

    hard_channel_ceiling = channel_cap + 3
    for row in rows:
        if can_add_relaxed(row, hard_channel_ceiling, 10**6):
            add(row)
            if len(selected) >= limit:
                break

    return selected


CATEGORY_KEYWORDS: dict[str, set[str]] = {
    "music": {"music", "song", "album", "live", "band", "guitar", "drum", "mix", "concert"},
    "gaming": {"game", "gaming", "minecraft", "roblox", "nintendo", "xbox", "playstation", "steam", "speedrun"},
    "technology": {"tech", "code", "coding", "software", "linux", "python", "javascript", "rust", "computer", "apple", "google", "ai"},
    "news": {"news", "breaking", "politics", "election", "court", "government", "world", "report"},
    "education": {"learn", "history", "science", "explained", "tutorial", "course", "school", "lecture"},
    "podcasts": {"podcast", "interview", "episode", "conversation", "talks"},
}


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_local_user_id(cur: Any, user_key: str = "local") -> str:
    cur.execute(
        """
        INSERT INTO users (authentik_sub, display_name, profile)
        VALUES (%s, %s, '{}'::jsonb)
        ON CONFLICT (authentik_sub) DO UPDATE SET last_seen_at = now(), updated_at = now()
        RETURNING id
        """,
        (user_key, "Local OpenFeed user" if user_key == "local" else user_key),
    )
    row = cur.fetchone()
    return str(row["id"])


def upsert_user(authentik_sub: str, display_name: str = "", profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile_json = json.dumps(profile or {})
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (authentik_sub, display_name, profile, last_seen_at)
                VALUES (%s, %s, %s::jsonb, now())
                ON CONFLICT (authentik_sub) DO UPDATE SET
                  display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), users.display_name),
                  profile = users.profile || EXCLUDED.profile,
                  last_seen_at = now(),
                  updated_at = now()
                RETURNING *
                """,
                (authentik_sub, display_name, profile_json),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else {}


def get_feed_preferences(user_key: str = "local") -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            user_id = _ensure_local_user_id(cur, user_key)
            cur.execute("SELECT profile FROM users WHERE id = %s", (user_id,))
            profile_row = cur.fetchone() or {}
            profile = profile_row.get("profile") if isinstance(profile_row.get("profile"), dict) else {}
            cur.execute(
                """
                SELECT topic, preference, weight
                FROM user_topic_preferences
                WHERE user_id = %s
                ORDER BY updated_at DESC, topic ASC
                """,
                (user_id,),
            )
            topics = list(cur.fetchall())
        conn.commit()
    preferred = [row["topic"] for row in topics if row.get("preference") == "follow"]
    muted = [row["topic"] for row in topics if row.get("preference") == "mute"]
    return {
        "preferred_topics": preferred,
        "muted_topics": muted,
        "prioritize_subscriptions": bool(profile.get("prioritize_subscriptions", True)),
        "exclude_shorts": bool(profile.get("exclude_shorts", False)),
        "discovery_level": _float_value(profile.get("discovery_level"), 0.35),
        "show_comments": bool(profile.get("show_comments", False)),
        # Off unless explicitly enabled: turning it on lets a third party learn which
        # videos this deployment is interested in.
        "ryd_enabled": bool(profile.get("ryd_enabled", False)),
        # Off by default for the same reason as RYD: it sends something about what this
        # deployment is watching to a third party.
        "sponsorblock_enabled": bool(profile.get("sponsorblock_enabled", False)),
        # The channel Posts tab works regardless; this only controls the feeds.
        "show_posts_in_feeds": bool(profile.get("show_posts_in_feeds", False)),
        "muted_channels": [str(c) for c in (profile.get("muted_channels") or []) if str(c).strip()],
        "sponsorblock_actions": profile.get("sponsorblock_actions") or {},
    }


def set_feed_preferences(
    *,
    preferred_topics: list[str] | None = None,
    muted_topics: list[str] | None = None,
    prioritize_subscriptions: bool | None = None,
    exclude_shorts: bool | None = None,
    discovery_level: float | None = None,
    show_comments: bool | None = None,
    ryd_enabled: bool | None = None,
    sponsorblock_enabled: bool | None = None,
    show_posts_in_feeds: bool | None = None,
    muted_channels: list[str] | None = None,
    sponsorblock_actions: dict[str, str] | None = None,
    user_key: str = "local",
) -> dict[str, Any]:
    preferred_topics = [topic.strip().lower() for topic in (preferred_topics or []) if topic.strip()]
    muted_topics = [topic.strip().lower() for topic in (muted_topics or []) if topic.strip()]
    with connect() as conn:
        with conn.cursor() as cur:
            user_id = _ensure_local_user_id(cur, user_key)
            profile_updates = {"prioritize_subscriptions": True, "exclude_shorts": False, "discovery_level": 0.35, "show_comments": False, "ryd_enabled": False, "sponsorblock_enabled": False, "sponsorblock_actions": {}, "show_posts_in_feeds": False, "muted_channels": []}
            cur.execute("SELECT profile FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone() or {}
            if isinstance(row.get("profile"), dict):
                profile_updates.update(row["profile"])
            if prioritize_subscriptions is not None:
                profile_updates["prioritize_subscriptions"] = bool(prioritize_subscriptions)
            if exclude_shorts is not None:
                profile_updates["exclude_shorts"] = bool(exclude_shorts)
            if discovery_level is not None:
                profile_updates["discovery_level"] = max(0.0, min(1.0, float(discovery_level)))
            if show_comments is not None:
                profile_updates["show_comments"] = bool(show_comments)
            if ryd_enabled is not None:
                profile_updates["ryd_enabled"] = bool(ryd_enabled)
            if sponsorblock_enabled is not None:
                profile_updates["sponsorblock_enabled"] = bool(sponsorblock_enabled)
            if show_posts_in_feeds is not None:
                profile_updates["show_posts_in_feeds"] = bool(show_posts_in_feeds)
            if muted_channels is not None:
                profile_updates["muted_channels"] = [
                    str(c).strip().lower() for c in muted_channels if str(c).strip()
                ][:400]
            if sponsorblock_actions is not None:
                from . import sponsorblock

                profile_updates["sponsorblock_actions"] = sponsorblock.normalize_actions(sponsorblock_actions)
            cur.execute("UPDATE users SET profile = %s::jsonb, updated_at = now() WHERE id = %s", (json.dumps(profile_updates), user_id))
            cur.execute("DELETE FROM user_topic_preferences WHERE user_id = %s", (user_id,))
            for topic in preferred_topics:
                cur.execute(
                    """
                    INSERT INTO user_topic_preferences (user_id, topic, preference, weight)
                    VALUES (%s, %s, 'follow', 1)
                    ON CONFLICT (user_id, topic) DO UPDATE SET preference = 'follow', weight = 1, updated_at = now()
                    """,
                    (user_id, topic),
                )
            for topic in muted_topics:
                cur.execute(
                    """
                    INSERT INTO user_topic_preferences (user_id, topic, preference, weight)
                    VALUES (%s, %s, 'mute', 1)
                    ON CONFLICT (user_id, topic) DO UPDATE SET preference = 'mute', weight = 1, updated_at = now()
                    """,
                    (user_id, topic),
                )
        conn.commit()
    return get_feed_preferences(user_key)


PLAYBACK_DEFAULTS: dict[str, Any] = {
    "prefer_local": True,
    "public_invidious_enabled": False,
    "public_invidious_urls": [],
    "youtube_fallback": True,
    # Source ids in the order the user dragged them. Unknown ids are ignored and
    # anything missing keeps its natural position, so the list never has to be complete.
    "source_order": [],
    # Source ids the user switched off. One uniform list so every row in Settings —
    # local, public instance, YouTube — toggles the same way.
    "disabled_sources": [],
}


def normalize_instance_urls(urls: Any, limit: int = 6) -> list[str]:
    """Accept what a person would actually type: bare hosts, trailing slashes, dupes."""
    cleaned: list[str] = []
    if not isinstance(urls, (list, tuple)):
        return cleaned
    for url in urls:
        value = str(url or "").strip().rstrip("/")
        if not value:
            continue
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        if value not in cleaned:
            cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def normalize_playback_preferences(stored: Any) -> dict[str, Any]:
    """Fill in defaults, clean the lists, and migrate the old per-source flags."""
    stored = stored if isinstance(stored, dict) else {}
    preferences = dict(PLAYBACK_DEFAULTS)
    preferences.update({key: stored[key] for key in PLAYBACK_DEFAULTS if key in stored})
    urls = preferences.get("public_invidious_urls")
    preferences["public_invidious_urls"] = [str(url).strip().rstrip("/") for url in urls if str(url).strip()] if isinstance(urls, list) else []
    order = preferences.get("source_order")
    preferences["source_order"] = [str(item) for item in order if str(item).strip()] if isinstance(order, list) else []
    preferences["prefer_local"] = bool(preferences["prefer_local"])
    preferences["public_invidious_enabled"] = bool(preferences["public_invidious_enabled"])
    preferences["youtube_fallback"] = bool(preferences["youtube_fallback"])

    disabled = preferences.get("disabled_sources")
    disabled_set = {str(item) for item in disabled if str(item).strip()} if isinstance(disabled, list) else set()
    # Settings written before the uniform on/off list still carry the old flags.
    if not preferences["prefer_local"]:
        disabled_set.add("invidious")
    if not preferences["youtube_fallback"]:
        disabled_set.add("youtube")
    preferences["disabled_sources"] = sorted(disabled_set)
    return preferences


def get_playback_preferences(user_key: str = "local") -> dict[str, Any]:
    """Which playback sources this user allows, and in what order of preference."""
    with connect() as conn:
        with conn.cursor() as cur:
            user_id = _ensure_local_user_id(cur, user_key)
            cur.execute("SELECT profile FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone() or {}
        conn.commit()
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    return normalize_playback_preferences(profile.get("playback"))


def set_playback_preferences(
    *,
    prefer_local: bool | None = None,
    public_invidious_enabled: bool | None = None,
    public_invidious_urls: list[str] | None = None,
    youtube_fallback: bool | None = None,
    source_order: list[str] | None = None,
    disabled_sources: list[str] | None = None,
    user_key: str = "local",
) -> dict[str, Any]:
    updates = get_playback_preferences(user_key)
    if disabled_sources is not None:
        disabled = sorted({str(item).strip() for item in disabled_sources if str(item).strip()})
        updates["disabled_sources"] = disabled
        # Keep the legacy flags in step so nothing reads a stale value.
        updates["prefer_local"] = "invidious" not in disabled
        updates["youtube_fallback"] = "youtube" not in disabled
    if source_order is not None:
        seen: set[str] = set()
        cleaned_order: list[str] = []
        for item in source_order:
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                cleaned_order.append(value)
        updates["source_order"] = cleaned_order[:12]
    if prefer_local is not None:
        updates["prefer_local"] = bool(prefer_local)
    if public_invidious_enabled is not None:
        updates["public_invidious_enabled"] = bool(public_invidious_enabled)
    if youtube_fallback is not None:
        updates["youtube_fallback"] = bool(youtube_fallback)
    if public_invidious_urls is not None:
        updates["public_invidious_urls"] = normalize_instance_urls(public_invidious_urls)
    with connect() as conn:
        with conn.cursor() as cur:
            user_id = _ensure_local_user_id(cur, user_key)
            cur.execute("SELECT profile FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone() or {}
            profile = dict(row.get("profile")) if isinstance(row.get("profile"), dict) else {}
            profile["playback"] = updates
            cur.execute("UPDATE users SET profile = %s::jsonb, updated_at = now() WHERE id = %s", (json.dumps(profile), user_id))
        conn.commit()
    return updates


def _subscription_interest_profile(limit: int = 500) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT v.title, v.author, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                WHERE (v.source_kind = 'subscription' OR fe.subscription_id IS NOT NULL)
                  AND {ACTIVE_SUBSCRIPTION_SQL}
                ORDER BY COALESCE(v.published_at, v.created_at) DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = list(cur.fetchall())
    token_counts: dict[str, int] = {}
    channel_ids: set[str] = set()
    for row in rows:
        channel_id = str(row.get("youtube_channel_id") or "").lower()
        if channel_id:
            channel_ids.add(channel_id)
        for token in _recommendation_tokens(row):
            token_counts[token] = token_counts.get(token, 0) + 1
    return {"tokens": token_counts, "channels": channel_ids, "count": len(rows)}


def _row_matches_feed_filter(row: dict[str, Any], feed_filter: str) -> bool:
    key = (feed_filter or "all").strip().lower()
    if key in {"", "all"}:
        return True
    source = str(row.get("source_kind") or "").lower()
    tokens = set(_recommendation_tokens(row))
    duration = int(row.get("duration_seconds") or row.get("stored_duration_seconds") or 0)
    if key in {"subscription", "subscriptions", "from-subscriptions"}:
        return source == "subscription" and bool(row.get("is_subscribed_channel", True))
    if key == "discovery":
        return source == "discovery"
    if key in {"recent", "recently-uploaded"}:
        return bool(row.get("published_at"))
    if key in {"continue", "continue-watching"}:
        percent = _float_value(row.get("percent_completed"))
        return 0 < percent < 0.85
    if key in {"long", "long-form"}:
        return duration >= 1200
    if key in {"short", "shorts", "short-videos"}:
        return 0 < duration <= 90
    if key.startswith("topic:"):
        return key.split(":", 1)[1] in tokens
    if key in CATEGORY_KEYWORDS:
        return bool(tokens & CATEGORY_KEYWORDS[key])
    return True


def set_comment_reaction(youtube_video_id: str, comment_id: str, reaction: str, user_key: str = "local") -> str:
    """Store a local vote on a comment. An empty reaction clears it."""
    reaction = reaction if reaction in {"like", "dislike"} else ""
    with connect() as conn:
        with conn.cursor() as cur:
            if not reaction:
                cur.execute(
                    "DELETE FROM user_comment_feedback WHERE user_key = %s AND comment_id = %s",
                    (user_key, comment_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO user_comment_feedback (user_key, youtube_video_id, comment_id, reaction)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_key, comment_id)
                    DO UPDATE SET reaction = EXCLUDED.reaction, updated_at = now()
                    """,
                    (user_key, youtube_video_id, comment_id, reaction),
                )
        conn.commit()
    return reaction


def comment_reactions(youtube_video_id: str, user_key: str = "local") -> dict[str, str]:
    """Every stored vote for one video's comments, so the buttons survive a reload."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comment_id, reaction FROM user_comment_feedback WHERE user_key = %s AND youtube_video_id = %s",
                (user_key, youtube_video_id),
            )
            return {str(row["comment_id"]): str(row["reaction"]) for row in cur.fetchall()}


def videos_missing_dislikes(limit: int = 40, max_age_days: int = 30) -> list[str]:
    """Video ids with no dislike estimate yet, newest first.

    Videos already carrying a count from YouTube's own metadata are skipped — only the
    gap left by YouTube removing dislikes in 2021 is worth asking a third party about.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.youtube_video_id
                FROM videos v
                WHERE v.youtube_video_id IS NOT NULL
                  -- A zero from YouTube's own metadata is a placeholder, not a count, so
                  -- it still needs filling in. A zero that RYD actually reported is real.
                  AND (v.dislike_count IS NULL
                       OR (v.dislike_count = 0 AND v.dislike_source IS DISTINCT FROM 'ryd'))
                  AND (v.dislike_fetched_at IS NULL
                       OR v.dislike_fetched_at < now() - make_interval(days => %s))
                ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (max(1, int(max_age_days)), max(1, min(int(limit), 200))),
            )
            return [str(row["youtube_video_id"]) for row in cur.fetchall()]


def store_search_results(items: list[dict[str, Any]], query: str) -> int:
    """Persist videos returned by a YouTube search so the rest of the app can use them.

    A search result that is not in the database has no watch page, cannot be saved to a
    playlist and cannot be rated. Storing it as a discovery row — tagged with the search
    that surfaced it — makes it an ordinary video everywhere else.
    """
    stored = 0
    for item in items:
        try:
            if upsert_discovered_video(item, None, "search", f"search: {query[:80]}"):
                stored += 1
        except Exception:
            continue
    return stored


def store_takeout_import(parsed: dict[str, Any], summary: dict[str, Any], user_key: str = "local") -> dict[str, Any]:
    """Replace this user's imported signal with a freshly parsed archive.

    A replace rather than a merge: re-importing an updated export should reflect what the
    export says now, not the union of every archive ever uploaded.
    """
    channels = parsed.get("watched_channels") or []
    videos = parsed.get("watched_videos") or []
    # Subscriptions were parsed and then dropped on the floor before this.
    store_takeout_subscriptions(parsed.get("subscriptions") or [], user_key)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM takeout_channel_affinity WHERE user_key = %s", (user_key,))
            cur.execute("DELETE FROM takeout_watched_videos WHERE user_key = %s", (user_key,))
            if channels:
                cur.executemany(
                    """
                    INSERT INTO takeout_channel_affinity (user_key, youtube_channel_id, title, watch_count, last_watched_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_key, youtube_channel_id) DO UPDATE SET
                      title = COALESCE(NULLIF(EXCLUDED.title, ''), takeout_channel_affinity.title),
                      watch_count = EXCLUDED.watch_count,
                      last_watched_at = EXCLUDED.last_watched_at
                    """,
                    [
                        (user_key, item["youtube_channel_id"], item.get("title") or None,
                         int(item.get("watch_count") or 0), item.get("last_watched_at"))
                        for item in channels
                    ],
                )
            if videos:
                cur.executemany(
                    """
                    INSERT INTO takeout_watched_videos (user_key, youtube_video_id, youtube_channel_id, watched_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_key, youtube_video_id) DO UPDATE SET
                      watched_at = EXCLUDED.watched_at
                    """,
                    [
                        (user_key, item["youtube_video_id"], item.get("youtube_channel_id") or None, item.get("watched_at"))
                        for item in videos
                    ],
                )
            cur.execute(
                "INSERT INTO takeout_imports (user_key, summary) VALUES (%s, %s::jsonb) RETURNING *",
                (user_key, json.dumps(summary, default=str)),
            )
            row = cur.fetchone()
        conn.commit()
    clear_takeout_cache(user_key)
    return dict(row) if row else {}


def latest_takeout_import(user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM takeout_imports WHERE user_key = %s ORDER BY created_at DESC LIMIT 1",
                (user_key,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def clear_takeout_data(user_key: str = "local") -> dict[str, int]:
    """Remove everything an import added. One statement per table, nothing left behind."""
    removed = {"channels": 0, "videos": 0, "imports": 0}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM takeout_channel_affinity WHERE user_key = %s", (user_key,))
            removed["channels"] = cur.rowcount
            cur.execute("DELETE FROM takeout_watched_videos WHERE user_key = %s", (user_key,))
            removed["videos"] = cur.rowcount
            cur.execute("DELETE FROM takeout_imports WHERE user_key = %s", (user_key,))
            removed["imports"] = cur.rowcount
        conn.commit()
    clear_takeout_cache(user_key)
    return removed


_TAKEOUT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TAKEOUT_TTL = 300.0


def clear_takeout_cache(user_key: str | None = None) -> None:
    if user_key is None:
        _TAKEOUT_CACHE.clear()
    else:
        _TAKEOUT_CACHE.pop(user_key, None)


def takeout_profile(user_key: str = "local") -> dict[str, Any]:
    """Imported affinity, shaped like watch_time_profile so ranking can use both.

    Channel shares rather than raw counts, for the same reason live watch time is
    normalised: somebody with 50,000 history entries and somebody with 500 should get
    comparably scaled boosts.
    """
    cached = _TAKEOUT_CACHE.get(user_key)
    if cached and time.time() < cached[0]:
        return cached[1]

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT youtube_channel_id, title, watch_count, last_watched_at
                FROM takeout_channel_affinity WHERE user_key = %s
                """,
                (user_key,),
            )
            rows = list(cur.fetchall())
            cur.execute("SELECT COUNT(*) AS n FROM takeout_watched_videos WHERE user_key = %s", (user_key,))
            watched_count = int((cur.fetchone() or {}).get("n") or 0)

    # Weight each channel by how recently it was watched, not just how often. A channel
    # with 200 views two years ago and one with 20 last month are not the same interest,
    # and raw counts said they were.
    weighted: dict[str, float] = {}
    titles: dict[str, str] = {}
    for row in rows:
        key = str(row.get("youtube_channel_id") or "").strip().lower()
        if not key:
            continue
        count = int(row.get("watch_count") or 0)
        if count <= 0:
            continue
        weighted[key] = weighted.get(key, 0.0) + count * recency_weight(row.get("last_watched_at"))
        if row.get("title"):
            titles[key] = str(row["title"])
    total = sum(weighted.values())
    channels = {key: value / total for key, value in weighted.items()} if total else {}

    profile = {
        "channels": channels,
        "titles": titles,
        "total_watches": total,
        "watched_videos": watched_count,
        # Ramped like the live watch-time signal: a thin import should nudge the feed,
        # not take it over.
        "confidence": min(1.0, total / 300.0) if total else 0.0,
    }
    _TAKEOUT_CACHE[user_key] = (time.time() + _TAKEOUT_TTL, profile)
    return profile


def takeout_watched_ids(user_key: str = "local") -> set[str]:
    """Videos the import says are already watched, so they are not recommended again."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT youtube_video_id FROM takeout_watched_videos WHERE user_key = %s", (user_key,))
            return {str(row["youtube_video_id"]) for row in cur.fetchall()}


def create_api_token(name: str, token_prefix: str, token_hash: str, read_only: bool = False, user_key: str = "local") -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_tokens (user_key, name, token_prefix, token_hash, read_only)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (user_key, name.strip() or "API token", token_prefix, token_hash, read_only),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def list_api_tokens(user_key: str = "local", include_revoked: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM api_tokens
                WHERE user_key = %s {"" if include_revoked else "AND revoked_at IS NULL"}
                ORDER BY created_at DESC
                """,
                (user_key,),
            )
            return [dict(row) for row in cur.fetchall()]


def find_api_token(token_prefix: str) -> dict[str, Any] | None:
    """Look up a live token by its clear prefix. The hash is still verified by the caller."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM api_tokens WHERE token_prefix = %s AND revoked_at IS NULL",
                (token_prefix,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def touch_api_token(token_id: Any) -> None:
    """Record use, so a stale token is identifiable before it is revoked."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_tokens SET last_used_at = now() WHERE id = %s", (token_id,))
        conn.commit()


def revoke_api_token(token_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE api_tokens SET revoked_at = now()
                WHERE id = %s AND user_key = %s AND revoked_at IS NULL
                RETURNING *
                """,
                (token_id, user_key),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def register_download(
    youtube_video_id: str,
    title: str,
    filename: str,
    path: str,
    *,
    container: str = "mp4",
    height: int = 0,
    quality: str = "",
    size_bytes: int = 0,
    user_key: str = "local",
) -> dict[str, Any] | None:
    """Record a video kept on the server so it shows up in the library."""
    with connect() as conn:
        with conn.cursor() as cur:
            video_id = _video_id_for_youtube(cur, youtube_video_id)
            cur.execute(
                """
                INSERT INTO downloads
                  (user_key, youtube_video_id, video_id, title, filename, path, container, height, quality, size_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_key, youtube_video_id) DO UPDATE SET
                  video_id = COALESCE(EXCLUDED.video_id, downloads.video_id),
                  title = EXCLUDED.title,
                  filename = EXCLUDED.filename,
                  path = EXCLUDED.path,
                  container = EXCLUDED.container,
                  height = EXCLUDED.height,
                  quality = EXCLUDED.quality,
                  size_bytes = EXCLUDED.size_bytes,
                  created_at = now()
                RETURNING *
                """,
                (user_key, youtube_video_id, video_id, title, filename, path, container, height or None, quality, size_bytes or None),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def list_downloads(user_key: str = "local", query: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Downloaded videos, newest first, joined to whatever else we know about them.

    The join is a LEFT JOIN so a download survives its video row being pruned — the file
    is still on disk and still playable.
    """
    cleaned = (query or "").strip()
    pattern = f"%{cleaned}%"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT d.id AS download_id, d.youtube_video_id, d.title AS download_title,
                       d.filename, d.container, d.height, d.quality, d.size_bytes, d.created_at AS downloaded_at,
                       COALESCE(v.title, d.title) AS title,
                       v.url, v.thumbnail_url, v.author, v.duration_seconds, v.view_count,
                       v.published_at, v.tags, v.source_kind, v.discovery_source,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       uvf.reaction, COALESCE(pp.percent_completed, 0) AS percent_completed
                FROM downloads d
                LEFT JOIN videos v ON v.id = d.video_id OR v.youtube_video_id = d.youtube_video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = d.user_key
                LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = d.user_key
                WHERE d.user_key = %s
                  {"AND (d.title ILIKE %s OR COALESCE(v.title, '') ILIKE %s OR COALESCE(c.title, v.author, '') ILIKE %s)" if cleaned else ""}
                ORDER BY d.created_at DESC
                LIMIT %s
                """,
                (user_key, pattern, pattern, pattern, max(1, min(limit, 500))) if cleaned
                else (user_key, max(1, min(limit, 500))),
            )
            return list(cur.fetchall())


def get_download(youtube_video_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM downloads WHERE user_key = %s AND youtube_video_id = %s",
                (user_key, youtube_video_id),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def delete_download(youtube_video_id: str, user_key: str = "local") -> dict[str, Any] | None:
    """Drop the record and hand back the path so the caller can remove the file."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM downloads WHERE user_key = %s AND youtube_video_id = %s RETURNING *",
                (user_key, youtube_video_id),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def video_dislike_state(youtube_video_id: str) -> dict[str, Any]:
    """Just the dislike fields, for the poll that fills the count in after a cover batch."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dislike_count, dislike_source, dislike_fetched_at FROM videos WHERE youtube_video_id = %s",
                (youtube_video_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"dislike_count": None, "dislike_source": "", "checked": False}
    return {
        "dislike_count": row.get("dislike_count"),
        "dislike_source": row.get("dislike_source") or "",
        "checked": row.get("dislike_fetched_at") is not None,
    }


def store_dislike_counts(votes: list[dict[str, Any]]) -> int:
    """Persist estimates fetched from RYD, tagged with where they came from.

    dislike_fetched_at is stamped even when a lookup produced nothing, so a video with no
    estimate available is not retried on every pass.
    """
    if not votes:
        return 0
    updated = 0
    with connect() as conn:
        with conn.cursor() as cur:
            for vote in votes:
                cur.execute(
                    """
                    UPDATE videos
                    SET dislike_count = %s,
                        dislike_source = 'ryd',
                        dislike_fetched_at = now(),
                        updated_at = now()
                    WHERE youtube_video_id = %s
                    """,
                    (vote.get("dislikes"), vote.get("youtube_video_id")),
                )
                updated += cur.rowcount
        conn.commit()
    return updated


def mark_dislikes_checked(video_ids: list[str]) -> int:
    """Record that these were asked about, so an empty answer is not asked again today."""
    if not video_ids:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET dislike_fetched_at = now() WHERE youtube_video_id = ANY(%s)",
                (list(video_ids),),
            )
            count = cur.rowcount
        conn.commit()
    return count



# How fast an old interest stops counting. At one half-life a watch carries half the
# weight it did when it was fresh; at two, a quarter. 120 days is deliberately slower than
# the "what am I into this week" horizon and much faster than "ever" — a channel binged in
# January and untouched since should fade out of the feed by summer without being erased.
INTEREST_HALF_LIFE_DAYS = 120.0


def recency_weight(when: Any, half_life_days: float = INTEREST_HALF_LIFE_DAYS) -> float:
    """Exponential decay on the age of a signal. 1.0 today, 0.5 one half-life ago.

    Ranking treated a video watched two years ago exactly like one watched yesterday, so
    interests the user had moved on from kept pulling the feed backwards. Exponential
    rather than a cutoff window because a hard boundary makes a signal vanish overnight,
    and because taste fades rather than expiring.
    """
    if when is None:
        return 0.35  # undated history is real but unplaceable, so it counts for less
    try:
        moment = when if isinstance(when, datetime) else datetime.fromisoformat(str(when))
    except (TypeError, ValueError):
        return 0.35
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(1.0, half_life_days))


WATCH_TIME_WINDOW_DAYS = 60
# How much watch history earns the signal its full weight. Below this the boost is scaled
# down, so a first watched video nudges the feed instead of taking it over.
WATCH_TIME_CONFIDENCE_SECONDS = 3600.0
_WATCH_TIME_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_WATCH_TIME_TTL = 120.0


def watch_time_profile(user_key: str = "local", days: int = WATCH_TIME_WINDOW_DAYS) -> dict[str, Any]:
    """How the user's watch time is distributed across channels and topics.

    Clicking a video says you found the thumbnail interesting; watching it says the video
    was actually worth your time. That is a far better ranking signal, and it is the one
    the engine was throwing away — playback_progress was only ever read to *penalise*
    videos that had been abandoned.

    Shares are returned rather than raw seconds so a heavy viewing week and a light one
    produce comparably scaled boosts.
    """
    cached = _WATCH_TIME_CACHE.get(user_key)
    if cached and time.time() < cached[0]:
        return cached[1]

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.youtube_channel_id, c.title AS channel_title, v.author, v.tags,
                       SUM(pp.total_watch_seconds) AS seconds,
                       MAX(pp.last_watched_at) AS last_watched_at,
                       COUNT(*) FILTER (WHERE pp.completed) AS completions
                FROM playback_progress pp
                JOIN videos v ON v.id = pp.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE pp.user_key = %s
                  AND pp.total_watch_seconds > 0
                  AND pp.last_watched_at >= now() - make_interval(days => %s)
                GROUP BY c.youtube_channel_id, c.title, v.author, v.tags
                """,
                (user_key, max(1, int(days))),
            )
            rows = list(cur.fetchall())

    channels: dict[str, float] = {}
    topics: dict[str, float] = {}
    completions: dict[str, int] = {}
    total = 0.0
    for row in rows:
        seconds = float(row.get("seconds") or 0)
        if seconds <= 0:
            continue
        # Same decay as the imported history: minutes spent months ago describe a past
        # self, and weighting them equally is what keeps stale interests in the feed.
        seconds *= recency_weight(row.get("last_watched_at"))
        if seconds <= 0:
            continue
        total += seconds
        key = _recommendation_channel_key(row)
        if key:
            channels[key] = channels.get(key, 0.0) + seconds
            completions[key] = completions.get(key, 0) + int(row.get("completions") or 0)
        for tag in (row.get("tags") or [])[:8]:
            tag = str(tag or "").strip().lower()
            if tag:
                topics[tag] = topics.get(tag, 0.0) + seconds

    profile = {
        "total_seconds": total,
        # Share of watch time, so the boost does not scale with how much you watch overall.
        "channels": {key: value / total for key, value in channels.items()} if total else {},
        "topics": {key: value / total for key, value in topics.items()} if total else {},
        "completions": completions,
        # Shares are meaningless on thin history: one watched video makes its channel 100%
        # of your viewing and would otherwise out-rank an actual subscription. The boost is
        # scaled by this until roughly an hour of history has accumulated.
        "confidence": min(1.0, total / WATCH_TIME_CONFIDENCE_SECONDS) if total else 0.0,
    }
    _WATCH_TIME_CACHE[user_key] = (time.time() + _WATCH_TIME_TTL, profile)
    return profile


def clear_watch_time_cache(user_key: str | None = None) -> None:
    if user_key is None:
        _WATCH_TIME_CACHE.clear()
    else:
        _WATCH_TIME_CACHE.pop(user_key, None)


def _apply_recommendation_scores(
    rows: list[dict[str, Any]],
    feed_filter: str = "all",
    user_key: str = "local",
    suppress_subscription_bias: bool = False,
) -> list[dict[str, Any]]:
    """Rank candidates.

    `suppress_subscription_bias` drops the two flat bonuses a video gets purely for
    coming from a followed channel. Those bonuses are right for a general feed and
    wrong for the recommendation shelf: with six subscribed channels against several
    hundred discovered ones, +8 and +5 meant the same handful of creators won every
    slot and topic affinity never got to decide anything. Subscriptions reach the page
    through their own shelf instead.
    """
    profile = _subscription_interest_profile()
    prefs = get_feed_preferences(user_key)
    subscription_tokens: dict[str, int] = profile["tokens"]
    subscription_channels: set[str] = profile["channels"]
    preferred = set(prefs.get("preferred_topics") or [])
    muted = set(prefs.get("muted_topics") or [])
    prioritize_subs = bool(prefs.get("prioritize_subscriptions", True))
    exclude_shorts = bool(prefs.get("exclude_shorts", False))
    discovery_level = _float_value(prefs.get("discovery_level"), 0.35)
    from . import topics

    # One profile vector per ranking run, not per row.
    semantic_profile = topics.profile_vector(user_key)
    watch_profile = watch_time_profile(user_key)
    watched_channels: dict[str, float] = watch_profile["channels"]
    watched_topics: dict[str, float] = watch_profile["topics"]
    channel_completions: dict[str, int] = watch_profile["completions"]
    watch_confidence: float = watch_profile["confidence"]
    # Muting is the one control a user expects to be absolute. A penalty large enough to
    # usually work is still a feed that occasionally shows the thing you muted.
    muted_channels: set[str] = {str(c).lower() for c in (prefs.get("muted_channels") or [])}
    takeout = takeout_profile(user_key)
    takeout_channels: dict[str, float] = takeout["channels"]
    takeout_confidence: float = takeout["confidence"]
    takeout_seen: set[str] = takeout_watched_ids(user_key) if takeout["watched_videos"] else set()
    scored: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        source = str(row.get("source_kind") or "subscription").lower()
        channel_key = _recommendation_channel_key(row)
        # An unsubscribed channel's videos stay in the database, so a stale
        # source_kind must not keep earning subscription treatment.
        subscribed = bool(row.get("is_subscribed_channel", True))
        row["is_subscribed_channel"] = subscribed
        if not _row_matches_feed_filter(row, feed_filter):
            continue
        if muted_channels and channel_key and channel_key in muted_channels:
            continue
        duration = int(row.get("duration_seconds") or row.get("stored_duration_seconds") or 0)
        if exclude_shorts and 0 < duration <= 90:
            continue
        tokens = _recommendation_tokens(row)
        token_set = set(tokens)
        score = _float_value(row.get("recommendation_score"))
        debug: dict[str, float] = {"base": round(score, 3)}
        if prioritize_subs and source == "subscription" and subscribed and not suppress_subscription_bias:
            debug["subscription_source"] = 8.0
            score += 8.0
        elif source == "discovery":
            discovery_adjustment = -2.5 + (discovery_level * 4.0)
            debug["discovery_balance"] = round(discovery_adjustment, 3)
            score += discovery_adjustment
        if channel_key and channel_key in subscription_channels and not suppress_subscription_bias:
            debug["subscribed_channel"] = 5.0
            score += 5.0
        # --- content similarity -----------------------------------------------------
        # TF-IDF cosine against what this user actually watched, in place of the raw tag
        # overlap that used to sit here. That measure fired on essentially every candidate
        # and so ranked none of them; this one is continuous and discriminates.
        semantic = 0.0
        if semantic_profile:
            row_vector = row.get("topic_vector")
            if isinstance(row_vector, str):
                try:
                    row_vector = json.loads(row_vector)
                except ValueError:
                    row_vector = None
            if isinstance(row_vector, dict) and row_vector:
                semantic = topics.cosine(row_vector, semantic_profile)
                if semantic > 0.02:
                    boost = min(12.0, semantic * 28.0)
                    debug["semantic_affinity"] = round(boost, 3)
                    row["match_terms"] = topics.explain(row_vector, semantic_profile)
                    score += boost

        affinity = sum(min(subscription_tokens.get(token, 0), 8) for token in tokens[:8])
        if affinity:
            # Kept, but small: it is a weak tie-breaker once real similarity is available,
            # not the ranking signal it used to be by default.
            boost = min(2.5, affinity * 0.09)
            debug["subscription_topic_affinity"] = round(boost, 3)
            score += boost
        elif source == "discovery" and not semantic:
            # Only penalise a discovery video we know nothing about. Previously this fired
            # whenever tag overlap was absent, which punished anything well-matched by
            # subject but sharing no literal tag string.
            debug["ungrounded_discovery_penalty"] = -4.5
            score -= 4.5
        preferred_hits = token_set & preferred
        if preferred_hits:
            boost = min(8.0, 3.0 * len(preferred_hits))
            debug["preferred_topic"] = boost
            score += boost
        muted_hits = token_set & muted
        if muted_hits:
            penalty = min(24.0, 12.0 * len(muted_hits))
            debug["muted_topic"] = -penalty
            score -= penalty
        youtube_id = str(row.get("youtube_video_id") or "")

        # --- imported history ---------------------------------------------------------
        # For somebody who just imported, this is the best evidence in the system: years of
        # real viewing against a handful of clicks here. It is weighted accordingly, and
        # scaled by confidence so a thin import cannot swamp everything.
        if channel_key and channel_key in takeout_channels:
            share = takeout_channels[channel_key]
            boost = min(14.0, share * 90.0) * takeout_confidence
            if boost >= 0.05:
                debug["takeout_channel"] = round(boost, 3)
                score += boost
        if youtube_id and youtube_id in takeout_seen:
            # Already watched on YouTube. Not hidden outright — a rewatch is legitimate —
            # but it should never outrank something new.
            debug["takeout_already_watched"] = -6.0
            score -= 6.0

        percent = _float_value(row.get("percent_completed"))
        open_count = int(row.get("open_count") or 0)
        if open_count and percent < 0.08:
            debug["quick_abandonment"] = -4.0
            score -= 4.0
        elif 0.08 <= percent < 0.4:
            debug["partial_watch_penalty"] = -2.0
            score -= 2.0
        if row.get("view_count") is None and source == "discovery":
            debug["unknown_quality_penalty"] = -0.8
            score -= 0.8
        if not row.get("published_at") and source == "discovery":
            debug["undated_discovery_penalty"] = -0.5
            score -= 0.5
        reason = "From your subscriptions" if source == "subscription" and subscribed else "Related to your subscription topics" if affinity else "Exploration"
        if preferred_hits:
            reason = f"Matches followed topic: {sorted(preferred_hits)[0]}"
        row["recommendation_score"] = round(score, 3)
        row["score_debug"] = debug
        row["recommendation_reason"] = reason
        row["topic_tokens"] = tokens[:8]
        scored.append(row)
    def sort_time(item: dict[str, Any]) -> float:
        value = item.get("published_at") or item.get("created_at")
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        return 0.0

    scored.sort(key=lambda item: (_float_value(item.get("recommendation_score")), sort_time(item)), reverse=True)
    return scored


def db_status() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()
    return {"ok": bool(row and row["ok"] == 1)}


def system_counts() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM freshrss_subscriptions) AS subscriptions,
                  (SELECT COUNT(*) FROM channels) AS channels,
                  (SELECT COUNT(*) FROM videos) AS videos,
                  (SELECT COUNT(*) FROM videos WHERE source_kind = 'subscription') AS subscription_videos,
                  (SELECT COUNT(*) FROM videos WHERE source_kind = 'discovery') AS discovery_videos,
                  (SELECT COUNT(*) FROM freshrss_entries) AS freshrss_entries,
                  (SELECT COUNT(*) FROM user_video_events) AS user_video_events,
                  (SELECT COUNT(*) FROM users) AS users,
                  (SELECT COUNT(*) FROM interactions) AS interactions,
                  (SELECT COUNT(*) FROM feed_impressions) AS feed_impressions,
                  (SELECT COUNT(*) FROM recommendation_runs) AS recommendation_runs,
                  (SELECT COUNT(*) FROM recommendation_candidates) AS recommendation_candidates,
                  (SELECT COUNT(*) FROM user_topic_preferences) AS user_topic_preferences,
                  (SELECT COUNT(*) FROM user_channel_preferences) AS user_channel_preferences,
                  (SELECT COUNT(*) FROM retrieval_attempts) AS retrieval_attempts,
                  (SELECT COUNT(*) FROM retrieval_attempts WHERE status = 'error') AS retrieval_errors,
                  (SELECT COUNT(*) FROM sync_runs) AS sync_runs,
                  (SELECT COUNT(*) FROM videos WHERE metadata_fetched_at IS NOT NULL AND metadata_error IS NULL) AS enriched_videos,
                  (SELECT COUNT(*) FROM videos WHERE metadata_error IS NOT NULL) AS metadata_errors,
                  (SELECT COUNT(*) FROM video_discoveries) AS video_discoveries,
                  (SELECT MAX(started_at) FROM sync_runs) AS latest_sync_started,
                  (SELECT MAX(finished_at) FROM sync_runs) AS latest_sync_finished,
                  (SELECT status FROM sync_runs ORDER BY started_at DESC LIMIT 1) AS latest_sync_status,
                  (SELECT MAX(metadata_fetched_at) FROM videos) AS latest_metadata_fetch,
                  (SELECT MAX(discovered_at) FROM videos WHERE source_kind = 'discovery') AS latest_discovery,
                  (SELECT MAX(attempted_at) FROM retrieval_attempts) AS latest_retrieval_attempt
                """
            )
            row = cur.fetchone()
    return dict(row) if row else {}


def retrieval_attempt_rollup() -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, status, COUNT(*) AS count, MAX(attempted_at) AS latest_attempt
                FROM retrieval_attempts
                GROUP BY source, status
                ORDER BY source ASC, status ASC
                """
            )
            return list(cur.fetchall())


def list_recent_retrieval_attempts(limit: int = 12) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ra.id, ra.source, ra.status, ra.message, ra.attempted_at,
                       v.youtube_video_id, v.title AS video_title
                FROM retrieval_attempts ra
                LEFT JOIN videos v ON v.id = ra.video_id
                ORDER BY ra.attempted_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def create_sync_run(source: str) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sync_runs (source) VALUES (%s) RETURNING id",
                (source,),
            )
            sync_id = cur.fetchone()["id"]
        conn.commit()
    return int(sync_id)


def finish_sync_run(sync_id: int, status: str, message: str | None, subscriptions_count: int, entries_count: int) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_runs
                SET finished_at = now(), status = %s, message = %s,
                    subscriptions_count = %s, entries_count = %s
                WHERE id = %s
                """,
                (status, message, subscriptions_count, entries_count, sync_id),
            )
        conn.commit()


SUBSCRIPTION_REAL_ID_RE = re.compile(r"^feed/\d+$")


def _dedupe_subscription(cur: Any, incoming_id: str, feed_url: str | None, youtube_channel_id: str | None) -> None:
    """Collapse rows that describe the same feed under different stream ids.

    Subscribing from OpenFeed has to store a row before FreshRSS has assigned a numeric id,
    so it uses a ``feed/<url>`` placeholder. The next sync then imports the same feed
    under its real ``feed/<n>`` id — a different primary key — leaving the channel listed
    twice. Keep the real id when we have one and repoint anything referencing the loser,
    since collection membership cascades on delete.
    """
    if not feed_url and not youtube_channel_id:
        return
    cur.execute(
        """
        SELECT id FROM freshrss_subscriptions
        WHERE (%s::text IS NOT NULL AND feed_url = %s)
           OR (%s::text IS NOT NULL AND youtube_channel_id = %s)
        """,
        (feed_url, feed_url, youtube_channel_id or None, youtube_channel_id or None),
    )
    ids = [str(row["id"]) for row in cur.fetchall()]
    if len(ids) < 2:
        return
    real_ids = [item for item in ids if SUBSCRIPTION_REAL_ID_RE.match(item)]
    keep = real_ids[0] if real_ids else incoming_id
    losers = [item for item in ids if item != keep]
    if not losers:
        return
    cur.execute("UPDATE freshrss_entries SET subscription_id = %s WHERE subscription_id = ANY(%s)", (keep, losers))
    # Move collection membership across, skipping rows that would collide with the keeper.
    cur.execute(
        """
        UPDATE subscription_collection_members m
        SET subscription_id = %s
        WHERE m.subscription_id = ANY(%s)
          AND NOT EXISTS (
            SELECT 1 FROM subscription_collection_members other
            WHERE other.collection_id = m.collection_id AND other.subscription_id = %s
          )
        """,
        (keep, losers, keep),
    )
    cur.execute("DELETE FROM freshrss_subscriptions WHERE id = ANY(%s)", (losers,))


def upsert_subscription(item: dict[str, Any], youtube_channel_id: str | None) -> None:
    categories = json.dumps(item.get("categories") or [])
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO freshrss_subscriptions (id, title, feed_url, html_url, youtube_channel_id, categories)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                  title = EXCLUDED.title,
                  feed_url = EXCLUDED.feed_url,
                  html_url = EXCLUDED.html_url,
                  youtube_channel_id = EXCLUDED.youtube_channel_id,
                  categories = EXCLUDED.categories,
                  updated_at = now()
                """,
                (
                    item["id"],
                    item.get("title") or item.get("sortid") or "Untitled subscription",
                    item.get("url"),
                    item.get("htmlUrl"),
                    youtube_channel_id,
                    categories,
                ),
            )
            _dedupe_subscription(cur, str(item["id"]), item.get("url"), youtube_channel_id)
        conn.commit()


def upsert_channel(title: str, youtube_channel_id: str | None, feed_url: str | None) -> int | None:
    if not title and not youtube_channel_id:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            if youtube_channel_id:
                cur.execute(
                    """
                    INSERT INTO channels (youtube_channel_id, title, feed_url)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (youtube_channel_id) DO UPDATE SET
                      title = COALESCE(NULLIF(EXCLUDED.title, ''), channels.title),
                      feed_url = COALESCE(EXCLUDED.feed_url, channels.feed_url),
                      updated_at = now()
                    RETURNING id
                    """,
                    (youtube_channel_id, title or youtube_channel_id, feed_url),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO channels (title, feed_url)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (title, feed_url),
                )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"]) if row else None


def upsert_video(entry: dict[str, Any], channel_id: int | None, youtube_video_id: str) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos (youtube_video_id, channel_id, title, url, thumbnail_url, author, summary, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (youtube_video_id) DO UPDATE SET
                  channel_id = COALESCE(EXCLUDED.channel_id, videos.channel_id),
                  title = EXCLUDED.title,
                  url = COALESCE(EXCLUDED.url, videos.url),
                  thumbnail_url = COALESCE(EXCLUDED.thumbnail_url, videos.thumbnail_url),
                  author = COALESCE(EXCLUDED.author, videos.author),
                  summary = COALESCE(EXCLUDED.summary, videos.summary),
                  published_at = COALESCE(EXCLUDED.published_at, videos.published_at),
                  source_kind = 'subscription',
                  updated_at = now()
                RETURNING id
                """,
                (
                    youtube_video_id,
                    channel_id,
                    entry.get("title") or youtube_video_id,
                    entry.get("url"),
                    entry.get("thumbnail_url"),
                    entry.get("author"),
                    entry.get("summary"),
                    entry.get("published_at"),
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def upsert_entry(entry: dict[str, Any], video_id: int | None, subscription_id: str | None) -> None:
    raw = json.dumps(entry.get("raw") or {})
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO freshrss_entries
                  (id, video_id, subscription_id, title, url, author, published_at, is_read, is_starred, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                  video_id = COALESCE(EXCLUDED.video_id, freshrss_entries.video_id),
                  subscription_id = COALESCE(EXCLUDED.subscription_id, freshrss_entries.subscription_id),
                  title = EXCLUDED.title,
                  url = COALESCE(EXCLUDED.url, freshrss_entries.url),
                  author = COALESCE(EXCLUDED.author, freshrss_entries.author),
                  published_at = COALESCE(EXCLUDED.published_at, freshrss_entries.published_at),
                  is_read = EXCLUDED.is_read,
                  is_starred = EXCLUDED.is_starred,
                  raw = EXCLUDED.raw,
                  updated_at = now()
                """,
                (
                    entry["id"],
                    video_id,
                    subscription_id,
                    entry.get("title") or entry["id"],
                    entry.get("url"),
                    entry.get("author"),
                    entry.get("published_at"),
                    bool(entry.get("is_read")),
                    bool(entry.get("is_starred")),
                    raw,
                ),
            )
        conn.commit()


def list_recent_videos(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred, v.source_kind, v.discovery_source
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def get_video_by_youtube_id(youtube_video_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.summary, v.description, v.duration_seconds, v.view_count, v.like_count,
                       v.dislike_count, v.dislike_source, v.dislike_fetched_at,
                       v.captions_status, v.metadata_source, v.metadata, v.metadata_fetched_at, v.metadata_error,
                       v.source_kind, v.discovery_source, v.discovered_at,
                       v.published_at, v.created_at, v.updated_at,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id, c.subscriber_count AS channel_subscriber_count,
                       fe.is_read, fe.is_starred, fe.subscription_id,
                       fs.title AS subscription_title, fs.feed_url AS subscription_feed_url,
                       cs.id AS channel_subscription_id, cs.feed_url AS channel_subscription_feed_url
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                LEFT JOIN freshrss_subscriptions fs ON fs.id = fe.subscription_id
                -- Subscription state belongs to the channel, not to how this particular
                -- video arrived. Joining only through the RSS entry above reports a
                -- recommended or searched video as unsubscribed even when the channel is
                -- subscribed, because such videos have no freshrss_entries row.
                LEFT JOIN freshrss_subscriptions cs ON cs.youtube_channel_id = c.youtube_channel_id
                WHERE v.youtube_video_id = %s
                ORDER BY fe.published_at DESC NULLS LAST, fe.created_at DESC NULLS LAST
                LIMIT 1
                """,
                (youtube_video_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def search_videos(query: str, limit: int = 50) -> list[dict[str, Any]]:
    cleaned = query.strip()
    if not cleaned:
        return list_recent_videos(limit)

    pattern = f"%{cleaned}%"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.summary, v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred, v.source_kind, v.discovery_source,
                       CASE
                         WHEN v.title ILIKE %s THEN 4
                         WHEN c.title ILIKE %s THEN 3
                         WHEN v.author ILIKE %s THEN 2
                         WHEN v.summary ILIKE %s THEN 1
                         ELSE 0
                       END AS search_score
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                WHERE v.title ILIKE %s
                   OR c.title ILIKE %s
                   OR v.author ILIKE %s
                   OR v.summary ILIKE %s
                ORDER BY search_score DESC, v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, limit),
            )
            return list(cur.fetchall())


def record_video_event(youtube_video_id: str, event_type: str, payload: dict[str, Any] | None = None, user_key: str = "local") -> dict[str, Any] | None:
    raw = json.dumps(payload or {})
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM videos WHERE youtube_video_id = %s", (youtube_video_id,))
            video = cur.fetchone()
            if not video:
                return None
            cur.execute(
                """
                INSERT INTO user_video_events (user_key, video_id, event_type, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id, user_key, video_id, event_type, payload, created_at
                """,
                (user_key, video["id"], event_type, raw),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def _video_id_for_youtube(cur: Any, youtube_video_id: str) -> int | None:
    cur.execute("SELECT id FROM videos WHERE youtube_video_id = %s", (youtube_video_id,))
    row = cur.fetchone()
    return int(row["id"]) if row else None


def get_video_feedback(youtube_video_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT uvf.*
                FROM user_video_feedback uvf
                WHERE uvf.youtube_video_id = %s AND uvf.user_key = %s
                LIMIT 1
                """,
                (youtube_video_id, user_key),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def set_video_feedback(youtube_video_id: str, event_type: str, payload: dict[str, Any] | None = None, user_key: str = "local") -> dict[str, Any] | None:
    payload = payload or {}
    raw = json.dumps(payload)
    with connect() as conn:
        with conn.cursor() as cur:
            video_id = _video_id_for_youtube(cur, youtube_video_id)
            if not video_id:
                return None

            sentinel = object()
            reaction: str | None | object = sentinel
            not_interested: bool | object = sentinel
            reason: str | None | object = sentinel
            if event_type in {"more_like_this", "like"}:
                reaction = "like"
                not_interested = False
                reason = None
            elif event_type in {"less_like_this", "dislike"}:
                reaction = "dislike"
                not_interested = False
                reason = None
            elif event_type in {"clear_reaction", "clear_like", "clear_dislike"}:
                reaction = None
            elif event_type in {"hide", "not_interested"}:
                reaction = None
                not_interested = True
                reason = str(payload.get("reason") or payload.get("shown_reason") or "Not interested")[:500]
            elif event_type in {"undo_hide", "undo_not_interested"}:
                not_interested = False
                reason = None
            elif event_type == "mark_watched":
                cur.execute(
                    """
                    INSERT INTO playback_progress
                      (user_key, video_id, youtube_video_id, completed, manually_marked_watched, percent_completed, last_watched_at, metadata)
                    VALUES (%s, %s, %s, true, true, 1, now(), %s::jsonb)
                    ON CONFLICT (user_key, video_id) DO UPDATE SET
                      completed = true,
                      manually_marked_watched = true,
                      percent_completed = GREATEST(playback_progress.percent_completed, 1),
                      last_watched_at = now(),
                      metadata = playback_progress.metadata || EXCLUDED.metadata,
                      updated_at = now()
                    """,
                    (user_key, video_id, youtube_video_id, raw),
                )

            set_reaction = reaction is not sentinel
            set_interest = not_interested is not sentinel
            set_reason = reason is not sentinel
            cur.execute(
                """
                INSERT INTO user_video_feedback
                  (user_key, video_id, youtube_video_id, reaction, not_interested, not_interested_reason, last_event_type, last_shown_reason, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (user_key, video_id) DO UPDATE SET
                  reaction = CASE WHEN %s THEN EXCLUDED.reaction ELSE user_video_feedback.reaction END,
                  not_interested = CASE WHEN %s THEN EXCLUDED.not_interested ELSE user_video_feedback.not_interested END,
                  not_interested_reason = CASE WHEN %s THEN EXCLUDED.not_interested_reason ELSE user_video_feedback.not_interested_reason END,
                  last_event_type = EXCLUDED.last_event_type,
                  last_shown_reason = COALESCE(EXCLUDED.last_shown_reason, user_video_feedback.last_shown_reason),
                  payload = user_video_feedback.payload || EXCLUDED.payload,
                  updated_at = now()
                RETURNING *
                """,
                (
                    user_key,
                    video_id,
                    youtube_video_id,
                    reaction if set_reaction else None,
                    bool(not_interested) if set_interest else False,
                    reason if set_reason else None,
                    event_type,
                    payload.get("shown_reason") or payload.get("reason"),
                    raw,
                    set_reaction,
                    set_interest,
                    set_reason,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def update_playback_progress(
    youtube_video_id: str,
    current_seconds: float | None = None,
    duration_seconds: float | None = None,
    event_type: str = "progress",
    payload: dict[str, Any] | None = None,
    user_key: str = "local",
    watch_delta_seconds: float | None = None,
    resume_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Record progress, and add `watch_delta_seconds` to the lifetime watch total.

    The delta is sent by the client rather than derived here because current_seconds
    restarts at zero on every page open: subtracting the stored value would score a
    rewatch as negative time.
    """
    payload = payload or {}
    raw = json.dumps(payload)
    current = max(0.0, float(current_seconds or 0))
    duration = float(duration_seconds) if duration_seconds else None
    percent = min(1.0, current / duration) if duration and duration > 0 else 0.0
    completed = percent >= 0.85
    # A single report cannot plausibly carry more than an hour, so a wild value is
    # clamped rather than trusted — this figure feeds ranking.
    delta = max(0.0, min(float(watch_delta_seconds or 0), 3600.0))
    # Near the end there is nothing left to resume, so the mark is dropped rather than
    # dumping the viewer back on the closing seconds next time they open it.
    resume: float | None = None
    if resume_seconds is not None:
        resume = max(0.0, float(resume_seconds))
        if resume < 10.0 or (duration and resume > duration * 0.95):
            resume = None
    with connect() as conn:
        with conn.cursor() as cur:
            video_id = _video_id_for_youtube(cur, youtube_video_id)
            if not video_id:
                return None
            cur.execute(
                """
                INSERT INTO playback_progress
                  (user_key, video_id, youtube_video_id, playback_started_at, last_watched_at, furthest_seconds,
                   current_seconds, duration_seconds, percent_completed, completed, open_count, total_watch_seconds,
                   resume_seconds, metadata)
                VALUES (%s, %s, %s, CASE WHEN %s = 'start' THEN now() ELSE NULL END, now(), %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'open' THEN 1 ELSE 0 END, %s, %s, %s::jsonb)
                ON CONFLICT (user_key, video_id) DO UPDATE SET
                  playback_started_at = COALESCE(playback_progress.playback_started_at, EXCLUDED.playback_started_at),
                  last_watched_at = now(),
                  furthest_seconds = GREATEST(playback_progress.furthest_seconds, EXCLUDED.furthest_seconds),
                  current_seconds = EXCLUDED.current_seconds,
                  duration_seconds = COALESCE(EXCLUDED.duration_seconds, playback_progress.duration_seconds),
                  percent_completed = GREATEST(playback_progress.percent_completed, EXCLUDED.percent_completed),
                  completed = playback_progress.completed OR EXCLUDED.completed,
                  open_count = playback_progress.open_count + EXCLUDED.open_count,
                  total_watch_seconds = playback_progress.total_watch_seconds + EXCLUDED.total_watch_seconds,
                  -- A report with no reading keeps whatever mark is already stored; a
                  -- report that dropped its mark (finished, or too near the start) clears it.
                  resume_seconds = CASE WHEN %s THEN EXCLUDED.resume_seconds ELSE playback_progress.resume_seconds END,
                  metadata = playback_progress.metadata || EXCLUDED.metadata,
                  updated_at = now()
                RETURNING *
                """,
                (user_key, video_id, youtube_video_id, event_type, current, current, duration, percent, completed,
                 event_type, delta, resume, raw, resume_seconds is not None),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def get_playback_progress(youtube_video_id: str, user_key: str = "local") -> dict[str, Any] | None:
    """Stored progress for one video, including where to pick it back up."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM playback_progress
                WHERE user_key = %s AND youtube_video_id = %s
                LIMIT 1
                """,
                (user_key, youtube_video_id),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def list_playback_progress(limit: int = 80, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pp.*, v.title, v.thumbnail_url, v.author, v.published_at, v.duration_seconds AS stored_duration_seconds,
                       v.view_count, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id, v.source_kind, v.discovery_source
                FROM playback_progress pp
                JOIN videos v ON v.id = pp.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE pp.user_key = %s
                ORDER BY pp.updated_at DESC
                LIMIT %s
                """,
                (user_key, limit),
            )
            return list(cur.fetchall())


def create_playlist(name: str, description: str | None = None, user_key: str = "local") -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO playlists (user_key, name, description)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (user_key, name.strip(), description),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def remove_subscription(feed_url: str | None = None, youtube_channel_id: str | None = None) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM freshrss_subscriptions
                WHERE (%s::text IS NOT NULL AND (feed_url = %s OR id = %s OR id = 'feed/' || %s))
                   OR (%s::text IS NOT NULL AND youtube_channel_id = %s)
                """,
                (feed_url, feed_url, feed_url, feed_url, youtube_channel_id, youtube_channel_id),
            )
            count = cur.rowcount or 0
        conn.commit()
    return int(count)



def create_subscription_collection(name: str, description: str | None = None, user_key: str = "local") -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscription_collections (user_key, name, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_key, name) DO UPDATE SET description = EXCLUDED.description, updated_at = now()
                RETURNING *
                """,
                (user_key, name.strip(), description),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def list_subscription_collections(user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sc.*, COUNT(scm.id) AS subscription_count, MAX(scm.added_at) AS latest_member_at,
                       COALESCE(preview.titles, ARRAY[]::text[]) AS member_titles,
                       COALESCE(preview.avatars, ARRAY[]::text[]) AS member_avatars
                FROM subscription_collections sc
                LEFT JOIN subscription_collection_members scm ON scm.collection_id = sc.id
                LEFT JOIN LATERAL (
                  SELECT array_agg(fs.title ORDER BY fs.title ASC) AS titles,
                         array_agg(COALESCE(fs.avatar_url, '') ORDER BY fs.title ASC) AS avatars
                  FROM (
                    SELECT fs.title, c.avatar_url
                    FROM subscription_collection_members m
                    JOIN freshrss_subscriptions fs ON fs.id = m.subscription_id
                    LEFT JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id
                    WHERE m.collection_id = sc.id
                    ORDER BY fs.title ASC
                    LIMIT 4
                  ) fs
                ) preview ON true
                WHERE sc.user_key = %s
                GROUP BY sc.id, preview.titles, preview.avatars
                ORDER BY sc.updated_at DESC, sc.name ASC
                """,
                (user_key,),
            )
            return list(cur.fetchall())


def get_subscription_collection(collection_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sc.*, COUNT(scm.id) AS subscription_count, MAX(scm.added_at) AS latest_member_at
                FROM subscription_collections sc
                LEFT JOIN subscription_collection_members scm ON scm.collection_id = sc.id
                WHERE sc.id = %s AND sc.user_key = %s
                GROUP BY sc.id
                """,
                (collection_id, user_key),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def update_subscription_collection(collection_id: str, name: str | None = None, description: str | None = None, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE subscription_collections
                SET name = COALESCE(NULLIF(%s, ''), name),
                    description = %s,
                    updated_at = now()
                WHERE id = %s AND user_key = %s
                RETURNING *
                """,
                (name, description, collection_id, user_key),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def list_collection_ids_for_subscriptions(user_key: str = "local") -> dict[str, list[str]]:
    """Map subscription id -> collection ids, for showing membership in pickers."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT scm.subscription_id, scm.collection_id
                FROM subscription_collection_members scm
                JOIN subscription_collections sc ON sc.id = scm.collection_id
                WHERE sc.user_key = %s
                """,
                (user_key,),
            )
            mapping: dict[str, list[str]] = {}
            for row in cur.fetchall():
                mapping.setdefault(str(row["subscription_id"]), []).append(str(row["collection_id"]))
            return mapping


def delete_subscription_collection(collection_id: str, user_key: str = "local") -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscription_collections WHERE id = %s AND user_key = %s", (collection_id, user_key))
            count = cur.rowcount or 0
        conn.commit()
    return int(count)


def add_subscription_to_collection(collection_id: str, subscription_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscription_collection_members (collection_id, subscription_id)
                SELECT sc.id, fs.id
                FROM subscription_collections sc
                JOIN freshrss_subscriptions fs ON fs.id = %s
                WHERE sc.id = %s AND sc.user_key = %s
                ON CONFLICT (collection_id, subscription_id) DO UPDATE SET added_at = subscription_collection_members.added_at
                RETURNING *
                """,
                (subscription_id, collection_id, user_key),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def remove_subscription_from_collection(collection_id: str, subscription_id: str, user_key: str = "local") -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM subscription_collection_members scm
                USING subscription_collections sc
                WHERE scm.collection_id = sc.id
                  AND sc.user_key = %s
                  AND scm.collection_id = %s
                  AND scm.subscription_id = %s
                """,
                (user_key, collection_id, subscription_id),
            )
            count = cur.rowcount or 0
        conn.commit()
    return int(count)


def list_collection_subscriptions(collection_id: str, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fs.*, c.avatar_url AS channel_avatar_url, COUNT(v.id) AS video_count, MAX(v.published_at) AS latest_video_at
                FROM subscription_collections sc
                JOIN subscription_collection_members scm ON scm.collection_id = sc.id
                JOIN freshrss_subscriptions fs ON fs.id = scm.subscription_id
                LEFT JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id
                LEFT JOIN videos v ON v.channel_id = c.id
                WHERE sc.id = %s AND sc.user_key = %s
                GROUP BY fs.id, c.avatar_url
                ORDER BY fs.title ASC
                """,
                (collection_id, user_key),
            )
            return list(cur.fetchall())


def list_collection_videos(collection_id: str, limit: int = 80, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred, v.source_kind, v.discovery_source
                FROM subscription_collections sc
                JOIN subscription_collection_members scm ON scm.collection_id = sc.id
                JOIN freshrss_subscriptions fs ON fs.id = scm.subscription_id
                JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id
                JOIN videos v ON v.channel_id = c.id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                WHERE sc.id = %s AND sc.user_key = %s
                ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (collection_id, user_key, limit),
            )
            return list(cur.fetchall())

def list_playlists(user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, COUNT(pi.id) AS video_count, MAX(pi.added_at) AS latest_video_at, cover.thumbnail_url AS cover_thumbnail_url,
                       cover.youtube_video_id AS cover_youtube_video_id
                FROM playlists p
                LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
                LEFT JOIN LATERAL (
                  SELECT v.thumbnail_url, v.youtube_video_id
                  FROM playlist_items x
                  JOIN videos v ON v.id = x.video_id
                  WHERE x.playlist_id = p.id
                  ORDER BY x.position ASC, x.added_at ASC
                  LIMIT 1
                ) cover ON true
                WHERE p.user_key = %s
                GROUP BY p.id, cover.thumbnail_url, cover.youtube_video_id
                ORDER BY p.updated_at DESC
                """,
                (user_key,),
            )
            return list(cur.fetchall())


def update_playlist(playlist_id: str, name: str | None = None, description: str | None = None, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE playlists
                SET name = COALESCE(NULLIF(%s, ''), name),
                    description = %s,
                    updated_at = now()
                WHERE id = %s AND user_key = %s
                RETURNING *
                """,
                (name, description, playlist_id, user_key),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def delete_playlist(playlist_id: str, user_key: str = "local") -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM playlists WHERE id = %s AND user_key = %s", (playlist_id, user_key))
            count = cur.rowcount or 0
        conn.commit()
    return int(count)


def get_playlist(playlist_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, COUNT(pi.id) AS video_count, MAX(pi.added_at) AS latest_video_at
                FROM playlists p
                LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
                WHERE p.id = %s AND p.user_key = %s
                GROUP BY p.id
                """,
                (playlist_id, user_key),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def list_playlist_videos(playlist_id: str, limit: int = 200, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.source_kind, v.discovery_source,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred,
                       pi.position, pi.added_at
                FROM playlist_items pi
                JOIN playlists p ON p.id = pi.playlist_id AND p.user_key = %s
                JOIN videos v ON v.id = pi.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                WHERE pi.playlist_id = %s
                ORDER BY pi.position ASC, pi.added_at ASC
                LIMIT %s
                """,
                (user_key, playlist_id, limit),
            )
            return list(cur.fetchall())


def remove_video_from_playlist(playlist_id: str, youtube_video_id: str, user_key: str = "local") -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM playlist_items pi
                USING playlists p
                WHERE pi.playlist_id = p.id
                  AND p.id = %s
                  AND p.user_key = %s
                  AND pi.youtube_video_id = %s
                """,
                (playlist_id, user_key, youtube_video_id),
            )
            count = cur.rowcount or 0
        conn.commit()
    return int(count)


def add_video_to_playlist(playlist_id: str, youtube_video_id: str, user_key: str = "local") -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            video_id = _video_id_for_youtube(cur, youtube_video_id)
            if not video_id:
                return None
            cur.execute("SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM playlist_items WHERE playlist_id = %s", (playlist_id,))
            position = int((cur.fetchone() or {}).get("next_position") or 1)
            cur.execute(
                """
                INSERT INTO playlist_items (playlist_id, video_id, youtube_video_id, position)
                SELECT p.id, %s, %s, %s FROM playlists p WHERE p.id = %s AND p.user_key = %s
                ON CONFLICT (playlist_id, video_id) DO UPDATE SET added_at = playlist_items.added_at
                RETURNING *
                """,
                (video_id, youtube_video_id, position, playlist_id, user_key),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def list_library_activity(limit: int = 80, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest_event AS (
                  SELECT DISTINCT ON (video_id) video_id, event_type, payload, created_at
                  FROM user_video_events
                  WHERE user_key = %s
                  ORDER BY video_id, created_at DESC
                ), event_rollup AS (
                  SELECT video_id,
                         COUNT(*) AS event_count,
                         COUNT(*) FILTER (WHERE event_type = 'watch_page_open') AS open_count,
                         COUNT(*) FILTER (WHERE event_type = 'more_like_this') AS more_like_count,
                         COUNT(*) FILTER (WHERE event_type = 'less_like_this') AS less_like_count,
                         COUNT(*) FILTER (WHERE event_type = 'mark_watched') AS watched_count,
                         COUNT(*) FILTER (WHERE event_type = 'hide') AS hide_count
                  FROM user_video_events
                  WHERE user_key = %s
                  GROUP BY video_id
                )
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred, v.source_kind, v.discovery_source,
                       le.event_type AS latest_event_type, le.created_at AS latest_event_at,
                       er.event_count, er.open_count, er.more_like_count, er.less_like_count, er.watched_count, er.hide_count
                FROM latest_event le
                JOIN videos v ON v.id = le.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                LEFT JOIN event_rollup er ON er.video_id = v.id
                ORDER BY le.created_at DESC
                LIMIT %s
                """,
                (user_key, user_key, limit),
            )
            return list(cur.fetchall())


def list_recommended_videos(limit: int = 50, offset: int = 0, feed_filter: str = "all", user_key: str = "local") -> list[dict[str, Any]]:
    """Ranked candidates for a feed lane.

    The `recommended` lane ranks on topic and watch-time evidence rather than on who you
    follow, which is what makes it a recommendation rather than a second subscription box.
    """
    # "recommended" is "all" as far as row matching goes; it only changes the weighting.
    recommended_lane = (feed_filter or "").strip().lower() == "recommended"
    if recommended_lane:
        feed_filter = "all"
    requested = max(1, min(int(limit or 50), 200))
    skip = max(0, int(offset or 0))
    candidate_limit = max((requested + skip) * 10, 500)
    subscription_weight = "0.0" if recommended_lane else "6.0"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH event_rollup AS (
                  SELECT video_id,
                         COUNT(*) FILTER (WHERE event_type = 'hide') AS hide_count,
                         COUNT(*) FILTER (WHERE event_type = 'mark_watched') AS watched_count,
                         COUNT(*) FILTER (WHERE event_type = 'watch_page_open') AS open_count,
                         SUM(CASE
                           WHEN event_type = 'more_like_this' THEN 5
                           WHEN event_type = 'less_like_this' THEN -6
                           -- Voting on a comment says something about the video, but far
                           -- less than rating the video itself, so it counts for little.
                           WHEN event_type = 'comment_like' THEN 1
                           WHEN event_type = 'comment_dislike' THEN -1.5
                           ELSE 0
                         END) AS feedback_score
                  FROM user_video_events
                  WHERE user_key = %s
                  GROUP BY video_id
                )
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.created_at, v.duration_seconds, v.view_count, v.tags, v.topic_vector, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred,
                       v.source_kind, v.discovery_source,
                       {ACTIVE_SUBSCRIPTION_SQL} AS is_subscribed_channel,
                       uvf.reaction, COALESCE(uvf.not_interested, false) AS not_interested,
                       COALESCE(pp.percent_completed, 0) AS percent_completed,
                       COALESCE(pp.completed, false) AS progress_completed,
                       COALESCE(pp.manually_marked_watched, false) AS manually_marked_watched,
                       COALESCE(er.feedback_score, 0) AS feedback_score,
                       COALESCE(er.hide_count, 0) AS hide_count,
                       COALESCE(er.watched_count, 0) AS watched_count,
                       COALESCE(er.open_count, 0) AS open_count,
                       COALESCE(er.feedback_score, 0)
                         + CASE WHEN uvf.reaction = 'like' THEN 9.0 WHEN uvf.reaction = 'dislike' THEN -10.0 ELSE 0.0 END
                         -- Zeroed on the recommendation lane. Left in, this alone put every
                         -- subscription video 5.6 points ahead before topics were considered,
                         -- and it also decided which rows survived the candidate LIMIT below.
                         + CASE WHEN v.source_kind = 'subscription' AND {ACTIVE_SUBSCRIPTION_SQL} THEN {subscription_weight} WHEN v.source_kind = 'discovery' THEN 0.4 ELSE 0.0 END
                         + CASE WHEN v.published_at >= now() - interval '3 days' THEN 2.4 WHEN v.published_at >= now() - interval '14 days' THEN 1.4 WHEN v.published_at >= now() - interval '60 days' THEN 0.5 ELSE 0.0 END
                         + CASE WHEN fe.is_starred THEN 4.5 ELSE 0.0 END
                         - CASE WHEN COALESCE(pp.percent_completed, 0) > 0 AND COALESCE(pp.percent_completed, 0) < 0.4 THEN 1.5 ELSE 0.0 END AS recommendation_score
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                LEFT JOIN event_rollup er ON er.video_id = v.id
                LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
                LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = %s
                WHERE CASE WHEN uvf.id IS NULL THEN COALESCE(er.hide_count, 0) = 0 ELSE COALESCE(uvf.not_interested, false) = false END
                  AND CASE WHEN pp.id IS NULL THEN COALESCE(er.watched_count, 0) = 0 ELSE COALESCE(pp.completed, false) = false AND COALESCE(pp.manually_marked_watched, false) = false END
                ORDER BY recommendation_score DESC, v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (user_key, user_key, user_key, candidate_limit),
            )
            rows = list(cur.fetchall())
    scored = _apply_recommendation_scores(rows, feed_filter, user_key, suppress_subscription_bias=recommended_lane)
    diversified = _diversify_recommendations(scored, requested + skip, channel_cap_override=2 if recommended_lane else 0)
    return diversified[skip:skip + requested]


def list_subscription_recommendations(limit: int = 50, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH event_rollup AS (
                  SELECT video_id,
                         COUNT(*) FILTER (WHERE event_type = 'hide') AS hide_count,
                         COUNT(*) FILTER (WHERE event_type = 'mark_watched') AS watched_count,
                         COUNT(*) FILTER (WHERE event_type = 'watch_page_open') AS open_count,
                         SUM(CASE
                           WHEN event_type = 'more_like_this' THEN 5
                           WHEN event_type = 'less_like_this' THEN -6
                           -- Voting on a comment says something about the video, but far
                           -- less than rating the video itself, so it counts for little.
                           WHEN event_type = 'comment_like' THEN 1
                           WHEN event_type = 'comment_dislike' THEN -1.5
                           ELSE 0
                         END) AS feedback_score
                  FROM user_video_events
                  WHERE user_key = %s
                  GROUP BY video_id
                )
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred,
                       v.source_kind, v.discovery_source,
                       uvf.reaction, COALESCE(uvf.not_interested, false) AS not_interested,
                       COALESCE(pp.percent_completed, 0) AS percent_completed,
                       COALESCE(pp.completed, false) AS progress_completed,
                       COALESCE(pp.manually_marked_watched, false) AS manually_marked_watched,
                       COALESCE(er.feedback_score, 0) AS feedback_score,
                       COALESCE(er.hide_count, 0) AS hide_count,
                       COALESCE(er.watched_count, 0) AS watched_count,
                       COALESCE(er.open_count, 0) AS open_count,
                       COALESCE(er.feedback_score, 0)
                         + CASE WHEN uvf.reaction = 'like' THEN 8.0 WHEN uvf.reaction = 'dislike' THEN -8.0 ELSE 0.0 END
                         + 4.0
                         + CASE WHEN v.published_at >= now() - interval '3 days' THEN 2.0 WHEN v.published_at >= now() - interval '14 days' THEN 1.2 WHEN v.published_at >= now() - interval '60 days' THEN 0.4 ELSE 0.0 END
                         + CASE WHEN fe.is_starred THEN 4.0 ELSE 0.0 END
                         - CASE WHEN COALESCE(pp.percent_completed, 0) > 0 AND COALESCE(pp.percent_completed, 0) < 0.4 THEN 1.5 ELSE 0.0 END AS recommendation_score
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                LEFT JOIN event_rollup er ON er.video_id = v.id
                LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
                LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = %s
                WHERE v.source_kind = 'subscription'
                  AND {ACTIVE_SUBSCRIPTION_SQL}
                  AND CASE WHEN uvf.id IS NULL THEN COALESCE(er.hide_count, 0) = 0 ELSE COALESCE(uvf.not_interested, false) = false END
                  AND CASE WHEN pp.id IS NULL THEN COALESCE(er.watched_count, 0) = 0 ELSE COALESCE(pp.completed, false) = false AND COALESCE(pp.manually_marked_watched, false) = false END
                ORDER BY recommendation_score DESC, v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (user_key, user_key, user_key, limit),
            )
            return list(cur.fetchall())


def list_related_videos(
    youtube_video_id: str,
    limit: int = 14,
    user_key: str = "local",
    include_shorts: bool = False,
) -> list[dict[str, Any]]:
    """Up-next ordering for the watch page.

    Takes the ordinary recommendation pool and re-sorts it by closeness to the video
    being watched - shared tags and title words first, same channel second - so the
    side rail reads as "more like this" instead of a second copy of the home feed.
    Shorts are excluded by default: they play in a dedicated vertical viewer, and a
    30-second clip is never a sensible follow-on from a long video.
    """
    requested = max(1, min(int(limit or 14), 40))
    anchor = get_video_by_youtube_id(youtube_video_id) or {}
    anchor_tokens = set(_recommendation_tokens(anchor))
    anchor_channel = _recommendation_channel_key(anchor)

    pool = list_recommended_videos(90, 0, "all", user_key) + list_subscription_recommendations(30, user_key)
    seen = {youtube_video_id}
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for position, row in enumerate(pool):
        candidate_id = str(row.get("youtube_video_id") or "")
        if not candidate_id or candidate_id in seen:
            continue
        if not include_shorts and is_short_video(row):
            continue
        seen.add(candidate_id)
        overlap = len(anchor_tokens & set(_recommendation_tokens(row)))
        score = overlap * 3.0
        if anchor_channel and _recommendation_channel_key(row) == anchor_channel:
            score += 4.0
        # `position` breaks ties, which keeps the pool's own ranking intact for the
        # long tail of candidates that share nothing with the anchor.
        scored.append((score, position, row))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:requested]]


def list_videos_needing_enrichment(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, youtube_video_id, title, url, metadata_fetched_at, metadata_error
                FROM videos
                WHERE metadata_fetched_at IS NULL
                   OR (metadata_error IS NOT NULL AND metadata_fetched_at < now() - interval '6 hours')
                ORDER BY published_at DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def _int_or_zero(value: Any) -> int:
    """Unix seconds as an int, or 0 when absent — NULLIF turns 0 back into NULL."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def update_video_metadata(youtube_video_id: str, metadata: dict[str, Any], source: str) -> int | None:
    raw = json.dumps(metadata.get("raw") or metadata)
    tags = _tags_from_metadata(metadata)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET title = COALESCE(NULLIF(%s, ''), title),
                    thumbnail_url = COALESCE(NULLIF(%s, ''), thumbnail_url),
                    author = COALESCE(NULLIF(%s, ''), author),
                    summary = COALESCE(NULLIF(%s, ''), summary),
                    description = COALESCE(NULLIF(%s, ''), description),
                    duration_seconds = COALESCE(%s, duration_seconds),
                    -- Only filled when missing: the feed's own date is authoritative
                    -- where we have one, and this backfills the discovered videos.
                    published_at = COALESCE(published_at, to_timestamp(NULLIF(%s, 0))),
                    view_count = COALESCE(%s, view_count),
                    like_count = COALESCE(%s, like_count),
                    dislike_count = COALESCE(%s, dislike_count),
                    captions_status = COALESCE(NULLIF(%s, ''), captions_status),
                    tags = CASE WHEN %s::text[] = '{}'::text[] THEN tags ELSE %s::text[] END,
                    metadata_source = %s,
                    metadata = %s::jsonb,
                    metadata_fetched_at = now(),
                    metadata_error = NULL,
                    updated_at = now()
                WHERE youtube_video_id = %s
                RETURNING id
                """,
                (
                    metadata.get("title"),
                    metadata.get("thumbnail_url"),
                    metadata.get("author"),
                    metadata.get("summary"),
                    metadata.get("description"),
                    metadata.get("duration_seconds"),
                    _int_or_zero(metadata.get("published_at")),
                    metadata.get("view_count"),
                    metadata.get("like_count"),
                    metadata.get("dislike_count"),
                    metadata.get("captions_status"),
                    tags,
                    tags,
                    source,
                    raw,
                    youtube_video_id,
                ),
            )
            row = cur.fetchone()
            if row:
                channel_key = str(metadata.get("youtube_channel_id") or "").strip()
                subscriber_count = metadata.get("subscriber_count")
                existing = None
                if channel_key:
                    cur.execute("SELECT id FROM channels WHERE youtube_channel_id = %s", (channel_key,))
                    existing = cur.fetchone()

                if existing:
                    # A discovered video creates a channel row from its title alone. Once
                    # enrichment learns the real channel id, stamping it onto that
                    # placeholder collides with the canonical row for the same channel —
                    # a unique violation that aborted the whole enrichment and left the
                    # video with no metadata at all. Repointing the video at the canonical
                    # row converges the duplicates instead of failing.
                    cur.execute(
                        "UPDATE videos SET channel_id = %s, updated_at = now() "
                        "WHERE youtube_video_id = %s AND channel_id IS DISTINCT FROM %s",
                        (existing["id"], youtube_video_id, existing["id"]),
                    )
                    cur.execute(
                        "UPDATE channels SET subscriber_count = COALESCE(%s, subscriber_count), "
                        "updated_at = now() WHERE id = %s",
                        (subscriber_count, existing["id"]),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE channels c
                        SET youtube_channel_id = COALESCE(NULLIF(%s, ''), c.youtube_channel_id),
                            subscriber_count = COALESCE(%s, c.subscriber_count),
                            updated_at = now()
                        FROM videos v
                        WHERE v.channel_id = c.id AND v.youtube_video_id = %s
                        """,
                        (channel_key, subscriber_count, youtube_video_id),
                    )
        conn.commit()
    return int(row["id"]) if row else None


def mark_video_metadata_error(youtube_video_id: str, message: str) -> int | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET metadata_error = %s, metadata_fetched_at = now(), updated_at = now()
                WHERE youtube_video_id = %s
                RETURNING id
                """,
                (message[:1000], youtube_video_id),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"]) if row else None


def record_retrieval_attempt(video_id: int | None, source: str, status: str, message: str | None = None) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO retrieval_attempts (video_id, source, status, message)
                VALUES (%s, %s, %s, %s)
                """,
                (video_id, source, status, message),
            )
        conn.commit()


def enrichment_status() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*) AS total_videos,
                  COUNT(*) FILTER (WHERE metadata_fetched_at IS NOT NULL AND metadata_error IS NULL) AS enriched_videos,
                  COUNT(*) FILTER (WHERE metadata_error IS NOT NULL) AS metadata_errors,
                  MAX(metadata_fetched_at) AS latest_metadata_fetch
                FROM videos
                """
            )
            row = cur.fetchone()
    return dict(row) if row else {}


def list_discovery_videos(limit: int = 50, user_key: str = "local") -> list[dict[str, Any]]:
    candidate_limit = max(limit * 6, 120)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH event_rollup AS (
                  SELECT video_id,
                         COUNT(*) FILTER (WHERE event_type = 'hide') AS hide_count,
                         COUNT(*) FILTER (WHERE event_type = 'mark_watched') AS watched_count,
                         COUNT(*) FILTER (WHERE event_type = 'watch_page_open') AS open_count,
                         SUM(CASE
                           WHEN event_type = 'more_like_this' THEN 5
                           WHEN event_type = 'less_like_this' THEN -6
                           -- Voting on a comment says something about the video, but far
                           -- less than rating the video itself, so it counts for little.
                           WHEN event_type = 'comment_like' THEN 1
                           WHEN event_type = 'comment_dislike' THEN -1.5
                           ELSE 0
                         END) AS feedback_score
                  FROM user_video_events
                  WHERE user_key = %s
                  GROUP BY video_id
                )
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred, v.source_kind, v.discovery_source,
                       COALESCE(er.feedback_score, 0) AS feedback_score,
                       COALESCE(er.hide_count, 0) AS hide_count,
                       COALESCE(er.watched_count, 0) AS watched_count,
                       COALESCE(er.open_count, 0) AS open_count,
                       COALESCE(er.feedback_score, 0)
                         + CASE WHEN uvf.reaction = 'like' THEN 8.0 WHEN uvf.reaction = 'dislike' THEN -8.0 ELSE 0.0 END
                         + 0.75
                         + CASE WHEN v.published_at >= now() - interval '3 days' THEN 2.0 WHEN v.published_at >= now() - interval '14 days' THEN 1.2 WHEN v.published_at >= now() - interval '60 days' THEN 0.4 ELSE 0.0 END
                         - CASE WHEN COALESCE(pp.percent_completed, 0) > 0 AND COALESCE(pp.percent_completed, 0) < 0.4 THEN 1.5 ELSE 0.0 END AS recommendation_score
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                LEFT JOIN event_rollup er ON er.video_id = v.id
                LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %s
                LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = %s
                WHERE v.source_kind = 'discovery'
                  AND CASE WHEN uvf.id IS NULL THEN COALESCE(er.hide_count, 0) = 0 ELSE COALESCE(uvf.not_interested, false) = false END
                  AND CASE WHEN pp.id IS NULL THEN COALESCE(er.watched_count, 0) = 0 ELSE COALESCE(pp.completed, false) = false AND COALESCE(pp.manually_marked_watched, false) = false END
                ORDER BY recommendation_score DESC, v.discovered_at DESC NULLS LAST, v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (user_key, user_key, user_key, candidate_limit),
            )
            rows = list(cur.fetchall())
    scored = _apply_recommendation_scores(rows, "discovery", user_key)
    return _diversify_recommendations(scored, limit)


def list_discovery_seed_videos(limit: int = 5) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.youtube_video_id, v.title
                FROM videos v
                LEFT JOIN retrieval_attempts ra
                  ON ra.video_id = v.id AND ra.source = 'invidious_related'
                WHERE v.youtube_video_id IS NOT NULL
                GROUP BY v.id
                ORDER BY COUNT(ra.id) ASC, v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def upsert_discovered_video(item: dict[str, Any], source_video_id: int | None, source: str, reason: str | None = None) -> int | None:
    youtube_video_id = item.get("youtube_video_id")
    if not youtube_video_id:
        return None
    channel_id = upsert_channel(item.get("channel_title") or item.get("author") or "Unknown channel", item.get("youtube_channel_id"), item.get("feed_url"))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO videos
                  (youtube_video_id, channel_id, title, url, thumbnail_url, author, summary, published_at,
                   duration_seconds, view_count, source_kind, discovery_source, discovered_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'discovery', %s, now())
                ON CONFLICT (youtube_video_id) DO UPDATE SET
                  channel_id = COALESCE(EXCLUDED.channel_id, videos.channel_id),
                  title = COALESCE(NULLIF(EXCLUDED.title, ''), videos.title),
                  url = COALESCE(EXCLUDED.url, videos.url),
                  thumbnail_url = COALESCE(EXCLUDED.thumbnail_url, videos.thumbnail_url),
                  author = COALESCE(EXCLUDED.author, videos.author),
                  summary = COALESCE(EXCLUDED.summary, videos.summary),
                  published_at = COALESCE(EXCLUDED.published_at, videos.published_at),
                  duration_seconds = COALESCE(EXCLUDED.duration_seconds, videos.duration_seconds),
                  view_count = COALESCE(EXCLUDED.view_count, videos.view_count),
                  source_kind = CASE WHEN videos.source_kind = 'subscription' THEN videos.source_kind ELSE 'discovery' END,
                  discovery_source = COALESCE(videos.discovery_source, EXCLUDED.discovery_source),
                  discovered_at = COALESCE(videos.discovered_at, now()),
                  updated_at = now()
                RETURNING id
                """,
                (
                    youtube_video_id,
                    channel_id,
                    item.get("title") or youtube_video_id,
                    item.get("url") or f"https://www.youtube.com/watch?v={youtube_video_id}",
                    item.get("thumbnail_url"),
                    item.get("author") or item.get("channel_title"),
                    item.get("summary"),
                    item.get("published_at"),
                    item.get("duration_seconds"),
                    item.get("view_count"),
                    source,
                ),
            )
            row = cur.fetchone()
            discovered_id = int(row["id"]) if row else None
            if discovered_id and source_video_id and discovered_id != source_video_id:
                cur.execute(
                    """
                    INSERT INTO video_discoveries (source_video_id, discovered_video_id, source, reason)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source_video_id, discovered_video_id, source) DO NOTHING
                    """,
                    (source_video_id, discovered_id, source, reason),
                )
        conn.commit()
    return discovered_id


def list_subscriptions(limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fs.id, fs.title, fs.feed_url, fs.html_url, fs.youtube_channel_id, fs.updated_at,
                       c.avatar_url AS channel_avatar_url,
                       COUNT(fe.id) AS video_count,
                       MAX(COALESCE(fe.published_at, fe.created_at)) AS latest_video_at
                FROM freshrss_subscriptions fs
                LEFT JOIN freshrss_entries fe ON fe.subscription_id = fs.id
                LEFT JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id
                GROUP BY fs.id, fs.title, fs.feed_url, fs.html_url, fs.youtube_channel_id, fs.updated_at, c.avatar_url
                ORDER BY fs.title ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def list_playlist_subscriptions(limit: int = 80) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fs.id, fs.title, fs.feed_url, fs.html_url, fs.youtube_channel_id, fs.updated_at,
                       COUNT(fe.id) AS video_count,
                       MAX(COALESCE(fe.published_at, fe.created_at)) AS latest_video_at
                FROM freshrss_subscriptions fs
                LEFT JOIN freshrss_entries fe ON fe.subscription_id = fs.id
                WHERE fs.feed_url ILIKE '%%playlist%%'
                   OR fs.html_url ILIKE '%%playlist%%'
                   OR fs.id ILIKE '%%playlist%%'
                   OR fs.categories::text ILIKE '%%playlist%%'
                GROUP BY fs.id, fs.title, fs.feed_url, fs.html_url, fs.youtube_channel_id, fs.updated_at
                ORDER BY latest_video_at DESC NULLS LAST, fs.title ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def list_liked_videos(limit: int = 80, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH liked_events AS (
                  SELECT video_id, MAX(created_at) AS liked_at
                  FROM user_video_events
                  WHERE event_type = 'more_like_this' AND user_key = %s
                  GROUP BY video_id
                  UNION ALL
                  SELECT video_id, MAX(updated_at) AS liked_at
                  FROM user_video_feedback
                  WHERE user_key = %s AND reaction = 'like'
                  GROUP BY video_id
                ), liked_rollup AS (
                  SELECT video_id, MAX(liked_at) AS liked_at
                  FROM liked_events
                  GROUP BY video_id
                ), starred_entries AS (
                  SELECT video_id, MAX(updated_at) AS starred_at
                  FROM freshrss_entries
                  WHERE is_starred = true AND video_id IS NOT NULL
                  GROUP BY video_id
                ), liked_rows AS (
                  SELECT DISTINCT ON (v.id)
                         v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                         v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                         fe.is_read, fe.is_starred, v.source_kind, v.discovery_source,
                         CASE WHEN le.liked_at IS NOT NULL THEN 'liked' ELSE 'starred' END AS latest_event_type,
                         GREATEST(COALESCE(le.liked_at, TIMESTAMPTZ 'epoch'), COALESCE(se.starred_at, TIMESTAMPTZ 'epoch')) AS latest_event_at
                  FROM videos v
                  LEFT JOIN liked_rollup le ON le.video_id = v.id
                  LEFT JOIN starred_entries se ON se.video_id = v.id
                  LEFT JOIN channels c ON c.id = v.channel_id
                  LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                  WHERE le.video_id IS NOT NULL OR se.video_id IS NOT NULL
                  ORDER BY v.id, latest_event_at DESC, v.published_at DESC NULLS LAST
                )
                SELECT *
                FROM liked_rows
                ORDER BY latest_event_at DESC NULLS LAST, published_at DESC NULLS LAST
                LIMIT %s
                """,
                (user_key, user_key, limit),
            )
            return list(cur.fetchall())


def list_history_videos(limit: int = 80, user_key: str = "local") -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH history_events AS (
                  SELECT video_id,
                         MAX(created_at) AS latest_event_at,
                         COUNT(*) FILTER (WHERE event_type = 'watch_page_open') AS open_count,
                         COUNT(*) FILTER (WHERE event_type = 'mark_watched') AS watched_count
                  FROM user_video_events
                  WHERE event_type IN ('watch_page_open', 'mark_watched') AND user_key = %s
                  GROUP BY video_id
                ), history_rows AS (
                  SELECT DISTINCT ON (v.id)
                         v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                         v.published_at, v.duration_seconds, v.view_count, v.tags, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                         fe.is_read, fe.is_starred, v.source_kind, v.discovery_source,
                         CASE WHEN he.watched_count > 0 THEN 'watched' ELSE 'opened' END AS latest_event_type,
                         he.latest_event_at, he.open_count, he.watched_count
                  FROM history_events he
                  JOIN videos v ON v.id = he.video_id
                  LEFT JOIN channels c ON c.id = v.channel_id
                  LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                  ORDER BY v.id, he.latest_event_at DESC
                )
                SELECT *
                FROM history_rows
                ORDER BY latest_event_at DESC NULLS LAST, published_at DESC NULLS LAST
                LIMIT %s
                """,
                (user_key, limit),
            )
            return list(cur.fetchall())


def list_channels(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.youtube_channel_id, c.title, c.feed_url, c.updated_at,
                       c.avatar_url, c.banner_url, c.subscriber_count,
                       COUNT(v.id) AS video_count,
                       MAX(v.published_at) AS latest_video_at
                FROM channels c
                LEFT JOIN videos v ON v.channel_id = c.id
                GROUP BY c.id
                ORDER BY latest_video_at DESC NULLS LAST, c.title ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def update_channel_media(youtube_channel_id: str, metadata: dict[str, Any]) -> bool:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE channels
                SET avatar_url = COALESCE(NULLIF(%s, ''), avatar_url),
                    banner_url = COALESCE(NULLIF(%s, ''), banner_url),
                    subscriber_count = COALESCE(%s, subscriber_count),
                    description = COALESCE(NULLIF(%s, ''), description),
                    handle = COALESCE(NULLIF(%s, ''), handle),
                    verified = COALESCE(%s, verified),
                    available_tabs = COALESCE(%s, available_tabs),
                    updated_at = now()
                WHERE youtube_channel_id = %s
                """,
                (
                    metadata.get("avatar_url"),
                    metadata.get("banner_url"),
                    metadata.get("subscriber_count"),
                    metadata.get("description"),
                    metadata.get("handle"),
                    metadata.get("verified"),
                    metadata.get("available_tabs") or None,
                    youtube_channel_id,
                ),
            )
            updated = (cur.rowcount or 0) > 0
        conn.commit()
    return updated


def list_channels_needing_media(limit: int = 10, refresh: bool = False) -> list[dict[str, Any]]:
    """Channels with no avatar yet, subscriptions first.

    Ordering matters more than it looks: discovered channels outnumber subscribed ones by
    roughly ten to one, so a queue drained in `updated_at` order spends days on channels
    that appear nowhere in the interface while the sidebar sits full of blank circles.
    """
    condition = "TRUE" if refresh else "c.avatar_url IS NULL"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.id, c.youtube_channel_id, c.title,
                       (fs.id IS NOT NULL) AS subscribed
                FROM channels c
                LEFT JOIN freshrss_subscriptions fs ON fs.youtube_channel_id = c.youtube_channel_id
                WHERE c.youtube_channel_id IS NOT NULL AND {condition}
                ORDER BY (fs.id IS NOT NULL) DESC, c.avatar_url IS NOT NULL, c.updated_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def count_subscription_channels_missing_media() -> int:
    """How many subscribed channels still have no avatar."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS missing
                FROM freshrss_subscriptions fs
                JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id
                WHERE c.avatar_url IS NULL
                """
            )
            row = cur.fetchone()
            return int((row or {}).get("missing") or 0)


def list_subscription_channels_needing_media(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.youtube_channel_id, c.title
                FROM freshrss_subscriptions fs
                JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id
                WHERE c.avatar_url IS NULL
                ORDER BY fs.title ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def get_channel_by_youtube_id(youtube_channel_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.youtube_channel_id, c.title, c.feed_url, c.updated_at,
                       c.avatar_url, c.banner_url, c.subscriber_count,
                       COUNT(v.id) AS video_count,
                       MAX(v.published_at) AS latest_video_at,
                       c.description, c.handle, c.verified, c.available_tabs,
                       fs.id AS subscription_id, fs.title AS subscription_title, fs.html_url AS subscription_html_url
                FROM channels c
                LEFT JOIN videos v ON v.channel_id = c.id
                LEFT JOIN freshrss_subscriptions fs ON fs.youtube_channel_id = c.youtube_channel_id
                WHERE c.youtube_channel_id = %s
                GROUP BY c.id, fs.id, fs.title, fs.html_url
                LIMIT 1
                """,
                (youtube_channel_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def list_channel_videos(youtube_channel_id: str, limit: int = 50, sort: str = "latest", offset: int = 0) -> list[dict[str, Any]]:
    order_by = "v.published_at DESC NULLS LAST, v.created_at DESC"
    if sort == "oldest":
        order_by = "v.published_at ASC NULLS LAST, v.created_at ASC"
    elif sort == "popular":
        order_by = "v.view_count DESC NULLS LAST, v.published_at DESC NULLS LAST"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
                       v.published_at, v.metadata_source, v.duration_seconds, v.view_count, v.source_kind, v.discovery_source,
                       c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
                       fe.is_read, fe.is_starred, v.source_kind, v.discovery_source
                FROM videos v
                JOIN channels c ON c.id = v.channel_id
                LEFT JOIN freshrss_entries fe ON fe.video_id = v.id
                WHERE c.youtube_channel_id = %s
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
                """,
                (youtube_channel_id, limit, max(0, int(offset or 0))),
            )
            return list(cur.fetchall())


def list_sync_runs(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source, started_at, finished_at, status, message,
                       subscriptions_count, entries_count
                FROM sync_runs
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


# --- content-based similarity --------------------------------------------------------

def term_document_counts() -> tuple[dict[str, int], int]:
    """How many videos each term appears in, plus the corpus size.

    Computed from the stored vectors rather than by re-tokenising every description: the
    vectors already hold the terms that matter, and this runs on a timer.
    """
    from . import topics

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.title, v.summary, v.tags, c.title AS channel_title, v.author
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE v.title IS NOT NULL
                """
            )
            rows = list(cur.fetchall())
    counts: dict[str, int] = {}
    for row in rows:
        for term in set(topics.document_terms(dict(row))):
            counts[term] = counts.get(term, 0) + 1
    return counts, len(rows)


def videos_needing_topic_vectors(limit: int = 500, force: bool = False) -> list[dict[str, Any]]:
    clause = "" if force else "WHERE v.topic_vector IS NULL"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT v.id, v.title, v.summary, v.tags, c.title AS channel_title, v.author
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                {clause}
                ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC
                LIMIT %s
                """,
                (max(1, min(limit, 2000)),),
            )
            return list(cur.fetchall())


def store_topic_vectors(updates: list[tuple[int, str]]) -> int:
    if not updates:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE videos SET topic_vector = %s::jsonb, topic_vector_at = now() WHERE id = %s",
                [(payload, video_id) for video_id, payload in updates],
            )
        conn.commit()
    return len(updates)


def watched_topic_vectors(user_key: str = "local", limit: int = 120) -> list[dict[str, Any]]:
    """Vectors for what this user actually watched, newest first."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.topic_vector, pp.total_watch_seconds, pp.completed, pp.last_watched_at
                FROM playback_progress pp
                JOIN videos v ON v.id = pp.video_id
                WHERE pp.user_key = %s
                  AND v.topic_vector IS NOT NULL
                  AND (pp.total_watch_seconds > 0 OR pp.completed)
                ORDER BY pp.last_watched_at DESC
                LIMIT %s
                """,
                (user_key, max(1, min(limit, 500))),
            )
            return list(cur.fetchall())


def store_takeout_subscriptions(items: list[dict[str, Any]], user_key: str = "local") -> int:
    """Record the channels an archive says the user follows.

    `subscribed_at` is preserved across re-imports so a channel already pushed to FreshRSS
    is not offered again.
    """
    rows = [
        (
            user_key,
            str(item.get("youtube_channel_id") or item.get("channel_id") or "").strip(),
            str(item.get("title") or "").strip(),
            str(item.get("channel_url") or item.get("url") or "").strip(),
        )
        for item in items or []
    ]
    rows = [row for row in rows if row[1]]
    if not rows:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO takeout_subscriptions (user_key, youtube_channel_id, title, channel_url)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_key, youtube_channel_id) DO UPDATE SET
                  title = COALESCE(NULLIF(EXCLUDED.title, ''), takeout_subscriptions.title),
                  channel_url = COALESCE(NULLIF(EXCLUDED.channel_url, ''), takeout_subscriptions.channel_url),
                  imported_at = now()
                """,
                rows,
            )
        conn.commit()
    return len(rows)


def pending_takeout_subscriptions(user_key: str = "local", limit: int = 500) -> list[dict[str, Any]]:
    """Imported channels not yet subscribed here, and not already in FreshRSS."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts.youtube_channel_id, ts.title, ts.channel_url
                FROM takeout_subscriptions ts
                LEFT JOIN freshrss_subscriptions fs ON fs.youtube_channel_id = ts.youtube_channel_id
                WHERE ts.user_key = %s AND ts.subscribed_at IS NULL AND ts.failed_at IS NULL AND fs.id IS NULL
                ORDER BY ts.title NULLS LAST
                LIMIT %s
                """,
                (user_key, max(1, min(limit, 2000))),
            )
            return list(cur.fetchall())


def takeout_subscription_counts(user_key: str = "local") -> dict[str, int]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE ts.subscribed_at IS NOT NULL) AS applied,
                       COUNT(*) FILTER (WHERE ts.subscribed_at IS NULL AND fs.id IS NOT NULL) AS already_present,
                       COUNT(*) FILTER (WHERE ts.subscribed_at IS NULL AND ts.failed_at IS NOT NULL) AS unavailable
                FROM takeout_subscriptions ts
                LEFT JOIN freshrss_subscriptions fs ON fs.youtube_channel_id = ts.youtube_channel_id
                WHERE ts.user_key = %s
                """,
                (user_key,),
            )
            row = cur.fetchone() or {}
    total = int(row.get("total") or 0)
    applied = int(row.get("applied") or 0)
    present = int(row.get("already_present") or 0)
    unavailable = int(row.get("unavailable") or 0)
    return {
        "total": total,
        "applied": applied,
        "already_present": present,
        "unavailable": unavailable,
        "pending": max(0, total - applied - present - unavailable),
    }


def mark_takeout_subscribed(youtube_channel_ids: list[str], user_key: str = "local") -> int:
    if not youtube_channel_ids:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE takeout_subscriptions SET subscribed_at = now() WHERE user_key = %s AND youtube_channel_id = ANY(%s)",
                (user_key, list(youtube_channel_ids)),
            )
            count = cur.rowcount
        conn.commit()
    return count


def channel_video_counts(youtube_channel_ids: list[str]) -> dict[str, int]:
    """How many videos we already hold per channel, keyed lowercase."""
    if not youtube_channel_ids:
        return {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lower(c.youtube_channel_id) AS key, COUNT(v.id) AS n
                FROM channels c LEFT JOIN videos v ON v.channel_id = c.id
                WHERE lower(c.youtube_channel_id) = ANY(%s)
                GROUP BY 1
                """,
                ([key.lower() for key in youtube_channel_ids],),
            )
            return {str(row["key"]): int(row["n"] or 0) for row in cur.fetchall()}


def takeout_channels_by_affinity(user_key: str = "local", limit: int = 60) -> list[dict[str, Any]]:
    """Imported channels in recency-weighted order, with their real IDs.

    The cached profile lowercases channel keys for matching, which is fine for scoring and
    useless for calling an API: YouTube channel IDs are case-sensitive.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT youtube_channel_id, title, watch_count, last_watched_at
                FROM takeout_channel_affinity
                WHERE user_key = %s AND youtube_channel_id IS NOT NULL
                """,
                (user_key,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row["weight"] = int(row.get("watch_count") or 0) * recency_weight(row.get("last_watched_at"))
    rows.sort(key=lambda row: row["weight"], reverse=True)
    return rows[: max(1, min(limit, 500))]


def mark_takeout_subscription_failed(youtube_channel_id: str, reason: str, user_key: str = "local") -> None:
    """Record a permanent failure so the channel stops being offered.

    Only for failures that cannot resolve themselves: a deleted channel has no feed to
    subscribe to, and retrying it every pass makes the button look broken.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE takeout_subscriptions SET failed_at = now(), failure_reason = %s
                WHERE user_key = %s AND youtube_channel_id = %s
                """,
                (reason[:200], user_key, youtube_channel_id),
            )
        conn.commit()


def retry_failed_takeout_subscriptions(user_key: str = "local") -> int:
    """Clear the failure marks so a later attempt can try them again."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE takeout_subscriptions SET failed_at = NULL, failure_reason = NULL WHERE user_key = %s AND failed_at IS NOT NULL",
                (user_key,),
            )
            count = cur.rowcount
        conn.commit()
    return count


def get_poll_vote(post_id: str, user_key: str = "local") -> int | None:
    """Which option this user picked, if any. Never sent to YouTube."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT choice_index FROM post_poll_votes WHERE user_key = %s AND post_id = %s",
                (user_key, post_id),
            )
            row = cur.fetchone()
    return int(row["choice_index"]) if row else None


def set_poll_vote(post_id: str, choice_index: int, user_key: str = "local") -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO post_poll_votes (user_key, post_id, choice_index)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_key, post_id) DO UPDATE SET choice_index = EXCLUDED.choice_index, created_at = now()
                """,
                (user_key, post_id, max(0, int(choice_index))),
            )
        conn.commit()
    return max(0, int(choice_index))


def clear_poll_vote(post_id: str, user_key: str = "local") -> bool:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM post_poll_votes WHERE user_key = %s AND post_id = %s", (user_key, post_id))
            removed = cur.rowcount > 0
        conn.commit()
    return removed


def top_feed_channels(user_key: str = "local", limit: int = 14) -> list[dict[str, Any]]:
    """Creators currently filling the recommendation feed, most-present first.

    Offered as mute targets because muting is only useful against what you are actually
    being shown; a list of every channel in the library would be noise.
    """
    from collections import Counter

    rows = list_recommended_videos(120, 0, "recommended", user_key)
    counts: Counter[str] = Counter()
    titles: dict[str, str] = {}
    for row in rows:
        key = _recommendation_channel_key(row)
        if not key:
            continue
        counts[key] += 1
        titles.setdefault(key, str(row.get("channel_title") or row.get("author") or key))
    return [
        {"key": key, "title": titles.get(key, key), "count": count}
        for key, count in counts.most_common(max(1, min(limit, 50)))
    ]


def channel_ids_for_titles(titles: list[str]) -> dict[str, str]:
    """Resolve channel names to ids, lowercased for matching.

    Used to link the guests on a collaboration. Only exact title matches count — a fuzzy
    match here would send someone to the wrong channel, which is worse than plain text.
    """
    cleaned = [str(title).strip() for title in titles if str(title or "").strip()]
    if not cleaned:
        return {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (lower(title)) lower(title) AS key, youtube_channel_id
                FROM channels
                WHERE youtube_channel_id IS NOT NULL AND lower(title) = ANY(%s)
                """,
                ([title.lower() for title in cleaned],),
            )
            return {str(row["key"]): str(row["youtube_channel_id"]) for row in cur.fetchall()}
