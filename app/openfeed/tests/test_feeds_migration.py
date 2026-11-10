"""The startup migration: old feed table names renamed, per-user subscriptions seeded.

Two layers:
- DB-free tests drive migrations.before_schema / after_schema with a fake cursor that
  answers the catalog questions, and check what SQL would run in each state.
- Opt-in PostgreSQL tests (OPENFEED_TEST_DATABASE_URL) build an install the way it looked
  before the rename in a throwaway schema, run db.init_db() twice, and check the data,
  indexes and constraints came through and that the second run changed nothing.

The old table names are read from migrations.TABLE_RENAMES rather than written out, so
the only place they appear in the tree is the migration itself.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from openfeed import catalog_scope, db, migrations

OLD_FEEDS, NEW_FEEDS = migrations.TABLE_RENAMES[0]
OLD_ENTRIES, NEW_ENTRIES = migrations.TABLE_RENAMES[1]


class _CatalogCursor:
    """Answers to_regclass / pg_constraint lookups from a set of names; records the rest."""

    def __init__(self, relations: set[str], constraints: set[tuple[str, str]] | None = None, user_keys: list[str] | None = None,
                 columns: set[tuple[str, str]] | None = None) -> None:
        self.relations = set(relations)
        self.columns = set(columns if columns is not None else {("feed_entries", "is_read"), ("feed_entries", "is_starred")})
        self.constraints = set(constraints or set())
        self.user_keys = user_keys or []
        self.calls: list[tuple[str, Any]] = []
        self._row: dict[str, Any] | None = None
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        self._rows, self._row, self.rowcount = [], None, 0
        if query.startswith("SELECT to_regclass"):
            self._row = {"present": params[0] in self.relations}
        elif "information_schema.columns" in query:
            self._row = {"present": (params[0], params[1]) in self.columns}
        elif "FROM pg_constraint" in query:
            self._row = {"present": (params[0], params[1]) in self.constraints}
        elif query.startswith("ALTER TABLE") and " RENAME TO " in query:
            old, new = query.split('"')[1], query.split('"')[3]
            self.relations.discard(old)
            self.relations.add(new)
        elif "SELECT DISTINCT user_key FROM (" in query:
            self._rows = [{"user_key": key} for key in self.user_keys]
        elif query.lstrip().startswith("INSERT INTO user_subscriptions"):
            self.rowcount = 3

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def statements(self) -> list[str]:
        return [query for query, _ in self.calls]


class MigrationLogicTests(unittest.TestCase):
    def test_the_lock_is_taken_first(self) -> None:
        cur = _CatalogCursor(set())
        migrations.before_schema(cur)
        self.assertIn("pg_advisory_xact_lock", cur.calls[0][0])

    def test_an_old_install_is_renamed_and_seeded(self) -> None:
        cur = _CatalogCursor(
            {OLD_FEEDS, OLD_ENTRIES, *(old for old, _ in migrations.INDEX_RENAMES)},
            {(table, old) for table, old, _ in migrations.CONSTRAINT_RENAMES},
        )
        state = migrations.before_schema(cur)
        self.assertEqual(state["renamed"], [NEW_FEEDS, NEW_ENTRIES])
        self.assertTrue(state["seed_subscriptions"])
        statements = cur.statements()
        self.assertIn(f'ALTER TABLE "{OLD_FEEDS}" RENAME TO "feeds"', statements)
        self.assertIn(f'ALTER TABLE "{OLD_ENTRIES}" RENAME TO "feed_entries"', statements)
        for table, old, new in migrations.CONSTRAINT_RENAMES:
            self.assertIn(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"', statements)
        for old, new in migrations.INDEX_RENAMES:
            self.assertIn(f'ALTER INDEX "{old}" RENAME TO "{new}"', statements)

    def test_a_migrated_install_is_left_alone(self) -> None:
        cur = _CatalogCursor({"feeds", "feed_entries", "user_subscriptions"})
        state = migrations.before_schema(cur)
        self.assertEqual(state, {"renamed": [], "seed_subscriptions": False})
        self.assertFalse([q for q in cur.statements() if q.startswith("ALTER")])
        result = migrations.after_schema(cur, state)
        self.assertEqual(result["seeded_rows"], 0)
        self.assertFalse([q for q in cur.statements() if "INSERT" in q or "UPDATE" in q])

    def test_both_names_present_renames_nothing(self) -> None:
        cur = _CatalogCursor({OLD_FEEDS, "feeds", OLD_ENTRIES, "feed_entries"})
        with self.assertLogs("openfeed.migrations", "WARNING"):
            state = migrations.before_schema(cur)
        self.assertEqual(state["renamed"], [])
        self.assertFalse([q for q in cur.statements() if q.startswith("ALTER TABLE") and "RENAME TO" in q])

    def test_a_fresh_install_gets_the_new_names_without_renames(self) -> None:
        cur = _CatalogCursor(set())
        state = migrations.before_schema(cur)
        self.assertEqual(state["renamed"], [])
        self.assertTrue(state["seed_subscriptions"])

    def test_seeding_gives_every_existing_user_key_the_feeds(self) -> None:
        cur = _CatalogCursor({"users", "account_profiles", *migrations._USER_DATA_TABLES}, user_keys=["alice", "local", "local:p:kid"])
        result = migrations.after_schema(cur, {"renamed": [], "seed_subscriptions": True})
        insert = next((q, p) for q, p in cur.calls if q.lstrip().startswith("INSERT INTO user_subscriptions"))
        self.assertEqual(insert[1], (["alice", "local", "local:p:kid"],))
        self.assertIn("ON CONFLICT (user_key, feed_id) DO NOTHING", insert[0])
        self.assertEqual(result["seeded_rows"], 3)
        # Legacy shared stars become each person's likes, then the shared flag is cleared.
        statements = cur.statements()
        self.assertTrue(any("INSERT INTO user_video_feedback" in q for q in statements))
        self.assertIn("UPDATE feed_entries SET is_starred = false WHERE is_starred", statements)
        # Then the old reader's install-wide read and starred flags are dropped.
        self.assertTrue(statements[-1].startswith("ALTER TABLE IF EXISTS feed_entries DROP COLUMN IF EXISTS is_read"))

    def test_a_fresh_install_has_no_shared_flags_to_move(self) -> None:
        cur = _CatalogCursor({"users", "account_profiles"}, user_keys=["local"], columns=set())
        result = migrations.after_schema(cur, {"renamed": [], "seed_subscriptions": True})
        self.assertEqual(result["stars_moved"], 0)
        self.assertFalse([q for q in cur.statements() if "is_starred AND" in q or "SET is_starred" in q])

    def test_a_migrated_install_drops_the_shared_read_state_once_more_harmlessly(self) -> None:
        cur = _CatalogCursor({"feeds", "feed_entries", "user_subscriptions"})
        migrations.after_schema(cur, {"renamed": [], "seed_subscriptions": False})
        self.assertEqual(cur.statements(), ["ALTER TABLE IF EXISTS feed_entries DROP COLUMN IF EXISTS is_read, DROP COLUMN IF EXISTS is_starred"])

    def test_the_key_query_reads_users_profiles_and_per_user_tables(self) -> None:
        cur = _CatalogCursor({"playback_progress", "playlists"})
        cur.user_keys = []
        self.assertEqual(migrations.existing_user_keys(cur), ["local"])
        query = next(q for q in cur.statements() if "SELECT DISTINCT user_key FROM (" in q)
        for part in ("FROM users", "FROM account_profiles", "FROM playback_progress", "FROM playlists"):
            self.assertIn(part, query)
        self.assertNotIn("FROM user_video_events", query)  # absent tables are skipped

    def test_init_db_runs_the_migration_around_the_schema_in_one_transaction(self) -> None:
        order: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, query, params=None):
                order.append("schema" if "CREATE TABLE IF NOT EXISTS feeds" in query else "other")

            def fetchone(self):
                return None

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def cursor(self):
                return Cursor()

            def commit(self):
                order.append("commit")

        with patch.object(db, "connect", return_value=Conn()), \
             patch.object(migrations, "before_schema", side_effect=lambda cur: order.append("before") or {"renamed": [], "seed_subscriptions": False}), \
             patch.object(migrations, "after_schema", side_effect=lambda cur, state: order.append("after") or {"renamed": []}), \
             patch.object(catalog_scope, "at_startup", side_effect=lambda cur, created: order.append("catalog") or {}):
            db.init_db()
        self.assertEqual([step for step in order if step != "other"], ["before", "schema", "after", "catalog", "commit"])

    def test_the_schema_uses_only_the_new_names(self) -> None:
        schema = db.SCHEMA_PATH.read_text(encoding="utf-8")
        for old, _ in migrations.TABLE_RENAMES:
            self.assertNotIn(old, schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS user_subscriptions", schema)
        self.assertIn("REFERENCES feeds(id) ON DELETE CASCADE", schema)


# The legacy data. Inserted after the schema has been put back to its pre-upgrade shape.
_LEGACY_DATA = """
INSERT INTO videos (id, youtube_video_id, title) VALUES (1, 'vid00000001', 'One'), (2, 'vid00000002', 'Two');
INSERT INTO {feeds} (id, title, feed_url, youtube_channel_id, first_seen_at) VALUES
  ('feed/4', 'Numbered', 'https://www.youtube.com/feeds/videos.xml?channel_id=UCaaaaaaaaaaaaaaaaaaaaaa', 'UCaaaaaaaaaaaaaaaaaaaaaa', '2025-01-01'),
  ('feed/https://example.com/rss', 'Plain feed', 'https://example.com/rss', NULL, '2025-02-01');
INSERT INTO {entries} (id, video_id, subscription_id, title, is_starred) VALUES
  ('e1', 1, 'feed/4', 'One', true), ('e2', 2, 'feed/https://example.com/rss', 'Two', false);
INSERT INTO users (authentik_sub) VALUES ('local'), ('oidc-sub-1');
INSERT INTO account_profiles (account_key, user_key, name, is_main) VALUES ('local', 'local', 'Main', true), ('local', 'local:p:kid', 'Kid', false);
INSERT INTO subscription_collections (id, user_key, name) VALUES ('00000000-0000-0000-0000-000000000001', 'local', 'Tech');
INSERT INTO subscription_collection_members (collection_id, subscription_id) VALUES ('00000000-0000-0000-0000-000000000001', 'feed/4');
"""


@unittest.skipUnless(os.environ.get("OPENFEED_TEST_DATABASE_URL"), "requires an isolated PostgreSQL test database")
class MigrationDatabaseTests(unittest.TestCase):
    """Opt-in; each test owns a throwaway schema on the explicitly supplied database."""

    def setUp(self) -> None:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row

        self.psycopg, self.sql, self.dict_row = psycopg, sql, dict_row
        self.dsn = os.environ["OPENFEED_TEST_DATABASE_URL"]
        self.schema = "feeds_mig_" + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.enterContext(patch.object(db, "connect", self.connect))

    @contextmanager
    def connect(self):
        with self.psycopg.connect(self.dsn, row_factory=self.dict_row) as conn:
            conn.execute(self.sql.SQL("SET search_path TO {}").format(self.sql.Identifier(self.schema)))
            yield conn

    def drop_schema(self) -> None:
        with self.psycopg.connect(self.dsn) as conn:
            conn.execute(self.sql.SQL("DROP SCHEMA {} CASCADE").format(self.sql.Identifier(self.schema)))

    def _fetch(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return list(conn.execute(query, params).fetchall())

    def _catalog(self) -> dict[str, Any]:
        rels = {row["relname"] for row in self._fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s", (self.schema,))}
        cons = {row["conname"] for row in self._fetch(
            "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s", (self.schema,))}
        return {"relations": rels, "constraints": cons}

    def _legacy(self) -> None:
        """An install from before the rename: today's schema with the old names put back,
        no per-user table, and some data."""
        db.init_db()
        with self.connect() as conn:
            conn.execute("DROP TABLE user_subscriptions")
            conn.execute("DROP INDEX idx_feeds_youtube_channel")
            for table, old, new in migrations.CONSTRAINT_RENAMES:
                conn.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{new}" TO "{old}"')
            for old, new in migrations.INDEX_RENAMES:
                conn.execute(f'ALTER INDEX "{new}" RENAME TO "{old}"')
            for old, new in migrations.TABLE_RENAMES:
                conn.execute(f'ALTER TABLE "{new}" RENAME TO "{old}"')
            conn.execute(_LEGACY_DATA.format(feeds=OLD_FEEDS, entries=OLD_ENTRIES))
        catalog = self._catalog()
        self.assertIn(OLD_FEEDS, catalog["relations"])
        self.assertNotIn("feeds", catalog["relations"])

    def test_an_old_install_upgrades_in_place_and_a_rerun_is_a_no_op(self) -> None:
        self._legacy()
        first = db.init_db()
        self.assertEqual(first["renamed"], ["feeds", "feed_entries"])
        catalog = self._catalog()
        for old, new in migrations.TABLE_RENAMES:
            self.assertNotIn(old, catalog["relations"])
            self.assertIn(new, catalog["relations"])
        for old, new in migrations.INDEX_RENAMES:
            self.assertNotIn(old, catalog["relations"])
            self.assertIn(new, catalog["relations"])
        for _, old, new in migrations.CONSTRAINT_RENAMES:
            self.assertNotIn(old, catalog["constraints"])
            self.assertIn(new, catalog["constraints"])
        self.assertFalse([name for name in catalog["relations"] | catalog["constraints"] if OLD_FEEDS in name or OLD_ENTRIES in name])
        # Data and references came through.
        self.assertEqual([row["id"] for row in self._fetch("SELECT id FROM feeds ORDER BY id")], ["feed/4", "feed/https://example.com/rss"])
        self.assertEqual(len(self._fetch("SELECT 1 FROM feed_entries")), 2)
        self.assertEqual(len(self._fetch("SELECT 1 FROM subscription_collection_members")), 1)
        # Every user_key with data got every feed, dated when the feed was first seen.
        subs = self._fetch("SELECT user_key, feed_id, created_at FROM user_subscriptions ORDER BY user_key, feed_id")
        self.assertEqual(sorted({row["user_key"] for row in subs}), ["local", "local:p:kid", "oidc-sub-1"])
        self.assertEqual(len(subs), 6)
        self.assertEqual(str(next(r for r in subs if r["feed_id"] == "feed/4")["created_at"].date()), "2025-01-01")
        # The legacy star became a like for each of them and the shared flag is gone.
        likes = self._fetch("SELECT user_key FROM user_video_feedback WHERE reaction = 'like' ORDER BY user_key")
        self.assertEqual([row["user_key"] for row in likes], ["local", "local:p:kid", "oidc-sub-1"])
        columns = {row["column_name"] for row in self._fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'feed_entries'")}
        self.assertFalse(columns & {"is_read", "is_starred"})
        # The foreign keys still hold: deleting a feed cascades its follows and members.
        before = self._catalog()
        snapshot = self._fetch("SELECT user_key, feed_id, created_at FROM user_subscriptions ORDER BY 1, 2")
        second = db.init_db()
        self.assertEqual(second, {"renamed": [], "seeded_users": 0, "seeded_rows": 0, "stars_moved": 0})
        self.assertEqual(self._catalog(), before)
        self.assertEqual(self._fetch("SELECT user_key, feed_id, created_at FROM user_subscriptions ORDER BY 1, 2"), snapshot)

    def test_a_fresh_install_starts_with_the_new_names_and_nobody_subscribed(self) -> None:
        result = db.init_db()
        self.assertEqual(result["renamed"], [])
        catalog = self._catalog()
        self.assertIn("feeds", catalog["relations"])
        self.assertIn("user_subscriptions", catalog["relations"])
        self.assertFalse([name for name in catalog["relations"] if OLD_FEEDS in name or OLD_ENTRIES in name])
        self.assertEqual(self._fetch("SELECT COUNT(*) AS n FROM user_subscriptions")[0]["n"], 0)

    def test_two_users_are_isolated_end_to_end(self) -> None:
        db.init_db()
        channel = "UC" + "c" * 22
        item = {"id": f"feed/https://www.youtube.com/feeds/videos.xml?channel_id={channel}", "title": "Shared",
                "url": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}", "categories": []}
        with self.connect() as conn:
            conn.execute("INSERT INTO channels (youtube_channel_id, title) VALUES (%s, 'Shared')", (channel,))
        first = db.subscribe("alice", item, channel)
        self.assertTrue(first["created"])
        self.assertFalse(db.subscribe("alice", item, channel)["created"])
        self.assertEqual([row["youtube_channel_id"] for row in db.list_subscriptions(user_key="alice")], [channel])
        self.assertEqual(db.list_subscriptions(user_key="bob"), [])
        self.assertTrue(db.is_subscribed(channel, user_key="alice"))
        self.assertFalse(db.is_subscribed(channel, user_key="bob"))
        self.assertIsNotNone(db.get_channel_by_youtube_id(channel, "alice")["subscription_id"])
        self.assertIsNone(db.get_channel_by_youtube_id(channel, "bob")["subscription_id"])
        self.assertIsNone(db.get_channel_by_youtube_id(channel)["subscription_id"])
        # Polled once while anybody follows it.
        self.assertEqual([row["id"] for row in db.list_rss_due_channels(10)], [first["feed_id"]])
        db.subscribe("bob", item, channel)
        self.assertEqual(len(db.list_rss_due_channels(10)), 1)
        self.assertEqual(self._fetch("SELECT COUNT(*) AS n FROM feeds")[0]["n"], 1)
        # A collection only takes the owner's own feeds.
        collection = db.create_subscription_collection("Mine", None, "carol")
        self.assertIsNone(db.add_subscription_to_collection(str(collection["id"]), first["feed_id"], "carol"))
        alice_collection = db.create_subscription_collection("Mine", None, "alice")
        self.assertIsNotNone(db.add_subscription_to_collection(str(alice_collection["id"]), first["feed_id"], "alice"))
        # Unsubscribing is per person, and takes the feed out of that person's collections.
        self.assertEqual(db.remove_subscription(None, channel, user_key="alice"), 1)
        self.assertEqual(db.list_collection_subscriptions(str(alice_collection["id"]), "alice"), [])
        self.assertEqual(len(db.list_subscriptions(user_key="bob")), 1)
        self.assertEqual(len(db.list_rss_due_channels(10)), 1)
        self.assertEqual(db.remove_subscription(None, channel, user_key="alice"), 0)
        # The last follower leaving stops polling but keeps the feed row.
        db.unsubscribe_feeds("bob", [first["feed_id"]])
        self.assertEqual(db.list_rss_due_channels(10), [])
        self.assertEqual(self._fetch("SELECT COUNT(*) AS n FROM feeds")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
