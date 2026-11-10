"""Which part of the shared catalog one person may be shown.

Videos, channels and related-video links are one catalog for the whole install, a cache of
public YouTube data like Invidious' own, and those rows never record who caused them. But
the catalog grows from what people do: a search stores its results, opening a playlist or a
channel stores its videos, a Takeout import seeds the channels it names. Ranked into somebody
else's Home or library search, those videos would tell them what another account looked for.

So every per-person surface that reads the catalog (the ranker, Home shelves, Shorts, the
watch page's Up next, library search) keeps to what that person has a reason to see:

* videos from the feeds and channels they follow;
* their **provenance** rows (`video_provenance`, one per person and video): search results
  shown to them, videos they watched, opened, rated, saved or downloaded (written by
  database triggers, whatever code path wrote the history row), remote playlists they
  opened, channel pages they opened, their Takeout, and discovery runs seeded from their own
  history;
* videos discovered as related to either (one link: `video_discoveries` in SQL, and the
  related lists stored on those videos in the ranker);
* a **claim** (`catalog_claims`): the catalog as it stood when the install first hosted a
  second person, which belongs to the one person who had built it.

The SQL side is one function, `openfeed_video_scope(user_key)` in schema.sql, wrapped by
`video_filter_sql`. The ranker reads the same sets once per user and builds a mask
(ranker.scope_mask). Shared background work (enrichment, related discovery, the RSS poller)
still reads the whole catalog; what it finds reaches a person only through this scope.

Single person. With sign-in off and no household profiles there is nobody to keep apart, so
`applies()` is False for `local` and every surface reads the whole catalog exactly as before:
background discovery keeps reaching Home with no provenance to maintain. The first time the
install hosts a second person (a profile is added, or sign-in is turned on), `mark_shared`
gives `local` a claim on everything in the catalog at that moment, and scoping applies to
everyone from then on, for good. Why a rule rather than only a one-time backfill: a backfill
alone would freeze the single user's catalog at upgrade time, and videos that later shared
discovery runs crawl two links away would stop reaching the only person on the install, which
is not "keep today's experience".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Iterable

from . import db

logger = logging.getLogger("openfeed.catalog_scope")

# Why a person has a video. Stored in video_provenance.source (the first reason only).
SOURCES = (
    "search",     # a search result shown to them
    "watch",      # playback progress (written by a trigger)
    "event",      # opened, marked watched, hidden, more/less like this (trigger)
    "feedback",   # liked, disliked, not interested (trigger)
    "playlist",   # saved to one of their playlists (trigger), or a remote playlist they opened
    "download",   # downloaded (trigger)
    "channel",    # listed on a channel page they opened
    "discovery",  # stored by a discovery run seeded from their own history or started by them
    "takeout",    # in their imported watch history
    "catalog",    # the claim of the single-person catalog (catalog_claims), never a row here
)
SETTING_KEY = "catalog_scope"
LOCAL = "local"
CACHE_SECONDS = 10.0

_state_lock = threading.Lock()
_state: dict[str, Any] = {"at": 0.0, "shared": None}


# --- whether scoping applies -------------------------------------------------------------


def _read_shared() -> bool:
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (SETTING_KEY,)).fetchone()
    except Exception as exc:  # noqa: BLE001 - unknown state is treated as shared: never wider
        logger.info("catalog scope state unreadable: %s", exc)
        return True
    value = (row or {}).get("value") if row else None
    return bool(isinstance(value, dict) and value.get("shared"))


def install_shared() -> bool:
    """Whether this install has ever hosted more than one person. Cached for a few seconds."""
    from .config import get_settings

    if get_settings().sign_in_enabled:
        return True
    now = time.monotonic()
    with _state_lock:
        if _state["shared"] is not None and now - _state["at"] < CACHE_SECONDS:
            return bool(_state["shared"])
    shared = _read_shared()
    with _state_lock:
        _state.update(at=now, shared=shared)
    return shared


def applies(user_key: str | None) -> bool:
    """Whether `user_key`'s surfaces are scoped. False only for the one person of a
    single-person install (sign-in off, no household profile ever added)."""
    key = str(user_key or "").strip()
    if not key:
        return True
    if key != LOCAL:
        return True
    return install_shared()


def clear_cache() -> None:
    with _state_lock:
        _state.update(at=0.0, shared=None)


# --- the SQL side ------------------------------------------------------------------------

CLAIM_SQL = "(SELECT COALESCE(max(cc.through_video_id), 0) FROM catalog_claims cc WHERE cc.user_key = %s)"


def video_filter_sql(user_key: str | None, column: str = "v.id") -> tuple[str, tuple[Any, ...]]:
    """(" AND <predicate>", params) keeping `column` (a videos.id) inside the person's scope,
    or ("", ()) when scoping does not apply. Two placeholders, both the user_key; splice the
    params where the fragment goes. The claim is a scalar InitPlan and the scope function a
    hashed subplan, so each runs once per query, not per row."""
    if not applies(user_key):
        return "", ()
    key = str(user_key or "")
    return (
        f" AND ({column} <= {CLAIM_SQL} OR {column} IN (SELECT openfeed_video_scope(%s)))",
        (key, key),
    )


def scope_sets(cur: Any, user_key: str) -> tuple[int, list[int], list[int]]:
    """(claim through id, every scoped id above it, the person's own ids above it) for the
    ranker. "Own" is provenance plus subscribed feeds: the videos whose stored related lists
    the ranker may also follow one link (related-of-related is never in scope)."""
    cur.execute(f"SELECT {CLAIM_SQL} AS through", (user_key,))
    through = int((cur.fetchone() or {}).get("through") or 0)
    cur.execute("SELECT s.id FROM openfeed_video_scope(%s) AS s(id)", (user_key,))
    scoped = [int(row["id"]) for row in cur.fetchall()]
    cur.execute(
        """
        SELECT video_id AS id FROM video_provenance WHERE user_key = %s AND video_id > %s
        UNION
        SELECT fe.video_id FROM feed_entries fe JOIN user_subscriptions us ON us.feed_id = fe.subscription_id
        WHERE us.user_key = %s AND fe.video_id > %s
        """,
        (user_key, through, user_key, through),
    )
    own = [int(row["id"]) for row in cur.fetchall()]
    return through, scoped, own


FINGERPRINT_SQL = (
    "(SELECT concat_ws(':', COUNT(*), MAX(first_seen), "
    "(SELECT MAX(through_video_id) FROM catalog_claims WHERE user_key = %(u)s)) "
    "FROM video_provenance WHERE user_key = %(u)s)"
)


# --- recording provenance ----------------------------------------------------------------


def _clean_ids(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _person(user_key: str | None) -> str:
    """The key to record under, or "" for nobody: `local` while sign-in is on is the
    server's own background work (the worker runs discovery as `local`), not a person."""
    from .config import get_settings

    key = str(user_key or "").strip()
    if key == LOCAL and get_settings().sign_in_enabled:
        return ""
    return key


def record_youtube_ids(user_key: str | None, youtube_video_ids: Iterable[Any], source: str) -> int:
    """Note that `user_key` was shown these stored videos (by YouTube id) for `source`.

    Ids not in the catalog are skipped. Never raises: a missed row narrows what this person
    is offered later, which is the safe direction."""
    key = _person(user_key)
    ids = _clean_ids(youtube_video_ids)
    if not key or not ids or source not in SOURCES:
        return 0
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO video_provenance (user_key, video_id, source)
                    SELECT %s, v.id, %s FROM videos v WHERE v.youtube_video_id = ANY(%s)
                    ON CONFLICT (user_key, video_id) DO NOTHING
                    """,
                    (key, source, ids),
                )
                added = int(cur.rowcount or 0)
            conn.commit()
        return added
    except Exception as exc:  # noqa: BLE001
        logger.info("provenance not recorded (%s, %d ids): %s", source, len(ids), exc)
        return 0


def record_rows(user_key: str | None, rows: Iterable[dict[str, Any]], source: str) -> int:
    """record_youtube_ids for rows that carry `youtube_video_id`."""
    return record_youtube_ids(user_key, (row.get("youtube_video_id") for row in rows or () if isinstance(row, dict)), source)


def record_video_ids(user_key: str | None, video_ids: Iterable[Any], source: str) -> int:
    """The same by videos.id (what db.upsert_discovered_video returns)."""
    key = _person(user_key)
    ids = sorted({int(v) for v in video_ids or () if v is not None})
    if not key or not ids or source not in SOURCES:
        return 0
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO video_provenance (user_key, video_id, source)
                    SELECT %s, v.id, %s FROM videos v WHERE v.id = ANY(%s)
                    ON CONFLICT (user_key, video_id) DO NOTHING
                    """,
                    (key, source, ids),
                )
                added = int(cur.rowcount or 0)
            conn.commit()
        return added
    except Exception as exc:  # noqa: BLE001
        logger.info("provenance not recorded (%s, %d ids): %s", source, len(ids), exc)
        return 0


# --- the claim and the upgrade -----------------------------------------------------------


def claim_catalog(cur: Any, user_key: str) -> int:
    """Give `user_key` every video stored so far. Returns the through id (0 for an empty
    catalog). Raising a claim keeps the larger one."""
    cur.execute("SELECT COALESCE(max(id), 0) AS top FROM videos")
    top = int((cur.fetchone() or {}).get("top") or 0)
    if top <= 0:
        return 0
    cur.execute(
        """
        INSERT INTO catalog_claims (user_key, through_video_id) VALUES (%s, %s)
        ON CONFLICT (user_key) DO UPDATE
          SET through_video_id = GREATEST(catalog_claims.through_video_id, EXCLUDED.through_video_id), claimed_at = now()
        """,
        (user_key, top),
    )
    return top


def _only_local_data(cur: Any) -> bool:
    from . import migrations

    keys = migrations.existing_user_keys(cur)
    return all(key == LOCAL or key.startswith(f"{LOCAL}:p:") for key in keys)


def mark_shared(cur: Any, reason: str) -> dict[str, Any]:
    """The install now hosts more than one person. Inside the caller's transaction.

    The first time: if everything so far belongs to `local` (and its profiles), `local`
    claims the catalog as it stands, since that one person built it. Idempotent."""
    cur.execute("SELECT value FROM app_settings WHERE key = %s FOR UPDATE", (SETTING_KEY,))
    row = cur.fetchone()
    value = (row or {}).get("value") if row else None
    if isinstance(value, dict) and value.get("shared"):
        return {"changed": False}
    through = claim_catalog(cur, LOCAL) if _only_local_data(cur) else 0
    state = {"shared": True, "reason": reason, "claimed_by": LOCAL if through else "", "through_video_id": through}
    cur.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (SETTING_KEY, json.dumps(state)),
    )
    with _state_lock:
        _state.update(at=time.monotonic(), shared=True)
    logger.info("catalog scoping on (%s); local claimed through video %s", reason, through)
    return {"changed": True, **state}


BACKFILL_SQL = (
    ("watch", "SELECT user_key, video_id FROM playback_progress WHERE video_id IS NOT NULL"),
    ("event", "SELECT user_key, video_id FROM user_video_events WHERE video_id IS NOT NULL"),
    ("feedback", "SELECT user_key, video_id FROM user_video_feedback WHERE video_id IS NOT NULL"),
    ("playlist", "SELECT p.user_key, pi.video_id FROM playlist_items pi JOIN playlists p ON p.id = pi.playlist_id WHERE pi.video_id IS NOT NULL"),
    ("download", "SELECT user_key, video_id FROM downloads WHERE video_id IS NOT NULL"),
    ("takeout", "SELECT t.user_key, v.id FROM takeout_watched_videos t JOIN videos v ON v.youtube_video_id = t.youtube_video_id"),
)


def backfill(cur: Any) -> dict[str, int]:
    """One-time upgrade: every person's own history, progress, ratings, playlists, downloads
    and Takeout become provenance rows, so nothing they already had leaves their scope."""
    counts: dict[str, int] = {}
    for source, select in BACKFILL_SQL:
        cur.execute(
            f"INSERT INTO video_provenance (user_key, video_id, source) "
            f"SELECT DISTINCT s.user_key, s.video_id, %s FROM ({select}) AS s(user_key, video_id) "
            "WHERE s.user_key IS NOT NULL AND btrim(s.user_key) <> '' "
            "ON CONFLICT (user_key, video_id) DO NOTHING",
            (source,),
        )
        counts[source] = int(cur.rowcount or 0)
    return counts


def _has_other_profiles(cur: Any) -> bool:
    cur.execute("SELECT 1 FROM account_profiles WHERE account_key = %s AND NOT is_main LIMIT 1", (LOCAL,))
    return cur.fetchone() is not None


def at_startup(cur: Any, created: bool) -> dict[str, Any]:
    """db.init_db's last step, in its transaction. `created`: video_provenance did not exist
    before this start (the upgrade), so every person's own history is backfilled once."""
    from .config import get_settings

    result: dict[str, Any] = {}
    if created:
        result["backfilled"] = backfill(cur)
        logger.info("catalog provenance backfilled: %s", result["backfilled"])
    if get_settings().sign_in_enabled:
        result["shared"] = mark_shared(cur, "sign-in")
    elif _has_other_profiles(cur):
        result["shared"] = mark_shared(cur, "profiles")
    return result
