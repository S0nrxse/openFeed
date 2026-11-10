"""Search type-ahead, the real search sorts, search inside playlists, Watch later on cards,
Undo for feedback, the Not interested list, pull to refresh and Back to top."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from openfeed import db, discovery, main, search_extras
from openfeed.tests.support import offline_db


def _request(path: str = "/", query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
                    "headers": [(b"accept", b"text/html")], "scheme": "http", "server": ("testserver", 80)})


class _Cursor:
    """Records SQL and answers each execute with the next queued result."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.results = list(results or [])
        self._last: Any = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        self._last = self.results.pop(0) if self.results else []

    def fetchone(self) -> Any:
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self) -> list[Any]:
        return self._last if isinstance(self._last, list) else [self._last]


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _with_db(results: list[Any] | None = None) -> tuple[_Cursor, Any]:
    cursor = _Cursor(results)
    conn = _Conn(cursor)
    return cursor, patch.object(db, "connect", return_value=conn)


def _video(video_id: str, published: datetime | None = None, **extra: Any) -> dict[str, Any]:
    return {"youtube_video_id": video_id, "title": f"Video {video_id}", "published_at": published, **extra}


class _Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _Client:
    calls: list[dict[str, Any]] = []
    payload: Any = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, params: dict[str, Any] | None = None) -> _Response:
        _Client.calls.append({"url": url, "params": params})
        return _Response(_Client.payload)


class TypeAheadTests(unittest.TestCase):
    def setUp(self) -> None:
        search_extras._suggest_cache.clear()
        _Client.calls = []

    def test_suggestions_come_from_invidious_once_per_prefix(self) -> None:
        _Client.payload = {"query": "minecraft bu", "suggestions": ["minecraft builds", "Minecraft Builds", "a &amp; b", 7]}
        settings = SimpleNamespace(invidious_base_url="http://inv")
        with patch.object(search_extras.httpx, "Client", _Client), patch.object(search_extras, "get_settings", return_value=settings):
            first = search_extras.suggestions("  minecraft   bu ")
            second = search_extras.suggestions("MINECRAFT BU")
        self.assertEqual(first, ["minecraft builds", "a & b"])
        self.assertEqual(second, first)
        self.assertEqual(len(_Client.calls), 1)
        self.assertEqual(_Client.calls[0]["url"], "http://inv/api/v1/search/suggestions")
        self.assertEqual(_Client.calls[0]["params"], {"q": "minecraft bu"})

    def test_nothing_typed_or_no_backend_asks_nobody(self) -> None:
        with patch.object(search_extras.httpx, "Client", _Client), \
             patch.object(search_extras, "get_settings", return_value=SimpleNamespace(invidious_base_url="")):
            self.assertEqual(search_extras.suggestions("   "), [])
            self.assertEqual(search_extras.suggestions("wood"), [])
        self.assertEqual(_Client.calls, [])

    def test_a_failing_backend_returns_nothing_and_is_not_cached(self) -> None:
        class _Broken(_Client):
            def get(self, url: str, params: dict[str, Any] | None = None) -> _Response:
                return _Response({}, 502)

        with patch.object(search_extras.httpx, "Client", _Broken), \
             patch.object(search_extras, "get_settings", return_value=SimpleNamespace(invidious_base_url="http://inv")):
            self.assertEqual(search_extras.suggestions("wood"), [])
        self.assertNotIn("wood", search_extras._suggest_cache)

    def test_the_endpoint_returns_the_list(self) -> None:
        with patch.object(search_extras, "suggestions", return_value=["wood carving"]) as found:
            response = TestClient(main.app).get("/api/search/suggestions?q=wood")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"query": "wood", "suggestions": ["wood carving"]})
        self.assertIn("max-age", response.headers.get("cache-control", ""))
        # Sign-in is off: one user, one cache scope.
        found.assert_called_once_with("wood", scope="")

    def test_the_box_is_a_combobox_that_reuses_the_phone_recents(self) -> None:
        script = main._home_script()
        self.assertIn("'openfeed-recent-searches'", script)
        self.assertIn("setAttribute('role', 'combobox')", script)
        self.assertIn("/api/search/suggestions?q=", script)
        self.assertIn("aria-activedescendant", script)
        self.assertIn("Remove ${item.text} from recent searches", script)
        styles = main._home_styles()
        self.assertIn(".suggest-panel", styles)
        self.assertIn(".suggest-row.is-recent", styles)


class UploadDateSortTests(unittest.TestCase):
    def test_windows_never_reach_past_the_chosen_date(self) -> None:
        self.assertEqual(search_extras.date_windows(""), ("today", "week", "month", "year", ""))
        self.assertEqual(search_extras.date_windows("week"), ("today", "week"))
        self.assertEqual(search_extras.date_windows("hour"), ("hour",))
        self.assertEqual(search_extras.date_windows("bogus"), search_extras.date_windows(""))

    def test_windows_are_merged_newest_first_without_repeats(self) -> None:
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 11, 9, tzinfo=timezone.utc)
        mid = datetime(2026, 6, 1, tzinfo=timezone.utc)
        answers = {"today": [_video("n", new)], "week": [_video("n", new), _video("m", mid)], "month": [],
                   "year": [_video("m", mid)], "": [_video("o", old), _video("x", None)]}
        seen: list[dict[str, Any]] = []

        def fake(query: str, page: int = 1, sort: str = "", date: str = "", duration: str = "", features: str = "") -> list:
            seen.append({"date": date, "sort": sort, "page": page, "duration": duration})
            return answers[date]

        with patch.object(discovery, "search_youtube", side_effect=fake):
            rows = search_extras.search_newest_first("wood", 2, "", "long", "")
        self.assertEqual([row["youtube_video_id"] for row in rows], ["n", "m", "o", "x"])
        self.assertEqual(sorted(call["date"] for call in seen), sorted(["today", "week", "month", "year", ""]))
        self.assertTrue(all(call["page"] == 2 and call["sort"] == "relevance" and call["duration"] == "long" for call in seen))

    def test_one_failed_window_is_left_out_and_all_failing_raises(self) -> None:
        def flaky(query: str, page: int = 1, sort: str = "", date: str = "", **_: Any) -> list:
            if date == "week":
                raise RuntimeError("timeout")
            return [_video(date or "any", datetime(2026, 1, 1, tzinfo=timezone.utc))]

        with patch.object(discovery, "search_youtube", side_effect=flaky):
            self.assertEqual(len(search_extras.search_newest_first("wood", 1, "month")), 2)
        with patch.object(discovery, "search_youtube", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                search_extras.search_newest_first("wood", 1)

    def test_the_search_page_uses_the_windows_for_upload_date_only(self) -> None:
        filters = main._search_filters("all", "upload_date", "", "")
        with patch.object(search_extras, "search_newest_first", return_value=[_video("a")]) as newest, \
             patch.object(discovery, "search_youtube") as plain:
            rows, scanned = main._search_youtube_videos("wood", 1, filters)
        newest.assert_called_once_with("wood", 1, "", "", "")
        plain.assert_not_called()
        self.assertEqual((len(rows), scanned), (1, 1))
        with patch.object(search_extras, "search_newest_first") as newest, \
             patch.object(discovery, "search_youtube", return_value=[]) as plain:
            main._search_youtube_videos("wood", 1, main._search_filters("all", "view_count", "", ""))
        newest.assert_not_called()
        self.assertEqual(plain.call_args.kwargs["sort"], "view_count")


class RatingSortTests(unittest.TestCase):
    def test_rating_is_only_offered_with_return_youtube_dislike(self) -> None:
        filters = main._search_filters("all", "relevance", "", "")
        self.assertNotIn("Rating", main._search_controls("wood", filters))
        self.assertIn("Rating", main._search_controls("wood", filters, rating=True))
        self.assertIn("Upload date", main._search_controls("wood", filters))
        self.assertIn("View count", main._search_controls("wood", filters))

    def test_a_rating_link_without_ratings_falls_back_but_can_be_cleared(self) -> None:
        filters = main._search_filters("all", "rating", "", "")
        with patch.object(search_extras, "rating_available", return_value=False):
            self.assertFalse(main._search_rating_gate(filters, "local"))
        self.assertEqual(filters["sort"], "relevance")
        self.assertIn("data-clear-search-filters", main._search_controls("wood", filters))
        kept = main._search_filters("all", "rating", "", "")
        with patch.object(search_extras, "rating_available", return_value=True):
            self.assertTrue(main._search_rating_gate(kept, "local"))
        self.assertEqual(kept["sort"], "rating")
        self.assertFalse(main._search_rating_gate(main._search_filters("all", "rating", "", ""), ""))

    def test_the_ratio_needs_enough_votes(self) -> None:
        self.assertIsNone(search_extras.rating_of(5, 1))
        self.assertIsNone(search_extras.rating_of(None, 30))
        self.assertAlmostEqual(search_extras.rating_of(90, 10), 0.9)

    def test_rated_videos_lead_and_unrated_keep_their_order(self) -> None:
        rows = [_video("u1"), _video("low"), _video("u2"), _video("high")]
        ordered = search_extras.order_by_rating(rows, {"low": 0.6, "high": 0.97})
        self.assertEqual([row["youtube_video_id"] for row in ordered], ["high", "low", "u1", "u2"])

    def test_stored_ratings_read_only_ryd_counts(self) -> None:
        cursor, connect = _with_db([[{"youtube_video_id": "a", "like_count": 95, "dislike_count": 5},
                                     {"youtube_video_id": "b", "like_count": 1, "dislike_count": 0}]])
        with connect:
            found = search_extras.stored_ratings(["a", "b", "a", ""])
        self.assertEqual(found, {"a": 0.95})
        sql, params = cursor.calls[0]
        self.assertIn("dislike_source = 'ryd'", sql)
        self.assertEqual(params, (["a", "b"],))


class PlaylistSearchTests(unittest.TestCase):
    def test_matches_are_the_callers_own_playlist_items(self) -> None:
        cursor, connect = _with_db([[{"youtube_video_id": "a", "playlist_names": ["Watch later"]}]])
        with connect:
            rows = search_extras.playlist_matches("50%_off", "alice")
        self.assertEqual(rows[0]["youtube_video_id"], "a")
        sql, params = cursor.calls[0]
        self.assertIn("p.user_key = %s", sql)
        self.assertIn("v.title ILIKE %s", sql)
        self.assertEqual(params[0], "alice")
        self.assertEqual(params[1], "%50\\%\\_off%")

    def test_the_note_names_the_playlists(self) -> None:
        self.assertEqual(search_extras.playlist_note(["Watch later"]), "In Watch later")
        self.assertEqual(search_extras.playlist_note(["b", "A"]), "In A and b")
        self.assertEqual(search_extras.playlist_note(["c", "b", "a", "d"]), "In a, b and 2 more")
        self.assertEqual(search_extras.playlist_note([]), "In your playlists")

    def test_the_search_page_shows_them_first_and_not_twice(self) -> None:
        settings = SimpleNamespace(invidious_public_url="")
        saved = {**_video("saved1"), "playlist_names": ["Woodworking"], "channel_title": "Shop"}
        library = [_video("saved1"), _video("lib2")]
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(main, "_search_user_key", return_value="local"), \
             patch.object(search_extras, "rating_available", return_value=False), \
             patch.object(search_extras, "playlist_matches", return_value=[saved]), \
             patch.object(main, "_search_youtube_videos", return_value=([], 0)), \
             patch.object(main, "_search_channels", return_value=([], "", False)), \
             patch.object(db, "search_videos", return_value=library), \
             patch.object(db, "attach_user_progress", side_effect=lambda rows, key: rows), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: content):
            html = main.search_page(_request("/search"), q="wood")
        self.assertIn("In your playlists", html)
        self.assertIn("In Woodworking", html)
        self.assertLess(html.index("data-search-playlist-hits"), html.index("data-search-list"))
        self.assertEqual(html.count('data-video-card data-video-id="saved1"'), 1)
        self.assertIn('data-video-card data-video-id="lib2"', html)
        self.assertIn("2 results", html)

    def test_filtered_searches_and_channel_searches_skip_the_block(self) -> None:
        with patch.object(search_extras, "playlist_matches") as matches:
            self.assertEqual(main._search_playlist_hits("wood", "local", main._search_filters("all", "relevance", "week", "")), [])
            self.assertEqual(main._search_playlist_hits("wood", "local", main._search_filters("channels", "relevance", "", "")), [])
        matches.assert_not_called()

    def test_playlist_pages_keep_their_filter_box(self) -> None:
        html = main._detail_layout("Watch later", [], "", 2, "", "<div></div>", filter_label="Filter this playlist", noun="video")
        self.assertIn('placeholder="Filter this playlist"', html)
        self.assertIn("data-filter-input", html)


class WatchLaterTests(unittest.TestCase):
    def test_cards_offer_it_on_hover_and_in_the_menu(self) -> None:
        quick = main._card_quick_actions("dQw4w9WgXcQ", "Title", False)
        self.assertIn('data-watch-later="dQw4w9WgXcQ"', quick)
        self.assertIn('aria-label="Save to Watch later"', quick)
        self.assertIn('aria-pressed="false"', quick)
        menu = main._video_action_menu("dQw4w9WgXcQ", "Title", "/watch/dQw4w9WgXcQ", "https://y")
        self.assertIn("Save to Watch later", menu)
        self.assertLess(menu.index("Save to Watch later"), menu.index("Save to playlist"))

    def test_the_playlist_is_found_by_name_and_made_once_under_a_lock(self) -> None:
        cursor, connect = _with_db([[], [], [{"id": "p1", "name": "Watch later"}]])
        with connect:
            playlist = search_extras.watch_later_playlist("alice", create=True)
        self.assertEqual(playlist, {"id": "p1", "name": "Watch later"})
        statements = [sql for sql, _ in cursor.calls]
        self.assertIn("pg_advisory_xact_lock", statements[0])
        self.assertIn("lower(btrim(name)) = 'watch later'", statements[1])
        self.assertIn("INSERT INTO playlists", statements[2])
        self.assertEqual(cursor.calls[2][1], ("alice", "Watch later"))

    def test_reading_never_creates(self) -> None:
        cursor, connect = _with_db([[]])
        with connect:
            self.assertIsNone(search_extras.watch_later_playlist("alice"))
        self.assertEqual(len(cursor.calls), 1)

    def test_saving_and_removing_go_through_the_playlist(self) -> None:
        with patch.object(search_extras, "watch_later_playlist", return_value={"id": "p1"}), \
             patch.object(db, "add_video_to_playlist", return_value={"id": 1}) as add, \
             patch.object(db, "remove_video_from_playlist", return_value=1) as remove:
            self.assertEqual(search_extras.set_watch_later("dQw4w9WgXcQ", True, "alice"), {"saved": True, "playlist_id": "p1"})
            self.assertEqual(search_extras.set_watch_later("dQw4w9WgXcQ", False, "alice"), {"saved": False, "playlist_id": "p1"})
        add.assert_called_once_with("p1", "dQw4w9WgXcQ", "alice")
        remove.assert_called_once_with("p1", "dQw4w9WgXcQ", "alice")

    def test_the_endpoints(self) -> None:
        client = TestClient(main.app)
        with patch.object(search_extras, "set_watch_later", return_value={"saved": True, "playlist_id": "p1"}) as setter:
            response = client.post("/api/watch-later", json={"youtube_video_id": "dQw4w9WgXcQ", "saved": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["saved"])
        setter.assert_called_once_with("dQw4w9WgXcQ", True, "local")
        self.assertEqual(client.post("/api/watch-later", json={"youtube_video_id": "../etc"}).status_code, 400)
        with patch.object(search_extras, "set_watch_later", return_value=None):
            self.assertEqual(client.post("/api/watch-later", json={"youtube_video_id": "dQw4w9WgXcQ"}).status_code, 404)
        with patch.object(search_extras, "watch_later_playlist", return_value={"id": "p1"}), \
             patch.object(search_extras, "watch_later_ids", return_value=["dQw4w9WgXcQ"]):
            listing = client.get("/api/watch-later").json()
        self.assertEqual(listing["ids"], ["dQw4w9WgXcQ"])

    def test_the_script_toggles_with_a_toast_and_undo(self) -> None:
        script = main._home_script()
        self.assertIn("'Saved to Watch later'", script)
        self.assertIn("'Removed from Watch later'", script)
        self.assertIn("fetch('/api/watch-later')", script)


class FeedbackUndoTests(unittest.TestCase):
    def test_feedback_reports_what_undo_puts_back(self) -> None:
        with patch.object(db, "get_video_feedback", return_value={"reaction": "like", "not_interested": False}), \
             patch.object(db, "record_video_event", return_value={"id": 41, "event_type": "less_like_this"}), \
             patch.object(db, "set_video_feedback", return_value={"reaction": "dislike"}):
            body = TestClient(main.app).post("/api/feedback", json={"youtube_video_id": "dQw4w9WgXcQ", "event_type": "less_like_this"}).json()
        self.assertEqual(body["previous"], {"reaction": "like", "not_interested": False, "not_interested_reason": None})
        self.assertEqual(body["event"]["id"], 41)

    def test_other_events_skip_the_extra_read(self) -> None:
        with patch.object(db, "get_video_feedback", return_value={}) as read, \
             patch.object(db, "record_video_event", return_value={"id": 1}), \
             patch.object(db, "set_video_feedback", return_value={"reaction": None}), \
             patch.object(db, "update_playback_progress", return_value={}):
            body = TestClient(main.app).post("/api/feedback", json={"youtube_video_id": "dQw4w9WgXcQ", "event_type": "watch_page_open"}).json()
        self.assertNotIn("previous", body)
        read.assert_not_called()

    def test_undo_deletes_the_event_and_restores_the_row(self) -> None:
        cursor, connect = _with_db([[{"video_id": 7}], []])
        with connect:
            done = search_extras.undo_feedback("dQw4w9WgXcQ", 41, {"reaction": "like", "not_interested": False}, "alice")
        self.assertTrue(done)
        delete_sql, delete_params = cursor.calls[0]
        self.assertIn("DELETE FROM user_video_events", delete_sql)
        self.assertEqual(delete_params[:3], (41, "alice", "dQw4w9WgXcQ"))
        self.assertIn("less_like_this", delete_params[3])
        update_sql, update_params = cursor.calls[1]
        self.assertIn("UPDATE user_video_feedback", update_sql)
        self.assertEqual(update_params, ("like", False, None, "alice", 7))

    def test_undo_of_someone_elses_event_changes_nothing(self) -> None:
        cursor, connect = _with_db([[]])
        with connect:
            self.assertFalse(search_extras.undo_feedback("dQw4w9WgXcQ", 41, {}, "alice"))
        self.assertEqual(len(cursor.calls), 1)

    def test_a_forged_previous_state_is_reduced_to_valid_values(self) -> None:
        self.assertEqual(search_extras.feedback_snapshot({"reaction": "<script>", "not_interested": "yes", "not_interested_reason": "x" * 900}),
                         {"reaction": None, "not_interested": True, "not_interested_reason": "x" * 500})

    def test_the_endpoint(self) -> None:
        client = TestClient(main.app)
        with patch.object(search_extras, "undo_feedback", return_value=True) as undo, \
             patch.object(db, "clear_watch_time_cache"), \
             patch.object(db, "get_video_feedback", return_value={"reaction": None}):
            response = client.post("/api/feedback/undo", json={"youtube_video_id": "dQw4w9WgXcQ", "event_id": 41, "previous": {"reaction": None}})
        self.assertEqual(response.status_code, 200)
        undo.assert_called_once_with("dQw4w9WgXcQ", 41, {"reaction": None}, "local")
        with patch.object(search_extras, "undo_feedback", return_value=False):
            self.assertEqual(client.post("/api/feedback/undo", json={"youtube_video_id": "x", "event_id": 1}).status_code, 404)

    def test_the_toast_offers_undo_for_more_and_less_like_this(self) -> None:
        script = main._home_script()
        self.assertIn("window.ofFeedbackToast(videoId, eventType, data)", script)
        self.assertIn("/api/feedback/undo", script)
        self.assertIn("ll see more like this", script)


class NotInterestedTests(unittest.TestCase):
    def test_the_list_reads_not_interested_and_less_like_this(self) -> None:
        cursor, connect = _with_db([[]])
        with connect:
            search_extras.list_not_interested_videos("alice")
        sql, params = cursor.calls[0]
        self.assertIn("f.not_interested", sql)
        self.assertIn("event_type = 'less_like_this'", sql)
        self.assertEqual(params, ("alice", 300))

    def test_removing_clears_the_flags_and_the_events(self) -> None:
        cursor, connect = _with_db([[{"video_id": 9}], []])
        with connect:
            self.assertEqual(search_extras.clear_not_interested("dQw4w9WgXcQ", "alice"), 1)
        self.assertIn("not_interested = false", cursor.calls[0][0])
        self.assertIn("DELETE FROM user_video_events", cursor.calls[1][0])
        self.assertEqual(cursor.calls[1][1], ("alice", 9, ["hide", "not_interested", "less_like_this"]))

    def test_muted_channels_get_their_names(self) -> None:
        cursor, connect = _with_db([[{"youtube_channel_id": "UCabc", "title": "Rick", "avatar_url": None, "handle": None}]])
        with patch.object(db, "get_feed_preferences", return_value={"muted_channels": ["ucabc", "some name"]}), connect:
            rows = search_extras.muted_channel_rows("alice")
        self.assertEqual([row["key"] for row in rows], ["some name", "ucabc"])
        self.assertEqual(rows[1]["title"], "Rick")
        self.assertNotIn("title", rows[0])

    def test_the_page_lists_both_with_remove_controls(self) -> None:
        videos = [{**_video("dQw4w9WgXcQ"), "not_interested": True, "channel_title": "Rick"},
                  {**_video("abcdefghijk"), "not_interested": False, "reaction": "dislike"}]
        channels = [{"key": "ucabc", "youtube_channel_id": "UCabc", "title": "Rick"}, {"key": "gone"}]
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(search_extras, "list_not_interested_videos", return_value=videos), \
             patch.object(search_extras, "muted_channel_rows", return_value=channels), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: content):
            html = main.not_interested_page(_request("/not-interested"))
        self.assertIn('data-restore-video="dQw4w9WgXcQ"', html)
        self.assertIn("Not interested", html)
        self.assertIn("Less like this", html)
        self.assertIn('data-unmute-interest="ucabc"', html)
        self.assertIn('aria-label="Remove from Not interested: Rick"', html)
        self.assertIn("4 items", html)
        self.assertIn('placeholder="Filter this list"', html)

    def test_an_empty_list_says_how_things_get_here(self) -> None:
        html = main._not_interested_body([], [], SimpleNamespace(invidious_public_url=""))
        self.assertIn("empty-state", html)
        self.assertIn("Not interested", html)

    def test_the_delete_endpoint(self) -> None:
        client = TestClient(main.app)
        with patch.object(search_extras, "clear_not_interested", return_value=1), patch.object(db, "clear_watch_time_cache"):
            self.assertEqual(client.delete("/api/not-interested/dQw4w9WgXcQ").status_code, 200)
        with patch.object(search_extras, "clear_not_interested", return_value=0):
            self.assertEqual(client.delete("/api/not-interested/dQw4w9WgXcQ").status_code, 404)

    def test_it_is_linked_from_settings_and_history(self) -> None:
        with offline_db():
            dialog = main._settings_dialog(None, "local")
        self.assertIn('href="/not-interested"', dialog)
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(db, "list_history_videos", return_value=[]), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: content):
            history = main.history_page(_request("/history"))
        self.assertIn('href="/not-interested"', history)
        self.assertIn("not-interested", main._YOU_TAB_ROUTES)


class FeedChromeTests(unittest.TestCase):
    def test_pull_to_refresh_reloads_the_feed_in_place(self) -> None:
        script = main._home_script()
        self.assertIn("window.ofReloadFeed = () =>", script)
        self.assertIn("await window.ofReloadFeed?.()", script)
        self.assertIn("'touchstart'", script)
        self.assertIn("querySelector('[data-view-target]')", script)
        self.assertIn("html.has-pull-refresh", main._home_styles())

    def test_back_to_top_and_the_tab_tap(self) -> None:
        script = main._home_script()
        self.assertIn("Back to top", script)
        # Tapping the tab you are on scrolls to the top on a phone (shell.js).
        self.assertIn("tab.matches('a[aria-current=\"page\"]')", script)
        self.assertIn(".back-to-top.btn", main._home_styles())


if __name__ == "__main__":
    unittest.main()
