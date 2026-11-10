"""Startup migrations that `CREATE TABLE IF NOT EXISTS` cannot express.

`db.init_db` replays schema.sql on every start, which covers new tables and columns. Two
changes need more than that, and live here:

* **Table renames.** Feeds and their entries used to be stored under the names of the
  feed reader OpenFeed once mirrored (FreshRSS). They are now `feeds` and `feed_entries`.
  The rename has to run *before* schema.sql, or the replay would create empty tables under
  the new names next to the old ones.
* **Per-user subscriptions.** `user_subscriptions` says who follows which feed. When the
  table is first created on an install that already had feeds, every person who has data
  there is given the whole existing list, so nobody's feed empties on upgrade.

Everything runs inside init_db's one transaction, under an advisory lock so the web app and
the worker starting together cannot both migrate. Each step checks the catalog first, so a
second start is a no-op. Names are resolved through the search_path (no `public.` prefix),
which is what lets the opt-in database tests run this in a throwaway schema.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("openfeed.migrations")

# pg_advisory_xact_lock key: any constant; released when init_db's transaction ends.
SCHEMA_LOCK_KEY = 7_311_420_001

# (old, new). The only place the old names may appear.
TABLE_RENAMES = (
    ("freshrss_subscriptions", "feeds"),
    ("freshrss_entries", "feed_entries"),
)
# (table after rename, old constraint, new constraint). Renaming a primary key renames its
# index too, so these keep `\d feeds` readable rather than naming a product that is gone.
CONSTRAINT_RENAMES = (
    ("feeds", "freshrss_subscriptions_pkey", "feeds_pkey"),
    ("feed_entries", "freshrss_entries_pkey", "feed_entries_pkey"),
    ("feed_entries", "freshrss_entries_subscription_id_fkey", "feed_entries_subscription_id_fkey"),
    ("feed_entries", "freshrss_entries_video_id_fkey", "feed_entries_video_id_fkey"),
)
INDEX_RENAMES = (
    ("idx_freshrss_entries_published_at", "idx_feed_entries_published_at"),
    ("idx_freshrss_entries_video_id", "idx_feed_entries_video_id"),
    ("idx_freshrss_entries_video_updated", "idx_feed_entries_video_updated"),
)

# Tables whose user_key column proves that a person has data on this install. A key found
# in any of them, in `users` or in `account_profiles` receives the pre-existing feeds.
_USER_DATA_TABLES = (
    "user_video_events",
    "user_video_feedback",
    "playback_progress",
    "playlists",
    "subscription_collections",
    "channel_notifications",
    "takeout_subscriptions",
    "api_tokens",
    "user_history_settings",
)


def _exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (name,))
    row = cur.fetchone() or {}
    return bool(row.get("present"))


def _constraint_exists(cur: Any, table: str, name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = to_regclass(%s) AND conname = %s) AS present",
        (table, name),
    )
    row = cur.fetchone() or {}
    return bool(row.get("present"))


def _has_column(cur: Any, table: str, column: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s) AS present",
        (table, column),
    )
    row = cur.fetchone() or {}
    return bool(row.get("present"))


def _drop_shared_read_state(cur: Any) -> None:
    """Drop feed_entries.is_read and is_starred, the old feed reader's install-wide state.

    One flag per feed entry meant one person's "read" was everyone's. Nothing sets either
    any more (stars became each user's likes above, and read state was only a tie-break
    for the catch-up shelf), so they go rather than becoming per-user tables. Idempotent.
    """
    cur.execute("ALTER TABLE IF EXISTS feed_entries DROP COLUMN IF EXISTS is_read, DROP COLUMN IF EXISTS is_starred")


def _quote(name: str) -> str:
    # Every name passed here is a constant from this module; quoting is belt and braces.
    return '"' + name.replace('"', '""') + '"'


def before_schema(cur: Any) -> dict[str, Any]:
    """Run before schema.sql. Returns what after_schema needs to know."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
    state: dict[str, Any] = {"renamed": [], "seed_subscriptions": not _exists(cur, "user_subscriptions")}
    for old, new in TABLE_RENAMES:
        old_present, new_present = _exists(cur, old), _exists(cur, new)
        if old_present and not new_present:
            cur.execute(f"ALTER TABLE {_quote(old)} RENAME TO {_quote(new)}")
            state["renamed"].append(new)
        elif old_present and new_present:
            # Never merge or drop on a guess: both exist only if someone created the new
            # table by hand. The new one is used and the old one is left for a person.
            logger.warning("both %s and %s exist; leaving %s untouched", old, new, old)
    for table, old, new in CONSTRAINT_RENAMES:
        if _exists(cur, table) and _constraint_exists(cur, table, old) and not _constraint_exists(cur, table, new):
            cur.execute(f"ALTER TABLE {_quote(table)} RENAME CONSTRAINT {_quote(old)} TO {_quote(new)}")
    for old, new in INDEX_RENAMES:
        if _exists(cur, old) and not _exists(cur, new):
            cur.execute(f"ALTER INDEX {_quote(old)} RENAME TO {_quote(new)}")
    if state["renamed"]:
        logger.info("renamed tables: %s", ", ".join(state["renamed"]))
    return state


def existing_user_keys(cur: Any) -> list[str]:
    """Every user_key that has data here: accounts, profiles and owners of per-user rows.

    With none at all (feeds but nobody has used the app yet), the single-user default
    `local` is returned, so the feeds still belong to somebody.
    """
    parts = ["SELECT authentik_sub AS user_key FROM users", "SELECT user_key FROM account_profiles"]
    parts += [f"SELECT DISTINCT user_key FROM {table}" for table in _USER_DATA_TABLES if _exists(cur, table)]
    cur.execute(
        "SELECT DISTINCT user_key FROM (" + " UNION ".join(parts) + ") keys "
        "WHERE user_key IS NOT NULL AND btrim(user_key) <> '' ORDER BY user_key"
    )
    keys = [str(row["user_key"]) for row in cur.fetchall()]
    return keys or ["local"]


def after_schema(cur: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Run after schema.sql, in the same transaction."""
    result = {"renamed": list(state.get("renamed") or []), "seeded_users": 0, "seeded_rows": 0, "stars_moved": 0}
    if not state.get("seed_subscriptions"):
        _drop_shared_read_state(cur)
        return result
    keys = existing_user_keys(cur)
    cur.execute(
        """
        INSERT INTO user_subscriptions (user_key, feed_id, created_at)
        SELECT k.user_key, f.id, f.first_seen_at
        FROM feeds f CROSS JOIN unnest(%s::text[]) AS k(user_key)
        ON CONFLICT (user_key, feed_id) DO NOTHING
        """,
        (keys,),
    )
    result["seeded_rows"] = int(cur.rowcount or 0)
    result["seeded_users"] = len(keys) if result["seeded_rows"] else 0
    # The old feed reader's stars were install-wide. They become likes of everyone who
    # could see them, and the shared flag is cleared, so a person who joins later does not
    # find somebody else's stars in their Liked videos.
    if not _has_column(cur, "feed_entries", "is_starred"):
        # A fresh install: schema.sql no longer has the old reader's columns.
        _drop_shared_read_state(cur)
        return result
    cur.execute(
        """
        INSERT INTO user_video_feedback (user_key, video_id, youtube_video_id, reaction, last_event_type)
        SELECT k.user_key, v.id, v.youtube_video_id, 'like', 'like'
        FROM (SELECT DISTINCT video_id FROM feed_entries WHERE is_starred AND video_id IS NOT NULL) s
        JOIN videos v ON v.id = s.video_id
        CROSS JOIN unnest(%s::text[]) AS k(user_key)
        ON CONFLICT (user_key, video_id) DO NOTHING
        """,
        (keys,),
    )
    result["stars_moved"] = int(cur.rowcount or 0)
    cur.execute("UPDATE feed_entries SET is_starred = false WHERE is_starred")
    _drop_shared_read_state(cur)
    if result["seeded_rows"] or result["stars_moved"]:
        logger.info(
            "per-user subscriptions: gave %s feed rows to %s user keys; %s legacy stars became likes",
            result["seeded_rows"], len(keys), result["stars_moved"],
        )
    return result
