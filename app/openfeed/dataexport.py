"""Settings -> Account -> Your data: everything one profile keeps here, as one JSON file.

Only rows that belong to the requesting user_key go in, the subscription list included:
subscriptions are per profile (user_subscriptions), so the file holds this profile's own
list and nobody else's. Nothing here is fetched from YouTube or Invidious; the export is
what this database holds.

The subscription list also comes out as OPML, the format every feed reader imports,
since each subscription already is a YouTube RSS feed.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable
from xml.sax.saxutils import escape

from . import __version__, db

EXPORT_FORMAT = "openfeed-export"
EXPORT_VERSION = 1

# Every query is filtered by %(user)s.
_QUERIES: dict[str, str] = {
    "subscriptions": """
        SELECT fs.id, fs.title, fs.feed_url, fs.html_url, fs.youtube_channel_id, us.created_at AS subscribed_at
        FROM feeds fs
        JOIN user_subscriptions us ON us.feed_id = fs.id
        WHERE us.user_key = %(user)s
        ORDER BY lower(fs.title) ASC, fs.id ASC
    """,
    "collections": """
        SELECT sc.id, sc.name, sc.description, sc.created_at, sc.updated_at,
               scm.subscription_id, fs.title AS subscription_title, fs.youtube_channel_id, scm.added_at
        FROM subscription_collections sc
        LEFT JOIN subscription_collection_members scm ON scm.collection_id = sc.id
        LEFT JOIN feeds fs ON fs.id = scm.subscription_id
        WHERE sc.user_key = %(user)s
        ORDER BY lower(sc.name) ASC, sc.id, scm.added_at ASC NULLS LAST
    """,
    "playlists": """
        SELECT p.id, p.name, p.description, p.visibility, p.created_at, p.updated_at,
               pi.youtube_video_id, pi.position, pi.note, pi.added_at,
               v.title AS video_title, COALESCE(c.title, v.author) AS channel_title,
               c.youtube_channel_id, v.duration_seconds
        FROM playlists p
        LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
        LEFT JOIN videos v ON v.id = pi.video_id
        LEFT JOIN channels c ON c.id = v.channel_id
        WHERE p.user_key = %(user)s
        ORDER BY p.created_at ASC, p.id, pi.position ASC NULLS LAST, pi.added_at ASC NULLS LAST
    """,
    "liked_videos": """
        SELECT f.youtube_video_id, f.updated_at AS liked_at,
               v.title AS video_title, COALESCE(c.title, v.author) AS channel_title, c.youtube_channel_id
        FROM user_video_feedback f
        LEFT JOIN videos v ON v.id = f.video_id
        LEFT JOIN channels c ON c.id = v.channel_id
        WHERE f.user_key = %(user)s AND f.reaction = 'like'
        ORDER BY f.updated_at DESC
    """,
    "watch_history": """
        SELECT pp.youtube_video_id, v.title AS video_title, COALESCE(c.title, v.author) AS channel_title,
               c.youtube_channel_id, pp.first_opened_at, pp.playback_started_at, pp.last_watched_at,
               pp.resume_seconds, pp.furthest_seconds, pp.duration_seconds, pp.percent_completed,
               pp.total_watch_seconds, pp.completed, pp.manually_marked_watched, pp.open_count
        FROM playback_progress pp
        LEFT JOIN videos v ON v.id = pp.video_id
        LEFT JOIN channels c ON c.id = v.channel_id
        WHERE pp.user_key = %(user)s
        ORDER BY COALESCE(pp.last_watched_at, pp.updated_at) DESC
    """,
    "video_feedback": """
        SELECT f.youtube_video_id, v.title AS video_title, COALESCE(c.title, v.author) AS channel_title,
               f.reaction, f.not_interested, f.not_interested_reason, f.last_event_type, f.updated_at
        FROM user_video_feedback f
        LEFT JOIN videos v ON v.id = f.video_id
        LEFT JOIN channels c ON c.id = v.channel_id
        WHERE f.user_key = %(user)s AND (f.not_interested OR f.reaction = 'dislike')
        ORDER BY f.updated_at DESC
    """,
    "feedback_requests": """
        SELECT v.youtube_video_id, v.title AS video_title, e.event_type, e.created_at
        FROM user_video_events e
        JOIN videos v ON v.id = e.video_id
        WHERE e.user_key = %(user)s AND e.event_type IN ('more_like_this', 'less_like_this')
        ORDER BY e.created_at DESC
    """,
    "settings": """
        SELECT u.profile,
               COALESCE((SELECT json_agg(json_build_object('topic', t.topic, 'preference', t.preference) ORDER BY t.topic)
                         FROM user_topic_preferences t WHERE t.user_id = u.id), '[]'::json) AS topics
        FROM users u
        WHERE u.authentik_sub = %(user)s
    """,
    "notifications": """
        SELECT youtube_channel_id, level, updated_at
        FROM channel_notifications
        WHERE user_key = %(user)s
        ORDER BY youtube_channel_id
    """,
    # Why this profile may be shown each shared-catalog video (catalog_scope.py).
    "catalog_reasons": """
        SELECT v.youtube_video_id, v.title AS video_title, COALESCE(c.title, v.author) AS channel_title,
               vp.source, vp.first_seen
        FROM video_provenance vp
        JOIN videos v ON v.id = vp.video_id
        LEFT JOIN channels c ON c.id = v.channel_id
        WHERE vp.user_key = %(user)s
        ORDER BY vp.first_seen DESC, v.youtube_video_id
    """,
}

# Watch later is a playlist by that name (main._resolve_watch_list); it gets its own section.
WATCH_LATER_NAME = "watch later"

Runner = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


def _run_with_db(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def _iso(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _number(value: Any) -> float | None:
    try:
        return None if value is None else round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _fraction(value: Any) -> float:
    """playback_progress keeps a 0-1 share; older rows used percentages, so both are read."""
    share = _number(value) or 0.0
    return round(min(1.0, share / 100 if share > 1 else share), 4)


def _video(row: dict[str, Any]) -> dict[str, Any]:
    video_id = str(row.get("youtube_video_id") or "")
    return {
        "youtube_video_id": video_id,
        "title": row.get("video_title") or "",
        "channel": row.get("channel_title") or "",
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
    }


def _subscriptions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row.get("id") or ""),
            "title": row.get("title") or "",
            "youtube_channel_id": row.get("youtube_channel_id") or "",
            "feed_url": row.get("feed_url") or "",
            "channel_url": row.get("html_url") or "",
            "subscribed_at": _iso(row.get("subscribed_at")),
        }
        for row in rows
    ]


def _grouped(rows: list[dict[str, Any]], head: Callable[[dict[str, Any]], dict[str, Any]],
             member_key: str, member: Callable[[dict[str, Any]], dict[str, Any]], present: str) -> list[dict[str, Any]]:
    """Fold a LEFT JOIN of parent and child rows back into parents with child lists."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("id") or "")
        if key not in groups:
            groups[key] = {**head(row), member_key: []}
        if row.get(present):
            groups[key][member_key].append(member(row))
    return list(groups.values())


def _collections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _grouped(
        rows,
        lambda row: {"name": row.get("name") or "", "description": row.get("description") or "",
                     "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))},
        "subscriptions",
        lambda row: {"subscription_id": str(row.get("subscription_id") or ""), "title": row.get("subscription_title") or "",
                     "youtube_channel_id": row.get("youtube_channel_id") or "", "added_at": _iso(row.get("added_at"))},
        "subscription_id",
    )


def _playlists(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _grouped(
        rows,
        lambda row: {"id": str(row.get("id") or ""), "name": row.get("name") or "", "description": row.get("description") or "",
                     "visibility": row.get("visibility") or "private",
                     "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))},
        "items",
        lambda row: {**_video(row), "position": row.get("position"), "note": row.get("note") or "",
                     "added_at": _iso(row.get("added_at"))},
        "youtube_video_id",
    )


def _history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **_video(row),
            "first_opened_at": _iso(row.get("first_opened_at")),
            "last_watched_at": _iso(row.get("last_watched_at")),
            "resume_seconds": _number(row.get("resume_seconds")),
            "furthest_seconds": _number(row.get("furthest_seconds")),
            "duration_seconds": _number(row.get("duration_seconds")),
            "fraction_watched": _fraction(row.get("percent_completed")),
            "total_watch_seconds": _number(row.get("total_watch_seconds")),
            "completed": bool(row.get("completed")),
            "marked_watched": bool(row.get("manually_marked_watched")),
            "times_opened": int(row.get("open_count") or 0),
        }
        for row in rows
    ]


def _settings(rows: list[dict[str, Any]], notifications: list[dict[str, Any]]) -> dict[str, Any]:
    row = rows[0] if rows else {}
    profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
    topics = row.get("topics") if isinstance(row.get("topics"), list) else []
    feed = {key: value for key, value in profile.items() if key != "playback"}
    return {
        "playback": db.normalize_playback_preferences(profile.get("playback")),
        "feed": feed,
        "topics": [{"topic": item.get("topic"), "preference": item.get("preference")} for item in topics if isinstance(item, dict)],
        "channel_notifications": [
            {"youtube_channel_id": item.get("youtube_channel_id") or "", "level": item.get("level") or "all",
             "updated_at": _iso(item.get("updated_at"))}
            for item in notifications
        ],
    }


def build_export(user_key: str, *, profile_name: str = "", run: Runner | None = None,
                 now: dt.datetime | None = None) -> dict[str, Any]:
    """The whole export for one user_key. `run` executes one query; tests pass a fake."""
    run = run or _run_with_db
    params = {"user": user_key}
    raw = {name: run(sql, params) for name, sql in _QUERIES.items()}

    playlists = _playlists(raw["playlists"])
    watch_later = next((item for item in playlists if item["name"].strip().lower() == WATCH_LATER_NAME), None)
    if watch_later:
        playlists = [item for item in playlists if item is not watch_later]
    settings = _settings(raw["settings"], raw["notifications"])
    feed = settings["feed"]
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "app_version": __version__,
        "exported_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
        "profile": {"user_key": user_key, "name": profile_name},
        "notes": [
            "Subscriptions are this profile's own: other profiles and accounts keep their own lists.",
            "Liked videos and feedback are what this OpenFeed recorded. Nothing here was sent to YouTube.",
            "fraction_watched runs from 0 to 1; resume_seconds is where playback would pick up.",
            "catalog_reasons lists the shared videos this profile may be offered and why (a search showed it, "
            "you opened it, a channel page or playlist listed it). Other accounts never see this list.",
        ],
        "subscriptions": {
            "channels": _subscriptions(raw["subscriptions"]),
            "collections": _collections(raw["collections"]),
        },
        "playlists": playlists,
        "watch_later": watch_later["items"] if watch_later else [],
        "liked_videos": [{**_video(row), "liked_at": _iso(row.get("liked_at"))} for row in raw["liked_videos"]],
        "watch_history": _history(raw["watch_history"]),
        "feedback": {
            "videos": [
                {**_video(row), "disliked": row.get("reaction") == "dislike", "not_interested": bool(row.get("not_interested")),
                 "reason": row.get("not_interested_reason") or "", "updated_at": _iso(row.get("updated_at"))}
                for row in raw["video_feedback"]
            ],
            "more_or_less_like_this": [
                {**_video(row), "request": "more" if row.get("event_type") == "more_like_this" else "less",
                 "at": _iso(row.get("created_at"))}
                for row in raw["feedback_requests"]
            ],
            "muted_channels": list(feed.get("muted_channels") or []),
            "hidden_shelves": list(feed.get("hidden_shelves") or []),
        },
        "settings": settings,
        "catalog_reasons": [
            {**_video(row), "reason": row.get("source") or "", "since": _iso(row.get("first_seen"))}
            for row in raw.get("catalog_reasons") or []
        ],
    }


def export_filename(now: dt.datetime | None = None, suffix: str = "json") -> str:
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    return f"openfeed-export-{stamp}.{suffix}"


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


def _attr(value: str) -> str:
    return '"' + escape(value, {'"': "&quot;"}) + '"'


def subscriptions_opml(channels: list[dict[str, Any]], now: dt.datetime | None = None) -> str:
    """OPML 2.0 with one rss outline per subscription that has a feed address."""
    created = (now or dt.datetime.now(dt.timezone.utc)).strftime("%a, %d %b %Y %H:%M:%S %z")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        "    <title>OpenFeed subscriptions</title>",
        f"    <dateCreated>{escape(created)}</dateCreated>",
        "  </head>",
        "  <body>",
        '    <outline text="YouTube" title="YouTube">',
    ]
    for channel in channels:
        feed = str(channel.get("feed_url") or "").strip()
        if not feed:
            continue
        title = str(channel.get("title") or channel.get("youtube_channel_id") or feed)
        attrs = f'type="rss" text={_attr(title)} title={_attr(title)} xmlUrl={_attr(feed)}'
        link = str(channel.get("channel_url") or "").strip()
        if link:
            attrs += f" htmlUrl={_attr(link)}"
        lines.append(f"      <outline {attrs}/>")
    lines += ["    </outline>", "  </body>", "</opml>", ""]
    return "\n".join(lines)


def subscription_channels(user_key: str) -> list[dict[str, Any]]:
    """Just this user's subscription list, for the OPML download."""
    return _subscriptions(_run_with_db(_QUERIES["subscriptions"], {"user": user_key}))
