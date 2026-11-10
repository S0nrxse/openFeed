"""Per-user subscriptions: every "is this subscribed?" names whose subscriptions it means.

Feeds are shared and fetched once; who follows what is user_subscriptions, per user_key
(so per household profile). These DB-free tests capture the SQL each reader runs for a
user called "alice" and check that it is scoped to her: the query reads
user_subscriptions, her key is bound as a parameter, and the number of placeholders
matches the parameters (a fragment added without its parameter would shift every value
after it). The end-to-end check with two users against PostgreSQL is in
test_feeds_migration.MigrationDatabaseTests.
"""

from __future__ import annotations

import inspect
import unittest
from typing import Any
from unittest.mock import patch

from openfeed.tests.sqlcapture import _capture_all_sql, _capture_sql, placeholder_count
from openfeed import channel_tools, dataexport, db, evaluation, main, profiles, ranker

ALICE = "alice"


def _ranking_patches() -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(db, "get_feed_preferences", return_value={}))
    stack.enter_context(patch.object(db, "watch_time_profile", return_value={"channels": {}, "topics": {}, "completions": {}, "confidence": 0.0}))
    stack.enter_context(patch.object(db, "takeout_profile", return_value={"channels": {}, "confidence": 0.0, "watched_videos": []}))
    stack.enter_context(patch("openfeed.topics.profile_vector", return_value={}))
    stack.enter_context(patch.object(ranker, "recommended_rows", return_value=None))
    stack.enter_context(patch.object(ranker, "note_fallback"))
    return stack


# Readers whose answer depends on the user's subscriptions.
PER_USER_READERS = {
    "list_subscriptions": lambda: db.list_subscriptions(50, user_key=ALICE),
    "list_subscriptions recent": lambda: db.list_subscriptions(8, recent_first=True, user_key=ALICE),
    "list_playlist_subscriptions": lambda: db.list_playlist_subscriptions(10, user_key=ALICE),
    "list_subscription_uploads": lambda: db.list_subscription_uploads(10, ALICE, False),
    "list_subscription_recommendations": lambda: db.list_subscription_recommendations(10, ALICE),
    "classic ranker": lambda: db._list_recommended_videos_classic(10, 0, "all", ALICE),
    "classic recommended lane": lambda: db._list_recommended_videos_classic(10, 0, "recommended", ALICE),
    "interest profile": lambda: db._subscription_interest_profile(ALICE),
    "get_video_by_youtube_id": lambda: db.get_video_by_youtube_id("vid", ALICE),
    "get_channel_by_youtube_id": lambda: db.get_channel_by_youtube_id("UCx", ALICE),
    "search_channels": lambda: db.search_channels("chess", 5, user_key=ALICE),
    "store_search_channels": lambda: db.store_search_channels([{"youtube_channel_id": "UCx", "title": "X"}], user_key=ALICE),
    "pending_takeout_subscriptions": lambda: db.pending_takeout_subscriptions(ALICE),
    "takeout_subscription_counts": lambda: db.takeout_subscription_counts(ALICE),
    "is_subscribed": lambda: db.is_subscribed("UCx", user_key=ALICE),
    "list_collection_subscriptions": lambda: db.list_collection_subscriptions("c", ALICE),
    "list_collection_videos": lambda: db.list_collection_videos("c", 10, ALICE),
    "add_subscription_to_collection": lambda: db.add_subscription_to_collection("c", "feed/1", ALICE),
    "list_subscription_collections": lambda: db.list_subscription_collections(ALICE),
    "subscription_stats": lambda: channel_tools.subscription_stats(ALICE),
    "subscriptions_by_ids": lambda: channel_tools.subscriptions_by_ids(["feed/1"], ALICE),
    "list_feed_uploads": lambda: channel_tools.list_feed_uploads(10, ALICE),
    "remove_subscription": lambda: db.remove_subscription("https://feed", "UCx", user_key=ALICE),
}


class ScopedQueryTests(unittest.TestCase):
    def test_every_reader_binds_the_user_and_reads_their_subscriptions(self) -> None:
        for name, call in PER_USER_READERS.items():
            with self.subTest(reader=name), _ranking_patches():
                calls = _capture_all_sql(call)
                scoped = [(query, params) for query, params in calls if "user_subscriptions" in query]
                self.assertTrue(scoped, "expected a query that reads user_subscriptions")
                for query, params in scoped:
                    self.assertIn(ALICE, list(params or ()))
                    self.assertNotIn(ALICE, query)

    def test_placeholders_match_parameters(self) -> None:
        for name, call in PER_USER_READERS.items():
            with self.subTest(reader=name), _ranking_patches():
                for query, params in _capture_all_sql(call):
                    if isinstance(params, (tuple, list)):
                        self.assertEqual(placeholder_count(query), len(params), query[:200])

    def test_feeds_are_never_read_as_somebodys_subscriptions_without_a_user(self) -> None:
        # Any query in a per-user reader that touches feeds must also say whose follows.
        for name, call in PER_USER_READERS.items():
            with self.subTest(reader=name), _ranking_patches():
                for query, _ in _capture_all_sql(call):
                    if " feeds " in query or "FROM feeds" in query or "JOIN feeds" in query:
                        self.assertIn("user_subscriptions", query)

    def test_no_branch_shows_everyone_everything_when_a_user_follows_nothing(self) -> None:
        # The old check fell back to "subscribed" for every channel on an install with no
        # subscriptions at all, which would hand a new account somebody else's feed.
        self.assertNotIn("NOT EXISTS", db.ACTIVE_SUBSCRIPTION_SQL)
        self.assertEqual(db.subscribed_channel_sql("%(u)s").count("%(u)s"), 1)
        self.assertIn("us.user_key = %s", db.user_feeds_sql())

    def test_ranker_reads_and_caches_per_user(self) -> None:
        self.assertIn("user_subscriptions", ranker._FINGERPRINT_SQL)
        self.assertIn("us.user_key = %(u)s", ranker._FINGERPRINT_SQL)
        self.assertIn("user_subscriptions", ranker._row_sql())
        self.assertNotIn("%s", ranker._row_sql().replace("%%", ""))  # named parameters only
        source = inspect.getsource(ranker.load_signals)
        self.assertIn("JOIN user_subscriptions us", source)
        self.assertIn("(user_key,)", source)
        # The cache is keyed by user and its fingerprint includes that user's subscriptions.
        self.assertIn("_USER_CACHE.get(user_key)", inspect.getsource(ranker._cached_entry))

    def test_evaluation_seeds_from_one_users_subscriptions(self) -> None:
        captured: list[Any] = []

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, query, params=None):
                captured.append((query, params))

                class Result:
                    def fetchall(self):
                        return []
                return Result()

        with patch.object(db, "connect", return_value=Conn()):
            evaluation._subscribed_keys(ALICE)
        self.assertIn("user_subscriptions", captured[0][0])
        self.assertEqual(captured[0][1], (ALICE,))


class SharedFeedTests(unittest.TestCase):
    """Fetching is shared: one row per feed, polled while anybody follows it."""

    def test_the_poller_only_fetches_followed_feeds(self) -> None:
        query, params = _capture_sql(lambda: db.list_rss_due_channels(10))
        self.assertIn(db.FOLLOWED_FEED_SQL, query)
        self.assertEqual(params, (10,))

    def test_background_work_covers_feeds_somebody_follows(self) -> None:
        for call in (db.count_subscription_channels_missing_media, lambda: db.list_subscription_channels_needing_media(5),
                     lambda: db.list_channels_needing_media(5), lambda: db.list_followed_feeds(5)):
            query, _ = _capture_sql(call)
            self.assertIn(db.FOLLOWED_FEED_SQL, query)

    def test_subscribe_upserts_the_shared_feed_then_the_users_follow(self) -> None:
        calls = _capture_all_sql(lambda: db.subscribe(ALICE, {"id": "feed/u", "title": "T", "url": "u"}, "UCx"))
        statements = [query.strip().split("\n")[0] for query, _ in calls]
        self.assertTrue(statements[0].startswith("INSERT INTO feeds"))
        follow = next((query, params) for query, params in calls if "INSERT INTO user_subscriptions" in query)
        self.assertEqual(follow[1], (ALICE, "feed/u"))
        self.assertIn("ON CONFLICT (user_key, feed_id) DO NOTHING", follow[0])

    def test_a_subscribers_title_never_renames_a_shared_feed_or_channel(self) -> None:
        calls = _capture_all_sql(lambda: db.subscribe("mallory", {"id": "feed/u", "title": "Renamed", "url": "u"}, "UCx"))
        upsert = calls[0][0]
        self.assertIn("THEN EXCLUDED.title ELSE feeds.title END", upsert)
        self.assertNotIn("title = EXCLUDED.title", upsert)
        query, _ = _capture_sql(lambda: db.upsert_channel("Renamed", "UCx", "u", keep_title=True))
        self.assertIn("COALESCE(NULLIF(channels.title, ''), EXCLUDED.title)", query)
        # The poller, which reads the feed itself, still names things.
        query, _ = _capture_sql(lambda: db.upsert_channel("Real name", "UCx", "u"))
        self.assertIn("COALESCE(NULLIF(EXCLUDED.title, ''), channels.title)", query)
        self.assertIn("keep_title=True", inspect.getsource(main._subscribe_channel_local))
        self.assertIn("keep_title=True", inspect.getsource(main.api_import_opml))

    def test_the_poller_names_the_feed_after_its_channel(self) -> None:
        from openfeed import rss

        self.assertIn("db.refresh_feed_title(sub_id, feed_title)", inspect.getsource(rss.sync_rss_once))
        query, params = _capture_sql(lambda: db.refresh_feed_title("feed/1", "  Real   name "))
        self.assertIn("UPDATE feeds SET title = %s", query)
        self.assertEqual(params, ("Real name", "feed/1", "Real name"))

    def test_unsubscribing_never_deletes_the_feed(self) -> None:
        calls = _capture_all_sql(lambda: db.unsubscribe_feeds(ALICE, ["feed/1"]))
        self.assertFalse([query for query, _ in calls if "DELETE FROM feeds" in query])
        delete = next(params for query, params in calls if "DELETE FROM user_subscriptions" in query)
        self.assertEqual(delete, (ALICE, ["feed/1"]))

    def test_deduping_a_feed_moves_its_followers_before_deleting_the_loser(self) -> None:
        source = inspect.getsource(db._dedupe_feed)
        self.assertLess(source.index("INSERT INTO user_subscriptions"), source.index("DELETE FROM feeds"))

    def test_deleting_a_profile_deletes_its_subscriptions(self) -> None:
        self.assertIn("user_subscriptions", profiles.USER_KEY_TABLES)

    def test_the_export_and_opml_are_the_callers_own(self) -> None:
        self.assertIn("us.user_key = %(user)s", dataexport._QUERIES["subscriptions"])


class RouteScopingTests(unittest.TestCase):
    """The pages and endpoints hand the viewer's key to the readers above."""

    def test_the_subscription_list_endpoint_is_the_callers(self) -> None:
        with patch.object(main, "_user_key", return_value="bob"), \
             patch.object(db, "list_subscriptions", return_value=[]) as listed:
            main.api_subscriptions(None, 20)
        listed.assert_called_once_with(20, user_key="bob")

    def test_the_channel_endpoint_reports_the_callers_subscription(self) -> None:
        with patch.object(main, "_user_key", return_value="bob"), \
             patch.object(db, "get_channel_by_youtube_id", return_value={"youtube_channel_id": "UCx"}) as lookup:
            main.api_channel(None, "UCx")
        lookup.assert_called_once_with("UCx", "bob")

    def test_the_sidebar_shell_and_pages_pass_the_viewer(self) -> None:
        for function in (main._app_shell, main.index):
            self.assertIn("_app_sidebar(", inspect.getsource(function))
        self.assertIn("_app_sidebar(active, user_key)", inspect.getsource(main._app_shell))
        self.assertIn('_app_sidebar("home", user_key)', inspect.getsource(main.index))
        for function, needle in (
            (main.watch_video, "get_video_by_youtube_id(youtube_video_id, user_key)"),
            (main.channel_page, "get_channel_by_youtube_id(youtube_channel_id, channel_viewer)"),
            (main.channel_posts_page, "get_channel_by_youtube_id(youtube_channel_id, user_key)"),
            (main.api_get_video, "get_video_by_youtube_id(youtube_video_id, user_key)"),
            (main.api_export_opml, "subscription_channels(_user_key(request))"),
            (main._subscription_posts_shelf, "list_subscriptions(200, user_key=user_key)"),
            (main._setup_checklist, "list_subscriptions(250, user_key=user_key)"),
        ):
            with self.subTest(function=function.__name__):
                self.assertIn(needle, inspect.getsource(function))

    def test_takeout_subscribes_the_importing_user(self) -> None:
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="kid"), \
             patch.object(db, "pending_takeout_subscriptions", return_value=[{"youtube_channel_id": "UC" + "d" * 22, "title": "D"}]), \
             patch.object(db, "subscribe", return_value={"feed_id": "feed/d", "created": True}) as subscribed, \
             patch.object(db, "upsert_channel"), \
             patch.object(db, "mark_takeout_subscribed"), \
             patch.object(db, "takeout_subscription_counts", return_value={"pending": 0}), \
             patch.object(main, "_fetch_single_feed_best_effort", return_value=200), \
             patch.object(main, "enrich_channel"), patch.object(main, "enrich_channels_paced"):
            main.api_apply_takeout_subscriptions(None, 5)
        self.assertEqual(subscribed.call_args.args[0], "kid")


if __name__ == "__main__":
    unittest.main()
