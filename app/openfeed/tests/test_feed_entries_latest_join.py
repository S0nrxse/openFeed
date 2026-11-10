"""Regression tests for the feed_entries fan-out fix.

A video syndicated in two feeds has two feed_entries rows, and every video
query used to `LEFT JOIN feed_entries fe ON fe.video_id = v.id`, which
multiplied one video into N rows and made LIMITs count duplicates. All such
queries must reuse the shared ``db._FEED_ENTRY_LATEST_JOIN`` fragment
(``DISTINCT ON (video_id) ... ORDER BY video_id, updated_at DESC``): one row per
video, newest row wins for subscription_id. Full-library ranking queries use
its materialized-CTE twin (``db._FEED_ENTRY_LATEST_CTE`` +
``db._FEED_ENTRY_LATEST_JOIN_CTE``), which dedupes identically but evaluates once
instead of once per video row.

DB/network-free: a fake cursor emulates Postgres join semantics against an
in-memory table -- a plain join fans out, the dedup fragment keeps one row per
video -- so the tests fail if any query regresses to the plain join, without
needing a database. Follows the fake-cursor style of test_db_queries.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Make `from openfeed import db` work however the suite is launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from openfeed.tests.sqlcapture import _capture_all_sql, _capture_sql, _FakeConnection
from openfeed import db


# Two videos; video 1 is syndicated in two feeds (two entries rows).
# The newer row names another feed so "newest wins" is observable.
_VIDEOS = (
    {"id": 1, "youtube_video_id": "vid-one", "title": "Video One"},
    {"id": 2, "youtube_video_id": "vid-two", "title": "Video Two"},
)
_ENTRIES = (
    {"video_id": 1, "subscription_id": "sub-a", "updated_at": 100},
    {"video_id": 1, "subscription_id": "sub-c", "updated_at": 200},
    {"video_id": 2, "subscription_id": "sub-b", "updated_at": 150},
)

_PLAIN_JOIN = "LEFT JOIN feed_entries fe ON fe.video_id = v.id"


class _EmulatingCursor:
    """Fake cursor that joins _VIDEOS to _ENTRIES the way Postgres would.

    If the executed SQL uses the LATERAL latest-row fragment, each video
    yields one row (newest entry wins); a plain join fans out to one row per
    matching entry -- which is exactly the bug under test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> "_EmulatingCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        deduped = "DISTINCT ON" in query and "fe_dedup.video_id" in query
        videos = list(_VIDEOS)
        if "v.youtube_video_id = %s" in query and params:
            # The id is the last parameter (any user_key for the subscription join comes first).
            videos = [v for v in videos if v["youtube_video_id"] == params[-1]]
        rows: list[dict[str, Any]] = []
        for video in videos:
            matches = sorted(
                (e for e in _ENTRIES if e["video_id"] == video["id"]),
                key=lambda e: e["updated_at"],
                reverse=True,
            )
            picked = matches[:1] if deduped else (matches or [None])
            for entry in picked:
                rows.append(
                    {
                        "id": video["id"],
                        "youtube_video_id": video["youtube_video_id"],
                        "title": video["title"],
                        "subscription_id": entry["subscription_id"] if entry else None,
                    }
                )
        if "LIMIT %s" in query and params and isinstance(params[-1], int):
            rows = rows[: params[-1]]
        elif "LIMIT 1" in query:
            rows = rows[:1]
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


def _run_emulated(callback: Any) -> tuple[Any, _EmulatingCursor]:
    cursor = _EmulatingCursor()
    with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
        return callback(), cursor


class FeedEntryLatestJoinTests(unittest.TestCase):
    def test_fragment_contract(self) -> None:
        for fragment in (db._FEED_ENTRY_LATEST_JOIN, db._FEED_ENTRY_LATEST_CTE):
            self.assertIn("DISTINCT ON", fragment)
            self.assertIn("ORDER BY fe_dedup.video_id, fe_dedup.updated_at DESC", fragment)
            for column in ("subscription_id", "published_at", "created_at"):
                self.assertIn(column, fragment)
            # The old reader's install-wide read/starred flags are gone (migrations.py).
            for column in ("is_read", "is_starred"):
                self.assertNotIn(column, fragment)
        self.assertIn("fe.video_id = v.id", db._FEED_ENTRY_LATEST_JOIN)
        self.assertIn("fe.video_id = v.id", db._FEED_ENTRY_LATEST_JOIN_CTE)

    def test_list_recent_videos_returns_one_row_per_video(self) -> None:
        rows, _ = _run_emulated(lambda: db.list_recent_videos(50))
        self.assertEqual([r["youtube_video_id"] for r in rows], ["vid-one", "vid-two"])
        newest = rows[0]
        self.assertEqual(newest["subscription_id"], "sub-c")

    def test_list_collection_videos_returns_one_row_per_video(self) -> None:
        rows, _ = _run_emulated(lambda: db.list_collection_videos("col-1", 80, "local"))
        self.assertEqual([r["youtube_video_id"] for r in rows], ["vid-one", "vid-two"])
        self.assertEqual(rows[0]["subscription_id"], "sub-c")

    def test_list_playlist_videos_returns_one_row_per_video(self) -> None:
        rows, _ = _run_emulated(lambda: db.list_playlist_videos("pl-1", 200, "local"))
        self.assertEqual([r["youtube_video_id"] for r in rows], ["vid-one", "vid-two"])
        self.assertEqual(rows[0]["subscription_id"], "sub-c")

    def test_get_video_by_youtube_id_returns_single_newest_row(self) -> None:
        row, _ = _run_emulated(lambda: db.get_video_by_youtube_id("vid-one"))
        assert row is not None
        self.assertEqual(row["youtube_video_id"], "vid-one")
        self.assertEqual(row["subscription_id"], "sub-c")

    def test_all_video_queries_reuse_shared_fragment(self) -> None:
        direct_calls = [
            lambda: db.list_recent_videos(50),
            lambda: db.get_video_by_youtube_id("vid-one"),
            lambda: db.search_videos("hello", 50),
            lambda: db.list_collection_videos("col-1", 80, "local"),
            lambda: db.list_playlist_videos("pl-1", 200, "local"),
            lambda: db.list_library_activity(80, "local"),
            lambda: db.list_subscription_recommendations(50, "local"),
            lambda: db.list_history_videos(80, "local"),
            lambda: db.list_channel_videos("chan-1", 50),
        ]
        ranked_calls = [
            lambda: db.list_recommended_videos(10, 0, "all", "local"),
            lambda: db.list_discovery_videos(50, "local"),
        ]
        # _subscription_interest_profile reads playback_progress, not feed_entries.
        # Liked videos are the user's own likes and no longer read feed_entries at all
        # (the old reader's shared stars used to be mixed in).
        self.assertEqual(len(direct_calls) + len(ranked_calls), 11)
        liked = _capture_all_sql(lambda: db.list_liked_videos(80, "local"))
        self.assertTrue(all("feed_entries" not in query and "is_starred" not in query for query, _ in liked))
        for call in direct_calls:
            self._assert_uses_fragment(call)
        # Ranked feeds post-process rows with DB-backed helpers; stub those so
        # the test still asserts on the SQL they actually execute.
        with (
            patch.object(db, "_subscription_interest_profile", return_value={"tokens": {}, "channels": set(), "count": 0}),
            patch.object(db, "get_feed_preferences", return_value={}),
            patch.object(db, "watch_time_profile", return_value={"channels": {}, "topics": {}, "completions": {}, "confidence": 0.0}),
            patch.object(db, "takeout_profile", return_value={"channels": {}, "confidence": 0.0, "watched_videos": []}),
            patch("openfeed.topics.profile_vector", return_value={}),
        ):
            for call in ranked_calls:
                self._assert_uses_fragment(call)

    def _assert_uses_fragment(self, call: Any) -> None:
        calls = _capture_all_sql(call)
        # Ranked feeds run helper queries (topic vectors, profiles) that never
        # touch feed_entries; assert on the video queries that do.
        video_queries = [q for q, _ in calls if "feed_entries" in q or "DISTINCT ON" in q]
        self.assertTrue(video_queries, "expected at least one video query")
        for query in video_queries:
            # Full-library ranking queries use the materialized-CTE twin of the
            # shared fragment (same dedup, evaluated once instead of once per
            # row); everything else inlines the fragment directly.
            uses_inline = db._FEED_ENTRY_LATEST_JOIN in query
            uses_cte = db._FEED_ENTRY_LATEST_CTE in query and db._FEED_ENTRY_LATEST_JOIN_CTE in query
            self.assertTrue(uses_inline or uses_cte, "video query must reuse a shared dedup fragment")
            self.assertNotIn(_PLAIN_JOIN, query)
        for query, _ in calls:
            self.assertNotIn(_PLAIN_JOIN, query)

    def test_user_input_stays_parameterized(self) -> None:
        hostile_values = (
            "pl-' OR '1'='1",
            "col-'; DROP TABLE videos; --",
            "vid-'; DROP TABLE videos; --",
        )
        calls = (
            lambda: db.list_playlist_videos(hostile_values[0], 200, "local"),
            lambda: db.list_collection_videos(hostile_values[1], 80, "local"),
            lambda: db.get_video_by_youtube_id(hostile_values[2]),
            lambda: db.search_videos(hostile_values[2], 50),
        )
        for call in calls:
            query, _params = _capture_sql(call)
            self.assertIn("%s", query)
            for hostile in hostile_values:
                self.assertNotIn(hostile, query)


if __name__ == "__main__":
    unittest.main()
