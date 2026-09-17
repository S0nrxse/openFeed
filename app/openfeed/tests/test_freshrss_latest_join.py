"""Regression tests for the freshrss_entries fan-out fix.

A video syndicated in two feeds has two freshrss_entries rows, and every video
query used to `LEFT JOIN freshrss_entries fe ON fe.video_id = v.id`, which
multiplied one video into N rows and made LIMITs count duplicates. All such
queries must reuse the shared ``db._FRESHRSS_LATEST_JOIN`` fragment
(``DISTINCT ON (video_id) ... ORDER BY video_id, updated_at DESC``): one row per
video, newest row wins for is_read/is_starred. Full-library ranking queries use
its materialized-CTE twin (``db._FRESHRSS_LATEST_CTE`` +
``db._FRESHRSS_LATEST_JOIN_CTE``), which dedupes identically but evaluates once
instead of once per video row.

DB/network-free: a fake cursor emulates Postgres join semantics against an
in-memory table -- a plain join fans out, the dedup fragment keeps one row per
video -- so the tests fail if any query regresses to the plain join, without
needing a database. Follows the fake-cursor style of test_db_queries.py.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Make `from openfeed import db` work however the suite is launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# psycopg is a runtime dependency that may not be installed where tests run;
# db.py only needs it inside connect(), which these tests stub out.
if "psycopg" not in sys.modules:
    _psycopg = types.ModuleType("psycopg")
    _rows = types.ModuleType("psycopg.rows")
    _rows.dict_row = object  # type: ignore[attr-defined]
    _psycopg.rows = _rows  # type: ignore[attr-defined]
    sys.modules["psycopg"] = _psycopg
    sys.modules["psycopg.rows"] = _rows

from openfeed import db


# Two videos; video 1 is syndicated in two feeds (two entries rows).
# The newer row flips is_read/is_starred so "newest wins" is observable.
_VIDEOS = (
    {"id": 1, "youtube_video_id": "vid-one", "title": "Video One"},
    {"id": 2, "youtube_video_id": "vid-two", "title": "Video Two"},
)
_ENTRIES = (
    {"video_id": 1, "is_read": True, "is_starred": False, "subscription_id": "sub-a", "updated_at": 100},
    {"video_id": 1, "is_read": False, "is_starred": True, "subscription_id": "sub-a", "updated_at": 200},
    {"video_id": 2, "is_read": True, "is_starred": False, "subscription_id": "sub-b", "updated_at": 150},
)

_PLAIN_JOIN = "LEFT JOIN freshrss_entries fe ON fe.video_id = v.id"


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
            videos = [v for v in videos if v["youtube_video_id"] == params[0]]
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
                        "is_read": entry["is_read"] if entry else None,
                        "is_starred": entry["is_starred"] if entry else None,
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


class _EmptyCursor(_EmulatingCursor):
    """Captures SQL only; returns no rows (safe for post-processing helpers)."""

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        self._rows = []


class _FakeConnection:
    def __init__(self, cursor: _EmulatingCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def cursor(self) -> _EmulatingCursor:
        return self._cursor

    def commit(self) -> None:
        return None


def _run_emulated(callback: Any) -> tuple[Any, _EmulatingCursor]:
    cursor = _EmulatingCursor()
    with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
        return callback(), cursor


def _capture_sql(callback: Any) -> tuple[str, Any]:
    cursor = _EmptyCursor()
    with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
        callback()
    return cursor.calls[-1]


def _capture_all_sql(callback: Any) -> list[tuple[str, Any]]:
    cursor = _EmptyCursor()
    with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
        callback()
    return list(cursor.calls)


class FreshrssLatestJoinTests(unittest.TestCase):
    def test_fragment_contract(self) -> None:
        for fragment in (db._FRESHRSS_LATEST_JOIN, db._FRESHRSS_LATEST_CTE):
            self.assertIn("DISTINCT ON", fragment)
            self.assertIn("ORDER BY fe_dedup.video_id, fe_dedup.updated_at DESC", fragment)
            for column in ("is_read", "is_starred", "subscription_id", "published_at", "created_at"):
                self.assertIn(column, fragment)
        self.assertIn("fe.video_id = v.id", db._FRESHRSS_LATEST_JOIN)
        self.assertIn("fe.video_id = v.id", db._FRESHRSS_LATEST_JOIN_CTE)

    def test_list_recent_videos_returns_one_row_per_video(self) -> None:
        rows, _ = _run_emulated(lambda: db.list_recent_videos(50))
        self.assertEqual([r["youtube_video_id"] for r in rows], ["vid-one", "vid-two"])
        newest = rows[0]
        self.assertEqual((newest["is_read"], newest["is_starred"]), (False, True))

    def test_list_collection_videos_returns_one_row_per_video(self) -> None:
        rows, _ = _run_emulated(lambda: db.list_collection_videos("col-1", 80, "local"))
        self.assertEqual([r["youtube_video_id"] for r in rows], ["vid-one", "vid-two"])
        self.assertEqual((rows[0]["is_read"], rows[0]["is_starred"]), (False, True))

    def test_list_playlist_videos_returns_one_row_per_video(self) -> None:
        rows, _ = _run_emulated(lambda: db.list_playlist_videos("pl-1", 200, "local"))
        self.assertEqual([r["youtube_video_id"] for r in rows], ["vid-one", "vid-two"])
        self.assertEqual((rows[0]["is_read"], rows[0]["is_starred"]), (False, True))

    def test_get_video_by_youtube_id_returns_single_newest_row(self) -> None:
        row, _ = _run_emulated(lambda: db.get_video_by_youtube_id("vid-one"))
        assert row is not None
        self.assertEqual(row["youtube_video_id"], "vid-one")
        self.assertEqual((row["is_read"], row["is_starred"]), (False, True))

    def test_all_video_queries_reuse_shared_fragment(self) -> None:
        direct_calls = [
            lambda: db.list_recent_videos(50),
            lambda: db.get_video_by_youtube_id("vid-one"),
            lambda: db.search_videos("hello", 50),
            lambda: db.list_collection_videos("col-1", 80, "local"),
            lambda: db.list_playlist_videos("pl-1", 200, "local"),
            lambda: db.list_library_activity(80, "local"),
            lambda: db.list_subscription_recommendations(50, "local"),
            lambda: db.list_liked_videos(80, "local"),
            lambda: db.list_history_videos(80, "local"),
            lambda: db.list_channel_videos("chan-1", 50),
        ]
        ranked_calls = [
            lambda: db.list_recommended_videos(10, 0, "all", "local"),
            lambda: db.list_discovery_videos(50, "local"),
        ]
        # _subscription_interest_profile reads playback_progress, not
        # freshrss_entries, so it has no read/starred state to dedupe.
        self.assertEqual(len(direct_calls) + len(ranked_calls), 12)
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
        # touch freshrss_entries; assert on the video queries that do.
        video_queries = [q for q, _ in calls if "freshrss_entries" in q or "DISTINCT ON" in q]
        self.assertTrue(video_queries, "expected at least one video query")
        for query in video_queries:
            # Full-library ranking queries use the materialized-CTE twin of the
            # shared fragment (same dedup, evaluated once instead of once per
            # row); everything else inlines the fragment directly.
            uses_inline = db._FRESHRSS_LATEST_JOIN in query
            uses_cte = db._FRESHRSS_LATEST_CTE in query and db._FRESHRSS_LATEST_JOIN_CTE in query
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
