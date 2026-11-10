"""Channel tools: remote playlists in the app, the Live tab, the Channels page's extra
sorts and bulk actions, bells for feeds without a channel, and Shorts subscribe and
Not interested. All database-free."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from openfeed import channel_tools, db, main
from openfeed.tests.sqlcapture import _capture_all_sql, _capture_sql


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {"invidious_public_url": "", "invidious_base_url": "http://inv", "enrichment_timeout_seconds": 5}
    values.update(overrides)
    return SimpleNamespace(**values)


def _item(video_id: str, index: int, **extra: Any) -> dict[str, Any]:
    return {"videoId": video_id, "title": f"T {video_id}", "author": "A", "authorId": "UCa", "index": index, "lengthSeconds": 60 * (index + 2), **extra}


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_FakeClient":
        return self

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


class RemotePlaylistIdTests(unittest.TestCase):
    def test_youtube_playlist_ids_are_recognised(self) -> None:
        for value in ("PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN", "UUsBjURrPoezykLs9EqgamOA", "OLAK5uy_kXyz12345", "PLYXhowtmyvso"):
            self.assertTrue(channel_tools.is_remote_playlist_id(value), value)

    def test_local_list_keys_are_not(self) -> None:
        for value in ("liked", "watch-later", "8a2f4c1e-0b7d-4c55-9a8e-1f2d3c4b5a69", "", "PL/../etc", "PL short", None):
            self.assertFalse(channel_tools.is_remote_playlist_id(value), value)


class RemotePlaylistFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        channel_tools._playlist_pages.clear()

    def test_a_page_keeps_positions_and_drops_entries_without_a_video(self) -> None:
        page = channel_tools.normalize_playlist_page({
            "playlistId": "PLx1234567890", "title": "Mix", "author": "Ann", "videoCount": 2, "updated": 1700000000,
            "playlistThumbnail": "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg?x=1",
            "videos": [_item("abc123def45", 0), {"title": "[Deleted video]", "index": 1}, _item("zzz123def45", 2)],
        })
        self.assertEqual([video["index"] for video in page["videos"]], [0, 2])
        self.assertEqual(page["cover_video_id"], "abc123def45")
        self.assertEqual(page["updated_at"].year, 2023)
        self.assertEqual(page["videos"][0]["channel_title"], "A")

    def test_private_and_deleted_entries_with_blank_titles_are_dropped(self) -> None:
        # Invidious lists them with a real id but an empty title and author (QA, 2026-11-09:
        # rows titled "e-hE5wEnAf8" by "Unknown channel").
        blank = {"videoId": "e-hE5wEnAf8", "title": "", "author": "", "lengthSeconds": 0, "index": 1}
        page = channel_tools.normalize_playlist_page({
            "playlistId": "PLx1234567890", "title": "P", "videos": [_item("abc123def45", 0), blank, _item("zzz123def45", 2)],
        })
        self.assertEqual([video["youtube_video_id"] for video in page["videos"]], ["abc123def45", "zzz123def45"])

    def test_rows_without_an_uploader_take_the_channel_the_library_knows(self) -> None:
        class _Rows:
            def fetchall(self):
                return [{"youtube_video_id": "abc123def45", "channel_title": "Alex Hormozi", "youtube_channel_id": "UCa"}]

        class _Conn:
            def __init__(self):
                self.params = None
            def execute(self, query, params=None):
                self.params = params
                return _Rows()

        conn = _Conn()
        videos = [{"youtube_video_id": "abc123def45", "channel_title": "", "author": ""},
                  {"youtube_video_id": "zzz123def45", "channel_title": "", "author": ""},
                  {"youtube_video_id": "kept0000000", "channel_title": "Ann", "author": "Ann"}]
        from contextlib import contextmanager

        @contextmanager
        def fake_connect():
            yield conn

        with patch.object(db, "connect", fake_connect):
            channel_tools.fill_missing_channels(videos)
        self.assertEqual([video["channel_title"] for video in videos], ["Alex Hormozi", "", "Ann"])
        self.assertEqual(videos[0]["youtube_channel_id"], "UCa")
        self.assertEqual(sorted(conn.params[0]), ["abc123def45", "zzz123def45"])

    def test_overlapping_pages_are_deduplicated_by_index(self) -> None:
        seen: set[int] = set()
        first = channel_tools.new_videos_on_page([{"index": i} for i in range(0, 200)], seen)
        second = channel_tools.new_videos_on_page([{"index": i} for i in range(50, 250)], seen)
        self.assertEqual(len(first), 200)
        self.assertEqual([video["index"] for video in second], list(range(200, 250)))

    def test_a_walk_follows_pages_until_the_count_and_stores_the_videos(self) -> None:
        pages = {
            1: {"video_count": 4, "title": "P", "videos": [{"youtube_video_id": "a", "index": 0}, {"youtube_video_id": "b", "index": 1}]},
            2: {"video_count": 4, "title": "P", "videos": [{"youtube_video_id": "b", "index": 1}, {"youtube_video_id": "c", "index": 2}, {"youtube_video_id": "d", "index": 3}]},
        }
        with patch.object(channel_tools, "fetch_invidious_playlist", side_effect=lambda pid, page: pages[page]) as fetch, \
             patch.object(channel_tools, "store_videos") as store:
            meta, videos = channel_tools.remote_playlist("PLx1234567890", 200)
        self.assertEqual([video["youtube_video_id"] for video in videos], ["a", "b", "c", "d"])
        self.assertEqual(fetch.call_count, 2)
        store.assert_called_once()
        self.assertEqual(meta["title"], "P")

    def test_the_walk_stops_at_the_limit(self) -> None:
        page = {"video_count": 500, "title": "P", "videos": [{"youtube_video_id": str(i), "index": i} for i in range(200)]}
        with patch.object(channel_tools, "fetch_invidious_playlist", return_value=page) as fetch, \
             patch.object(channel_tools, "store_videos"):
            _, videos = channel_tools.remote_playlist("PLx1234567890", 50)
        self.assertEqual(len(videos), 50)
        self.assertEqual(fetch.call_count, 1)

    def test_pages_are_fetched_once_within_the_window(self) -> None:
        client = _FakeClient([_FakeResponse({"playlistId": "PLx1234567890", "title": "P", "videos": [_item("abc123def45", 0)]})])
        with patch.object(channel_tools, "get_settings", return_value=_settings()), \
             patch.object(channel_tools.httpx, "Client", client):
            channel_tools.fetch_invidious_playlist("PLx1234567890", 1)
            channel_tools.fetch_invidious_playlist("PLx1234567890", 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0], ("http://inv/api/v1/playlists/PLx1234567890", {"page": 1}))

    def test_a_missing_playlist_is_its_own_error(self) -> None:
        client = _FakeClient([_FakeResponse({"error": "not found"}, status=404)])
        with patch.object(channel_tools, "get_settings", return_value=_settings()), \
             patch.object(channel_tools.httpx, "Client", client):
            with self.assertRaises(channel_tools.RemotePlaylistError):
                channel_tools.fetch_invidious_playlist("PLmissing12345", 1)

    def test_a_non_playlist_id_never_reaches_invidious(self) -> None:
        with self.assertRaises(channel_tools.RemotePlaylistError):
            channel_tools.fetch_invidious_playlist("../../admin", 1)

    def test_saving_copies_in_order_and_skips_nothing_it_can_store(self) -> None:
        with patch.object(db, "create_playlist", return_value={"id": "p-1", "name": "Mix"}):
            calls = _capture_all_sql(lambda: channel_tools.copy_into_new_playlist("Mix", "", ["a", "b"], "u1"))
        query, params = calls[-1]
        self.assertIn("WITH ORDINALITY", query)
        self.assertIn("ON CONFLICT (playlist_id, video_id) DO NOTHING", query)
        self.assertEqual(params, ("p-1", ["a", "b"]))


class LiveTabTests(unittest.TestCase):
    def setUp(self) -> None:
        channel_tools._stream_pages.clear()

    def test_a_row_with_no_length_that_is_not_upcoming_is_on_air(self) -> None:
        # Invidious reports liveNow false on every /streams row.
        self.assertTrue(channel_tools.is_live_now({"liveNow": False, "lengthSeconds": 0, "isUpcoming": False}))
        self.assertTrue(channel_tools.is_live_now({"liveNow": True, "lengthSeconds": 0}))
        self.assertFalse(channel_tools.is_live_now({"liveNow": False, "lengthSeconds": 3600}))
        self.assertFalse(channel_tools.is_live_now({"lengthSeconds": 0, "isUpcoming": True}))

    def test_rows_say_which_kind_they_are_and_on_air_rows_claim_no_views(self) -> None:
        live = channel_tools.normalize_stream({"videoId": "live1234567", "title": "x", "lengthSeconds": 0, "viewCount": 0}, "UCa")
        past = channel_tools.normalize_stream({"videoId": "past1234567", "title": "x", "lengthSeconds": 90, "viewCount": 10}, "UCa")
        self.assertEqual((live["stream_kind"], live["view_count"], live["youtube_channel_id"]), ("live", None, "UCa"))
        self.assertEqual((past["stream_kind"], past["view_count"]), ("past", 10))

    def test_on_air_streams_lead_and_the_continuation_is_kept(self) -> None:
        payload = {"videos": [
            {"videoId": "past1234567", "title": "old", "lengthSeconds": 300},
            {"videoId": "live1234567", "title": "now", "lengthSeconds": 0},
        ], "continuation": "tok"}
        client = _FakeClient([_FakeResponse(payload)])
        with patch.object(channel_tools, "get_settings", return_value=_settings()), \
             patch.object(channel_tools.httpx, "Client", client):
            items, token = channel_tools.fetch_channel_streams("UCa")
        self.assertEqual([item["youtube_video_id"] for item in items], ["live1234567", "past1234567"])
        self.assertEqual(token, "tok")
        self.assertEqual(client.calls[0][0], "http://inv/api/v1/channels/UCa/streams")

    def test_the_tab_shows_only_for_channels_that_have_streamed(self) -> None:
        self.assertTrue(channel_tools.channel_has_live_tab(["videos", "Streams"]))
        self.assertFalse(channel_tools.channel_has_live_tab(["videos", "shorts"]))
        self.assertFalse(channel_tools.channel_has_live_tab(None))

    def _head(self, tabs: list[str]) -> str:
        channel = {"title": "Lofi", "available_tabs": tabs}
        with patch.object(main.db, "get_channel_notification", return_value="all"):
            return main._channel_head(channel, "UCa", "live", "local")

    def test_the_live_tab_sits_after_shorts_and_is_current_on_its_page(self) -> None:
        head = self._head(["videos", "shorts", "streams", "playlists"])
        self.assertIn('href="/channel/UCa?filter=live" aria-current="page">Live</a>', head)
        self.assertLess(head.index(">Shorts<"), head.index(">Live<"))
        self.assertLess(head.index(">Live<"), head.index(">Playlists<"))
        self.assertNotIn(">Live<", self._head(["videos", "shorts", "playlists"]))

    def test_a_card_on_air_carries_a_live_badge_instead_of_a_duration(self) -> None:
        video = {"youtube_video_id": "live1234567", "title": "now", "duration_seconds": 0, "stream_kind": "live", "youtube_channel_id": "UCa"}
        card = main._home_video_card(video, 1, _settings())
        self.assertIn('class="duration is-live"', card)
        self.assertIn(">LIVE<", card)
        self.assertIn("Streaming now", card)

    def test_a_past_stream_says_streamed(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(days=3)
        parts, badge = main._stream_card_parts({"stream_kind": "past", "published_at": when}, ["5 views", main._relative_time(when)], "<b>1:00</b>")
        self.assertEqual(parts, ["5 views", f"Streamed {main._relative_time(when)}"])
        self.assertEqual(badge, "<b>1:00</b>")
        _, upcoming = main._stream_card_parts({"stream_kind": "upcoming"}, [], "")
        self.assertIn("UPCOMING", upcoming)

    def test_the_channel_page_asks_for_streams_not_stored_videos(self) -> None:
        with patch.object(channel_tools, "fetch_channel_streams", return_value=([], "")):
            html = main._channel_live_html("UCa", "local", _settings())
        self.assertIn("No live streams", html)

    def test_more_streams_come_back_rendered(self) -> None:
        item = channel_tools.normalize_stream({"videoId": "live1234567", "title": "now", "lengthSeconds": 0}, "UCa")
        client = TestClient(main.app)
        with patch.object(channel_tools, "fetch_channel_streams", return_value=([item], "next")) as fetch, \
             patch.object(channel_tools, "store_videos"), \
             patch.object(main, "_with_progress", side_effect=lambda videos, request: videos):
            body = client.get("/api/channels/UCa/streams/html?continuation=tok").json()
        fetch.assert_called_once_with("UCa", "tok")
        self.assertEqual((body["count"], body["continuation"]), (1, "next"))
        self.assertIn("is-live", body["html"])

    def test_long_continuation_tokens_reach_invidious_whole(self) -> None:
        # Big channels hand out 5,000+ character tokens; a cut one is a 400 upstream (QA,
        # 2026-11-09: Show more on penguinz0's Live tab returned 502).
        token = "4qmF" + "x" * 5500 + "%3D%3D"
        client = TestClient(main.app)
        with patch.object(channel_tools, "fetch_channel_streams", return_value=([], "")) as fetch, \
             patch.object(channel_tools, "store_videos"), \
             patch.object(main, "_with_progress", side_effect=lambda videos, request: videos):
            response = client.get("/api/channels/UCa/streams/html", params={"continuation": token})
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with("UCa", token)


class RemotePlaylistPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.meta = {"title": "Artificial Intelligence", "author": "Fireship", "video_count": 2, "view_count": 14841,
                     "updated_at": None, "cover_video_id": "abc123def45", "description": ""}
        self.videos = [
            {"youtube_video_id": "abc123def45", "title": "One", "channel_title": "Fireship", "index": 0, "duration_seconds": 300},
            {"youtube_video_id": "zzz123def45", "title": "Two", "channel_title": "Fireship", "index": 1, "duration_seconds": 200},
        ]

    def _page(self, path: str) -> Any:
        with patch.object(channel_tools, "remote_playlist", return_value=(self.meta, self.videos)), \
             patch.object(main, "_with_progress", side_effect=lambda videos, request: videos), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **kw: content):
            return self.client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)

    def test_the_channel_tab_opens_playlists_inside_the_app(self) -> None:
        row = main._remote_playlist_row({"playlist_id": "PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN", "title": "AI", "author": "Fireship",
                                         "video_count": 90, "thumbnail_url": "https://i.ytimg.com/vi/5D4Zqp9GLSc/hqdefault.jpg?x"})
        self.assertIn('href="/playlist?list=PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN"', row)
        self.assertNotIn("target=", row)
        self.assertNotIn("i.ytimg.com", row)
        self.assertIn("/vi/5D4Zqp9GLSc/hqdefault.jpg", row)

    def test_the_page_lists_the_videos_with_play_all_shuffle_and_save(self) -> None:
        response = self._page("/playlist?list=PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("Artificial Intelligence", html)
        self.assertIn('href="/watch/abc123def45?list=PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN" data-list-play', html)
        self.assertIn("data-list-shuffle", html)
        self.assertIn('data-save-remote-playlist="PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN"', html)
        self.assertIn("Save to library", html)
        self.assertIn("Two", html)

    def test_a_link_that_is_not_a_playlist_is_a_404(self) -> None:
        self.assertEqual(self._page("/playlist?list=nonsense").status_code, 404)

    def test_a_local_playlist_id_goes_to_its_own_page(self) -> None:
        response = self._page("/playlist?list=8a2f4c1e-0b7d-4c55-9a8e-1f2d3c4b5a69")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/playlist/8a2f4c1e-0b7d-4c55-9a8e-1f2d3c4b5a69")

    def test_list_mode_plays_through_a_remote_playlist(self) -> None:
        with patch.object(channel_tools, "remote_playlist", return_value=(self.meta, self.videos)):
            resolved = main._resolve_watch_list("PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN", "local")
        self.assertEqual(resolved["href"], "/playlist?list=PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN")
        panel = main._watch_list_panel(resolved, "abc123def45")
        self.assertIn("1 of 2", panel)
        self.assertIn("/watch/zzz123def45?list=PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN", panel)

    def test_save_copies_into_a_new_local_playlist(self) -> None:
        with patch.object(channel_tools, "remote_playlist", return_value=(self.meta, self.videos)), \
             patch.object(channel_tools, "copy_into_new_playlist", return_value={"id": "p-1", "name": "Artificial Intelligence", "added": 2}) as copy, \
             patch.object(main, "_user_key", return_value="owner-1"):
            body = self.client.post("/api/remote-playlists/PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN/save").json()
        copy.assert_called_once_with("Artificial Intelligence", "Saved from Fireship on YouTube.", ["abc123def45", "zzz123def45"], "owner-1")
        self.assertEqual((body["href"], body["added"]), ("/playlist/p-1", 2))

    def test_more_rows_come_back_rendered(self) -> None:
        with patch.object(channel_tools, "remote_playlist_slice", return_value=(self.videos[1:], False)) as slice_, \
             patch.object(main, "_with_progress", side_effect=lambda videos, request: videos):
            body = self.client.get("/api/remote-playlists/PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN/rows?offset=200").json()
        slice_.assert_called_once_with("PL0vfts4VzfNgNVZIPX7VIS4w2dhuwCXbN", 200, main.REMOTE_PLAYLIST_MORE_PAGE)
        self.assertEqual((body["next_offset"], body["more"]), (201, False))
        self.assertIn("Two", body["html"])


class ChannelSortTests(unittest.TestCase):
    def setUp(self) -> None:
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.items = [{"id": "feed/1", "title": "Beta"}, {"id": "feed/2", "title": "alpha"}, {"id": "feed/3", "title": "Gamma"}]
        self.stats = {
            "feed/1": {"subscribed_at": old, "watch_score": 3.0, "unwatched_new": 0},
            "feed/2": {"subscribed_at": old, "watch_score": 0.0, "unwatched_new": 5},
            "feed/3": {"subscribed_at": new, "watch_score": 0.5, "unwatched_new": 5},
        }

    def titles(self, key: str) -> list[str]:
        return [item["title"] for item in channel_tools.sort_subscriptions(self.items, key, self.stats)]

    def test_recently_subscribed_puts_the_newest_first(self) -> None:
        self.assertEqual(self.titles("recent"), ["Gamma", "alpha", "Beta"])

    def test_most_relevant_follows_watch_activity(self) -> None:
        self.assertEqual(self.titles("relevant"), ["Beta", "Gamma", "alpha"])

    def test_new_activity_ties_fall_back_to_the_name_and_rows_say_how_many(self) -> None:
        rows = channel_tools.sort_subscriptions(self.items, "activity", self.stats)
        self.assertEqual([row["title"] for row in rows], ["alpha", "Gamma", "Beta"])
        self.assertEqual(rows[0]["sort_note"], "5 new")
        self.assertNotIn("sort_note", rows[2])
        self.assertNotIn("sort_note", self.items[1])  # the input rows are not changed

    def test_the_page_offers_them_next_to_the_old_sorts(self) -> None:
        keys = [key for key, _ in main._CHANNEL_SORTS]
        self.assertEqual(keys, ["name", "latest", "videos", "recent", "relevant", "activity"])
        self.assertIn(("activity", "New activity"), main._CHANNEL_SORTS)

    def test_the_stats_query_binds_the_user_and_reads_watch_and_new_uploads(self) -> None:
        query, params = _capture_sql(lambda: channel_tools.subscription_stats("u1"))
        self.assertEqual(params, ("u1", "u1", "u1", "u1"))
        # "Recently subscribed" is when this user subscribed, and only their feeds count.
        self.assertIn("us.created_at AS subscribed_at", query)
        self.assertIn("JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = %s", query)
        self.assertIn("FROM playback_progress pp", query)
        self.assertIn("pp.id IS NULL", query)
        self.assertNotIn("u1", query)


class BulkActionTests(unittest.TestCase):
    rows = [
        {"id": "feed/1", "title": "A", "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCa", "youtube_channel_id": "UCa"},
        {"id": "feed/2", "title": "B", "feed_url": "https://example.com/rss", "youtube_channel_id": None},
    ]

    def test_unknown_actions_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            channel_tools.apply_bulk_action("delete-everything", ["feed/1"])

    def test_unsubscribe_removes_only_the_subscription_rows(self) -> None:
        with patch.object(channel_tools, "subscriptions_by_ids", return_value=self.rows), \
             patch.object(channel_tools, "delete_subscriptions", return_value=self.rows[:1]) as delete:
            result = channel_tools.apply_bulk_action("unsubscribe", ["feed/1", "feed/2"], "u1")
        delete.assert_called_once_with(["feed/1", "feed/2"], "u1")
        self.assertEqual(result["applied"], ["feed/1"])

    def test_the_delete_is_a_bound_list_of_this_users_follows(self) -> None:
        calls = _capture_all_sql(lambda: channel_tools.delete_subscriptions(["feed/1", "x'; drop table videos; --"], "u1"))
        query, params = calls[0]
        # Only the user's own follow goes; the shared feed row is never deleted.
        self.assertIn("DELETE FROM user_subscriptions us", query)
        self.assertIn("us.user_key = %s AND us.feed_id = ANY(%s)", query)
        self.assertNotIn("DELETE FROM feeds", query)
        self.assertEqual(params, ("u1", ["feed/1", "x'; drop table videos; --"]))

    def test_the_bulk_lookup_only_sees_this_users_feeds(self) -> None:
        query, params = _capture_sql(lambda: channel_tools.subscriptions_by_ids(["feed/1"], "u2"))
        self.assertIn("JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = %s", query)
        self.assertEqual(params, ("u2", ["feed/1"]))

    def test_notifications_key_a_feed_without_a_channel_by_its_own_key(self) -> None:
        with patch.object(channel_tools, "subscriptions_by_ids", return_value=self.rows), \
             patch.object(db, "set_channel_notification") as set_level:
            channel_tools.apply_bulk_action("notify", ["feed/1", "feed/2"], "u1", level="none")
        keys = [call.args[0] for call in set_level.call_args_list]
        self.assertEqual(keys, ["UCa", channel_tools.feed_notification_key("feed/2")])
        with self.assertRaises(ValueError):
            channel_tools.apply_bulk_action("notify", ["feed/1"], level="loud")

    def test_mute_skips_feeds_without_a_channel(self) -> None:
        with patch.object(channel_tools, "subscriptions_by_ids", return_value=self.rows), \
             patch.object(db, "set_channel_muted") as mute:
            result = channel_tools.apply_bulk_action("mute", ["feed/1", "feed/2"], "u1")
        mute.assert_called_once_with("UCa", True, "u1")
        self.assertEqual((result["applied"], result["skipped"]), (["feed/1"], ["feed/2"]))

    def test_a_collection_that_is_not_yours_is_not_found(self) -> None:
        with patch.object(channel_tools, "subscriptions_by_ids", return_value=self.rows), \
             patch.object(db, "add_subscription_to_collection", return_value=None):
            with self.assertRaises(LookupError):
                channel_tools.apply_bulk_action("collection", ["feed/1"], "u1", collection_id="c1")

    def test_the_endpoint_checks_the_collection_id_and_passes_the_user(self) -> None:
        client = TestClient(main.app)
        bad = client.post("/api/subscriptions/bulk", json={"action": "collection", "subscription_ids": ["feed/1"], "collection_id": "None"})
        self.assertEqual(bad.status_code, 404)
        with patch.object(channel_tools, "apply_bulk_action", return_value={"applied": ["feed/1"], "skipped": []}) as apply, \
             patch.object(main, "_user_key", return_value="owner-1"):
            body = client.post("/api/subscriptions/bulk", json={"action": "NOTIFY", "subscription_ids": ["feed/1"], "level": "none"}).json()
        apply.assert_called_once_with("notify", ["feed/1"], "owner-1", collection_id="", level="none")
        self.assertEqual(body["applied"], ["feed/1"])
        self.assertEqual(client.post("/api/subscriptions/bulk", json={"action": "nuke"}).status_code, 400)

    def test_the_select_bar_offers_every_action_and_each_collection(self) -> None:
        bar = main._channels_select_bar([{"id": "c-1", "name": "Tech"}])
        for needle in ('data-bulk-all', 'data-bulk-action="unsubscribe"', 'data-bulk-action="mute"',
                       'data-bulk-action="notify" data-level="all"', 'data-bulk-action="notify" data-level="none"',
                       'data-bulk-action="collection" data-collection-id="c-1"', "Tech", "data-bulk-done", 'role="toolbar"'):
            self.assertIn(needle, bar)
        self.assertIn(" hidden ", bar)


class FeedBellTests(unittest.TestCase):
    def test_a_feed_key_is_stable_and_fits_one_path_segment(self) -> None:
        key = channel_tools.feed_notification_key("feed/4")
        self.assertEqual(key, channel_tools.feed_notification_key("feed/4"))
        self.assertNotEqual(key, channel_tools.feed_notification_key("feed/5"))
        self.assertNotIn("/", key)
        self.assertTrue(channel_tools.is_feed_key(key))

    def test_channels_keep_their_own_key(self) -> None:
        self.assertEqual(channel_tools.notification_key({"id": "feed/1", "youtube_channel_id": "UCa"}), "UCa")

    def test_a_feed_that_delivers_videos_gets_a_bell_and_one_that_never_has_says_why(self) -> None:
        rows = main._subscriptions_with_bell([
            {"id": "feed/1", "title": "Playlist feed", "youtube_channel_id": None, "video_count": 3, "feed_url": "https://x"},
            {"id": "feed/2", "title": "Release notes", "youtube_channel_id": None, "video_count": 0, "feed_url": "https://y"},
            {"id": "feed/3", "title": "Channel", "youtube_channel_id": "UCa", "video_count": 0, "feed_url": "https://z"},
        ], {channel_tools.feed_notification_key("feed/1"): "none"})
        self.assertEqual([row["notify_level"] for row in rows], ["none", None, "all"])
        card = main._subscription_card(rows[0], notify_level=rows[0]["notify_level"])
        self.assertIn(f'data-channel-id="{channel_tools.feed_notification_key("feed/1")}"', card)
        self.assertIn("data-notify-menu", card)
        quiet = main._subscription_card(rows[1], notify_level=rows[1]["notify_level"])
        self.assertNotIn("data-notify-menu", quiet)
        self.assertIn("No YouTube videos in this feed", quiet)
        self.assertNotIn("No dated videos yet", quiet)

    def test_feed_uploads_are_read_through_the_feed_entries(self) -> None:
        query, params = _capture_sql(lambda: channel_tools.list_feed_uploads(10, "u1", include_shorts=False))
        self.assertIn("JOIN feed_entries fe ON fe.subscription_id = fs.id", query)
        self.assertIn("JOIN user_subscriptions us ON us.feed_id = fs.id AND us.user_key = %s", query)
        self.assertIn("fs.youtube_channel_id IS NULL", query)
        self.assertIn(db.SHORT_VIDEO_SQL, query)
        self.assertEqual(params, ("u1", "u1", 10))

    def test_the_notification_panel_honours_a_feed_bell(self) -> None:
        now = datetime.now(timezone.utc)
        feed_key = channel_tools.feed_notification_key("feed/9")
        channel_row = {"youtube_video_id": "c1", "title": "Channel video", "youtube_channel_id": "UCa", "published_at": now - timedelta(hours=2)}
        feed_rows = [
            {"youtube_video_id": "f1", "title": "Feed video", "youtube_channel_id": "UCz", "published_at": now - timedelta(hours=1), "notify_key": feed_key},
        ]
        with patch.object(db, "get_feed_preferences", return_value={}), \
             patch.object(db, "list_subscription_uploads", return_value=[channel_row]), \
             patch.object(channel_tools, "list_feed_uploads", return_value=feed_rows), \
             patch.object(db, "get_notifications_seen_at", return_value=None):
            with patch.object(db, "channel_notification_levels", return_value={}):
                items, _ = main._notification_items("u1")
            self.assertEqual([item["youtube_video_id"] for item in items], ["f1", "c1"])
            with patch.object(db, "channel_notification_levels", return_value={feed_key: "none"}):
                items, _ = main._notification_items("u1")
            self.assertEqual([item["youtube_video_id"] for item in items], ["c1"])


class ShortsTests(unittest.TestCase):
    def _slide(self, **extra: Any) -> str:
        video = {"youtube_video_id": "short123456", "title": "A short", "channel_title": "Chan", "youtube_channel_id": "UCa", **extra}
        return main._shorts_slide(video)

    def test_a_short_offers_subscribe_beside_the_channel(self) -> None:
        slide = self._slide(channel_subscribed=False)
        self.assertIn('class="shorts-subscribe" type="button" data-subscribe-feed', slide)
        self.assertIn('aria-label="Subscribe to Chan"', slide)
        self.assertLess(slide.index("shorts-channel"), slide.index("shorts-subscribe"))

    def test_a_subscribed_channel_shows_subscribed_and_unsubscribes(self) -> None:
        slide = self._slide(channel_subscribed=True)
        self.assertIn("shorts-subscribe subscribed", slide)
        self.assertIn("data-unsubscribe-feed", slide)
        self.assertIn(">Subscribed<", slide)

    def test_a_short_without_a_channel_has_no_pill(self) -> None:
        self.assertNotIn("shorts-subscribe", self._slide(youtube_channel_id=""))

    def test_the_player_menu_offers_not_interested(self) -> None:
        slide = self._slide()
        self.assertIn('data-shorts-not-interested data-video-id="short123456"', slide)
        self.assertIn('aria-label="More actions for A short"', slide)

    def test_subscription_state_is_read_once_for_a_page(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[{"youtube_channel_id": "UCA"}]) as listing:
            videos = main._with_subscription_state([{"youtube_channel_id": "UCa"}, {"youtube_channel_id": "UCb"}], "u1")
        listing.assert_called_once()
        self.assertEqual(listing.call_args.kwargs["user_key"], "u1")
        self.assertEqual([video["channel_subscribed"] for video in videos], [True, False])

    def test_the_home_shelf_card_has_a_menu_with_not_interested(self) -> None:
        card = main._short_card({"youtube_video_id": "short123456", "title": "A short", "channel_title": "Chan"}, 1)
        # The shared Not interested handler hides [data-video-card][data-video-id] and offers Undo.
        self.assertIn('data-video-card data-video-id="short123456"', card)
        self.assertIn('data-feedback="hide" data-video-id="short123456"', card)
        self.assertIn("Not interested", card)

    def test_the_script_and_styles_ship_in_the_bundle(self) -> None:
        script = main._home_script()
        for needle in ("data-shorts-not-interested", "data-save-remote-playlist", "/api/subscriptions/bulk", "undo_hide"):
            self.assertIn(needle, script)
        self.assertIn(".duration.is-live", main._home_styles())


class PlaylistFeedEntryTests(unittest.TestCase):
    """A feed with no channel of its own keeps its videos, so its bell has something to count."""

    def _run(self, channel_subscription: Any) -> Any:
        import contextlib

        from openfeed import rss
        from openfeed.tests import test_rss_poller as harness

        due = [{"id": "feed/pl", "youtube_channel_id": None, "feed_url": harness.FEED_URL, "etag": None, "last_modified": None}]
        mapping = {harness.FEED_URL: harness.ChannelFeedResult(status=200, text=harness.FEED_XML, etag=None, last_modified=None)}
        case = harness.SyncRssOnceTests()
        with contextlib.ExitStack() as stack:
            mocks = case._patched_db(stack, due)
            mocks[2].return_value = channel_subscription
            case._patched_fetch(stack, mapping)
            rss.sync_rss_once(channel_limit=5)
        return mocks[7]

    def test_entries_from_unsubscribed_channels_belong_to_the_feed(self) -> None:
        upsert_entry = self._run(None)
        self.assertEqual({call.args[2] for call in upsert_entry.call_args_list}, {"feed/pl"})

    def test_a_subscribed_uploader_keeps_its_own_entries(self) -> None:
        upsert_entry = self._run("feed/9")
        self.assertEqual({call.args[2] for call in upsert_entry.call_args_list}, {"feed/9"})


if __name__ == "__main__":
    unittest.main()
