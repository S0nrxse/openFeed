"""Server-side defects from the round-2 QA sweep. Each test names the failure it stops."""

from __future__ import annotations

import unittest
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from openfeed import db, discovery, main

APP = Path(main.__file__).parent
WATCH_MEDIA = (APP / "assets" / "js" / "watch-media.js").read_text(encoding="utf-8")


def _request(path: str = "/", query: str = "", accept: str = "text/html") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(),
                    "headers": [(b"accept", accept.encode())], "scheme": "http", "server": ("testserver", 80)})


def _video(video_id: str, channel: str = "UCa", title: str = "", duration: int = 600) -> dict:
    return {"youtube_video_id": video_id, "youtube_channel_id": channel, "title": title or f"Video {video_id}",
            "channel_title": channel, "duration_seconds": duration, "source_kind": "discovery"}


class CollectionIdValidationTests(unittest.TestCase):
    """POST /api/subscription-collections/None/members answered 500 instead of 404."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_a_malformed_collection_id_is_a_404_on_every_member_route(self) -> None:
        with patch.object(db, "add_subscription_to_collection", side_effect=AssertionError("must not query")), \
             patch.object(db, "remove_subscription_from_collection", side_effect=AssertionError("must not query")), \
             patch.object(db, "list_collection_subscriptions", side_effect=AssertionError("must not query")), \
             patch.object(db, "list_collection_videos", side_effect=AssertionError("must not query")):
            calls = (
                ("POST", "/api/subscription-collections/None/members", {"subscription_id": "feed/41"}),
                ("DELETE", "/api/subscription-collections/None/members", {"subscription_id": "feed/41"}),
                ("DELETE", "/api/subscription-collections/None/members/feed/41", None),
                ("GET", "/api/subscription-collections/None/subscriptions", None),
                ("GET", "/api/subscription-collections/None/videos", None),
            )
            for method, url, body in calls:
                with self.subTest(method=method, url=url):
                    response = self.client.request(method, url, json=body)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"], "Collection not found")


class PagedDiversityTests(unittest.TestCase):
    """Home's Recent chip served 300 rows over pages 0-7 with only 197 distinct videos."""

    def _rows(self) -> list[dict]:
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
        return [
            _video(f"v{i:03d}", channel=f"UC{i % 9}", title=f"{words[i % 8]} {words[(i * 3) % 8]} clip {i}")
            for i in range(400)
        ]

    def test_consecutive_pages_never_repeat_a_video(self) -> None:
        rows = self._rows()
        seen: list[str] = []
        for page in range(8):
            ids = [row["youtube_video_id"] for row in db._paged_diversify(rows, 30, page * 30)]
            self.assertEqual(len(ids), 30)
            self.assertFalse(set(ids) & set(seen), f"page {page} repeats an earlier page")
            seen += ids

    def test_page_zero_is_what_it_always_was(self) -> None:
        rows = self._rows()
        self.assertEqual(db._paged_diversify(rows, 30, 0), db._diversify_recommendations(rows, 30))

    def test_the_candidate_pool_does_not_change_between_pages(self) -> None:
        source = (APP / "db.py").read_text(encoding="utf-8")
        self.assertIn("candidate_limit = 2000 if requested + skip <= 1500 else 5000", source)
        self.assertNotIn("candidate_limit = max(min((requested + skip) * 12, 5000), 2000)", source)


class ChannelSearchTests(unittest.TestCase):
    def test_an_empty_query_is_dropped_from_the_url(self) -> None:
        with patch.object(main, "_login_required_redirect", return_value=None):
            response = main.channel_page(_request("/channel/UCx", "q=&sort=popular"), "UCx", q="", sort="popular")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/channel/UCx?sort=popular")
        with patch.object(main, "_login_required_redirect", return_value=None):
            bare = main.channel_page(_request("/channel/UCx", "q=%20"), "UCx", q=" ")
        self.assertEqual(bare.headers["location"], "/channel/UCx")

    def test_enter_in_an_empty_box_does_not_submit(self) -> None:
        script = main._home_script()
        self.assertIn("const form = event.target.closest?.('[data-ch-search]');", script)
        self.assertIn("if (new URLSearchParams(location.search).has('q'))", script)


class ShortsOffTests(unittest.TestCase):
    """Show Shorts off said "hides them everywhere" while /shorts kept playing them."""

    def _patches(self, preferences: dict):
        return (
            patch.object(main, "_login_required_redirect", return_value=None),
            patch.object(main, "_user_key", return_value="local"),
            patch.object(db, "get_feed_preferences", return_value=preferences),
            patch.object(db, "list_short_videos", side_effect=AssertionError("must not list Shorts")),
            patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: f"<shell>{content}</shell>"),
        )

    def test_the_viewer_says_shorts_are_off_and_links_to_the_setting(self) -> None:
        a, b, c, d, e = self._patches({"exclude_shorts": True})
        with a, b, c, d, e:
            html = main.shorts_page(_request("/shorts"))
        self.assertIn("Shorts are off", html)
        self.assertIn('href="/shorts?settings=content"', html)
        self.assertNotIn("shorts-slide", html)

    def test_the_api_returns_nothing_when_off_or_on_a_kids_profile(self) -> None:
        for preferences in ({"exclude_shorts": True}, {"exclude_shorts": True, "kids_profile": True}):
            a, b, c, d, e = self._patches(preferences)
            with self.subTest(preferences=preferences), a, b, c, d:
                self.assertEqual(main.api_shorts(_request("/api/shorts"))["count"], 0)

    def test_the_channel_shorts_tab_honours_it_too(self) -> None:
        self.assertIn('elif kind == "shorts" and _shorts_hidden(_user_key(request)):', (APP / "main.py").read_text(encoding="utf-8"))


class ShortsCommentTests(unittest.TestCase):
    """Top comments 502 upstream for Shorts; newest-first parses, so the panel uses it."""

    def _call(self, statuses: list[int]) -> tuple[dict | None, list, HTTPException | None]:
        asked: list = []
        payload = {"commentCount": 4, "comments": [{"author": "@a", "content": "hi", "authorThumbnails": []}]}

        class _Response:
            def __init__(self, status: int) -> None:
                self.status_code = status

            def json(self) -> dict:
                return payload

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def get(self, url, params=None):
                asked.append(params)
                return _Response(statuses[len(asked) - 1])

        settings = SimpleNamespace(invidious_base_url="http://invidious", enrichment_timeout_seconds=5)
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="local"), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(db, "get_feed_preferences", return_value={"show_comments": True}), \
             patch.object(db, "comment_reactions", return_value={}), \
             patch.object(main.httpx, "Client", lambda *a, **k: _Client()):
            try:
                return main.api_comments(_request(), "hhqCfQi41O4"), asked, None
            except HTTPException as exc:
                return None, asked, exc

    def test_a_failed_top_sort_falls_back_to_newest_and_says_so(self) -> None:
        data, asked, error = self._call([500, 200])
        self.assertIsNone(error)
        self.assertEqual(asked, [None, {"sort_by": "new"}])
        self.assertEqual(data["sort"], "new")
        self.assertIn("newest", data["notice"])
        self.assertEqual(len(data["items"]), 1)

    def test_a_working_top_sort_has_no_notice(self) -> None:
        data, asked, _ = self._call([200])
        self.assertEqual((data["sort"], data["notice"], len(asked)), ("top", "", 1))

    def test_both_failing_is_still_a_clear_502(self) -> None:
        _, asked, error = self._call([500, 500])
        self.assertEqual(len(asked), 2)
        self.assertEqual((error.status_code, error.detail), (502, "Comments are unavailable for this video"))

    def test_the_panels_show_the_notice_and_the_enable_button_names_its_scope(self) -> None:
        script = main._home_script()
        self.assertGreaterEqual(script.count("notice.dataset.commentsNotice = '';"), 2)
        self.assertIn("Turning them on shows comments everywhere: on Shorts and on watch pages.", script)
        self.assertIn("body: JSON.stringify({ show_comments: true })", script)
        self.assertIn("if (panel.dataset.commentsEnabled === '0') { showEnableComments(videoId); return; }", script)


class ChannelUploadPagingTests(unittest.TestCase):
    """Invidious ignores ?page=, so pages 2-6 were page 1 again, each rewritten row by row."""

    def setUp(self) -> None:
        discovery._upload_pages.clear()
        self.addCleanup(discovery._upload_pages.clear)

    def _client(self, pages: dict[str, dict], asked: list):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def get(self, url, params=None):
                asked.append(dict(params or {}))
                return _Response(pages[(params or {}).get("continuation", "")])

        return lambda *a, **k: _Client()

    def _pages(self) -> dict[str, dict]:
        item = lambda vid: {"videoId": vid, "title": vid, "author": "Chan", "authorId": "UCx", "lengthSeconds": 600}  # noqa: E731
        return {
            "": {"videos": [item("a1"), item("a2")], "continuation": "TOKEN2"},
            "TOKEN2": {"videos": [item("b1"), item("b2")], "continuation": "TOKEN3"},
            "TOKEN3": {"videos": [item("c1")], "continuation": ""},
        }

    def test_pages_follow_the_continuation_and_are_cached(self) -> None:
        asked: list = []
        settings = SimpleNamespace(invidious_base_url="http://inv", enrichment_timeout_seconds=5)
        with patch.object(discovery, "get_settings", return_value=settings), \
             patch.object(discovery.httpx, "Client", self._client(self._pages(), asked)):
            page2 = discovery.fetch_channel_uploads("UCx", 2)
            again = discovery.fetch_channel_uploads("UCx", 1)
            page3 = discovery.fetch_channel_uploads("UCx", 3)
            past_end = discovery.fetch_channel_uploads("UCx", 4)
        self.assertEqual([v["youtube_video_id"] for v in page2], ["b1", "b2"])
        self.assertEqual([v["youtube_video_id"] for v in again], ["a1", "a2"])
        self.assertEqual([v["youtube_video_id"] for v in page3], ["c1"])
        self.assertEqual(past_end, [])
        # Page 1, page 2 (via its token), then page 3 from the cached page-2 token: three requests.
        self.assertEqual([p.get("continuation", "") for p in asked], ["", "TOKEN2", "TOKEN3"])

    def test_ingest_writes_only_new_videos_in_one_batch_and_stops_when_covered(self) -> None:
        pages = {1: [_video("a1"), _video("a2")], 2: [_video("b1"), _video("b2")], 3: [_video("c1")]}
        with patch.object(discovery, "fetch_channel_uploads", side_effect=lambda cid, page, sort_by="newest": pages.get(page, [])) as fetch, \
             patch.object(db, "video_ids_in_library", side_effect=lambda ids: {"a1", "a2", "b1"} & set(ids)), \
             patch.object(db, "upsert_discovered_videos", side_effect=lambda items, source: len(items)) as upsert, \
             patch.object(db, "upsert_discovered_video", side_effect=AssertionError("per-row writes are gone")):
            stored = main._ingest_channel_uploads("UCx", needed=4)
        self.assertEqual(stored, 1)
        self.assertEqual(fetch.call_count, 2)
        upsert.assert_called_once()
        self.assertEqual([v["youtube_video_id"] for v in upsert.call_args.args[0]], ["b2"])


class ListPlaybackTests(unittest.TestCase):
    """Play all opened only the first video; the next one was YouTube's Up next."""

    VIDEOS = [_video("aaaaaaaaaa1"), _video("aaaaaaaaaa2"), _video("aaaaaaaaaa3")]

    def test_play_all_and_shuffle_open_the_watch_page_in_list_mode(self) -> None:
        html = main._play_all_buttons(self.VIDEOS, "liked")
        self.assertIn(f'href="{escape("/watch/aaaaaaaaaa1?list=liked", quote=True)}" data-list-play', html)
        self.assertIn("data-list-shuffle", html)
        self.assertIn(escape("&shuffle=", quote=True), html)
        self.assertNotIn("data-list-shuffle", main._play_all_buttons(self.VIDEOS[:1], "liked"))

    def test_the_panel_marks_the_current_video_and_tags_the_next(self) -> None:
        watch_list = {"key": "liked", "title": "Liked videos", "href": "/liked", "videos": self.VIDEOS}
        html = main._watch_list_panel(watch_list, "aaaaaaaaaa2")
        self.assertIn("2 of 3", html)
        current = html.index('aria-current="true"')
        self.assertLess(html.index("Video aaaaaaaaaa1"), current)
        nxt = html.index("data-list-next")
        self.assertIn(escape("/watch/aaaaaaaaaa3?list=liked", quote=True), html[nxt:nxt + 200])
        last = main._watch_list_panel(watch_list, "aaaaaaaaaa3")
        self.assertIn("3 of 3", last)
        self.assertNotIn("data-list-next", last)
        self.assertEqual(main._watch_list_panel(watch_list, "notinlist00"), "")

    def test_a_shuffle_seed_gives_the_same_order_on_every_page(self) -> None:
        many = [_video(f"vid{i:08d}") for i in range(20)]
        first = [v["youtube_video_id"] for v in main._list_order(many, "abc123")]
        self.assertEqual(first, [v["youtube_video_id"] for v in main._list_order(many, "abc123")])
        self.assertNotEqual(first, [v["youtube_video_id"] for v in many])
        panel = main._watch_list_panel({"key": "liked", "title": "Liked", "href": "/liked", "videos": many}, first[0], "abc123")
        self.assertIn("Shuffled · 1 of 20", panel)
        self.assertIn(escape(f"/watch/{first[1]}?list=liked&shuffle=abc123", quote=True), panel)

    def test_only_the_users_own_lists_resolve(self) -> None:
        with patch.object(db, "get_playlist", return_value=None) as get_playlist:
            self.assertIsNone(main._resolve_watch_list("00000000-0000-0000-0000-000000000000", "local"))
            self.assertIsNone(main._resolve_watch_list("not-a-uuid", "local"))
            self.assertIsNone(main._resolve_watch_list("", "local"))
        get_playlist.assert_called_once_with("00000000-0000-0000-0000-000000000000", "local")
        with patch.object(db, "list_playlists", return_value=[{"id": "11111111-1111-1111-1111-111111111111", "name": "Watch later"}]), \
             patch.object(db, "get_playlist", return_value={"name": "Watch later"}), \
             patch.object(db, "list_playlist_videos", return_value=self.VIDEOS):
            resolved = main._resolve_watch_list("watch-later", "local")
        self.assertEqual((resolved["title"], resolved["href"]), ("Watch later", "/playlist/11111111-1111-1111-1111-111111111111"))

    def test_the_end_of_a_video_moves_to_the_next_list_item(self) -> None:
        script = main._home_script()
        block = script[script.index("/* ---- list playback (Play all / Shuffle) ---- */"):]
        self.assertIn("const next = list.querySelector('[data-list-next] a[href^=\"/watch/\"]');", block)
        self.assertIn("if (data.type !== 'openfeed:clock') return;", block)
        self.assertIn("if (!played || !duration || Number(data.seconds) < duration - 0.75) return;", block)
        # Media Session "next" (headphones, lock screen) follows the list too.
        self.assertIn("document.querySelector('[data-watch-list] [data-list-next]')", WATCH_MEDIA)


class WhyThisVideoTests(unittest.TestCase):
    def test_the_menu_carries_the_reason_when_there_is_one(self) -> None:
        menu = main._video_action_menu("vid", "Title", "/watch/vid", "https://youtube.com/watch?v=vid", reason="Because you follow Astra")
        self.assertIn("Why this video", menu)
        self.assertIn('data-why-video="Because you follow Astra"', menu)
        self.assertIn('<span class="menu-sub">Because you follow Astra</span>', menu)
        self.assertNotIn("Why this video", main._video_action_menu("vid", "Title", "/watch/vid", "https://youtube.com/watch?v=vid"))

    def test_cards_pass_their_reason_to_the_menu(self) -> None:
        video = {**_video("vid00000001"), "recommendation_reason": "Similar to Astra"}
        settings = SimpleNamespace(invidious_public_url="")
        with patch.object(db, "is_short_video", return_value=False):
            self.assertIn('data-why-video="Similar to Astra"', main._home_video_card(video, 1, settings))
            self.assertIn('data-why-video="Similar to Astra"', main._media_row(video, settings))


class SearchFeatureNoteTests(unittest.TestCase):
    def test_a_feature_filter_says_the_library_is_left_out(self) -> None:
        settings = SimpleNamespace(invidious_public_url="")
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(main, "_search_youtube_videos", return_value=([], 0)), \
             patch.object(db, "search_videos", side_effect=AssertionError("library is skipped under a feature filter")), \
             patch.object(main, "_search_suggestions", return_value=[]), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: content):
            html = main.search_page(_request("/search"), q="python", features="hd")
        self.assertIn("data-feature-library-note", html)
        self.assertIn("HD filter", html)


if __name__ == "__main__":
    unittest.main()
