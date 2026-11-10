"""Channel pages and search show the user's watched state on cards.

list_channel_videos and search read videos without a user, so a video marked watched still
showed "Mark watched" and no check on those pages (QA retest, 2026-11-09). The fix merges
the user's playback_progress into the rows, keyed by YouTube id so YouTube search results,
which have no library id, are covered too.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from openfeed import db, main


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> _Result:
        self.calls.append((query, params))
        return _Result(self.rows)


def _patched(conn: _Conn):
    @contextmanager
    def fake_connect():
        yield conn

    return patch.object(db, "connect", fake_connect)


_STATE = {"youtube_video_id": "watched1", "percent_completed": 1.0, "progress_completed": True,
          "manually_marked_watched": True, "resume_seconds": None}


class AttachUserProgressTests(unittest.TestCase):
    def test_merges_state_by_youtube_id_for_this_user(self) -> None:
        conn = _Conn([_STATE])
        videos = [{"youtube_video_id": "watched1", "title": "A"}, {"youtube_video_id": "fresh", "title": "B"}]
        with _patched(conn):
            db.attach_user_progress(videos, "alice")
        self.assertTrue(videos[0]["manually_marked_watched"])
        self.assertTrue(main._is_watched(videos[0]))
        self.assertNotIn("progress_completed", videos[1])
        query, params = conn.calls[0]
        self.assertIn("user_key = %s", query)
        self.assertEqual(params[0], "alice")
        self.assertEqual(sorted(params[1]), ["fresh", "watched1"])

    def test_rows_that_already_carry_progress_are_not_queried(self) -> None:
        conn = _Conn([])
        videos = [{"youtube_video_id": "x", "progress_completed": False}]
        with _patched(conn):
            db.attach_user_progress(videos, "local")
        self.assertEqual(conn.calls, [])

    def test_card_menu_offers_mark_unwatched_once_attached(self) -> None:
        conn = _Conn([_STATE])
        video = {"youtube_video_id": "watched1", "title": "A", "id": 1}
        with _patched(conn):
            main._with_progress([video], "local")
        html = main._home_video_card(video, 1, main.get_settings())
        self.assertIn("Mark unwatched", html)

    def test_failure_leaves_cards_unchanged(self) -> None:
        videos = [{"youtube_video_id": "x"}]
        with patch.object(db, "attach_user_progress", side_effect=RuntimeError("db down")):
            self.assertIs(main._with_progress(videos, "local"), videos)


if __name__ == "__main__":
    unittest.main()
