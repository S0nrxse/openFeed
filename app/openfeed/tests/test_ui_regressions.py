"""Defects found by the whole-build QA pass. Each test names the failure it stops."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from openfeed import db, main

APP = Path(main.__file__).parent
ASSETS = APP / "assets"
SOURCE = (APP / "main.py").read_text(encoding="utf-8")


def _request(accept: str) -> Request:
    headers = [(b"accept", accept.encode())] if accept else []
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": headers,
                    "scheme": "http", "server": ("testserver", 80)})


class HomeFilterLoaderTests(unittest.TestCase):
    """The skeleton pulled the sentinel into view, so one click sent two page-0 requests."""

    def setUp(self) -> None:
        self.script = main._home_script()

    def test_a_scroll_load_never_starts_over_a_load_in_flight(self) -> None:
        guard = self.script.index("if (feedLoading && !options.reset) return;")
        self.assertLess(guard, self.script.index("const generation = ++feedGeneration;"))

    def test_a_reset_cancels_the_request_it_replaces(self) -> None:
        self.assertIn("feedAbort?.abort();", self.script)
        self.assertIn("{ signal: controller.signal }", self.script)

    def test_a_failed_reset_retries_as_a_reset(self) -> None:
        # Retrying as an append would put the page under the error message.
        self.assertIn("loadFeedPage({ reset: feedContainer?.dataset.nextPage === '0' })", self.script)


class PageNotFoundTests(unittest.TestCase):
    """A browser gets the app shell, anything else keeps the JSON it always had."""

    ROUTES = (
        ("Video not found", lambda request: main.watch_video(request, "aaaaaaaaaaa")),
        ("Channel not found", lambda request: main.channel_page(request, "UCmissing")),
        ("Channel not found", lambda request: main.channel_posts_page(request, "UCmissing")),
        ("Playlist not found", lambda request: main.playlist_page(request, "not-a-uuid")),
        ("Playlist not found", lambda request: main.playlist_page(request, "00000000-0000-0000-0000-000000000000")),
        ("Collection not found", lambda request: main.collection_page(request, "not-a-uuid")),
        ("Collection not found", lambda request: main.collection_page(request, "00000000-0000-0000-0000-000000000000")),
        ("Post not found", lambda request: main.post_detail_page(request, "UCmissing", "nope")),
    )

    def _patched(self):
        settings = SimpleNamespace()
        return (
            patch.object(main, "_login_required_redirect", return_value=None),
            patch.object(main, "_user_key", return_value="local"),
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: f"<shell>{content}</shell>"),
            patch.object(db, "get_video_by_youtube_id", return_value=None),
            patch.object(db, "get_channel_by_youtube_id", return_value=None),
            patch.object(db, "get_playlist", return_value=None),
            patch.object(db, "get_subscription_collection", return_value=None),
            patch("openfeed.posts.single_post", return_value=None),
        )

    def _run(self, route, accept: str):
        patches = self._patched()
        for item in patches:
            item.start()
        try:
            return route(_request(accept))
        finally:
            for item in reversed(patches):
                item.stop()

    def test_a_browser_navigation_gets_a_page_with_a_way_out(self) -> None:
        for title, route in self.ROUTES:
            with self.subTest(title=title):
                response = self._run(route, "text/html,application/xhtml+xml,*/*;q=0.8")
                body = response.body.decode()
                self.assertEqual(response.status_code, 404)
                self.assertIn('class="empty-state"', body)
                self.assertIn("<h1", body)
                self.assertRegex(body, r'<a class="btn" href="[^"]+">')

    def test_api_clients_keep_json(self) -> None:
        for accept in ("", "*/*", "application/json"):
            for title, route in self.ROUTES:
                with self.subTest(title=title, accept=accept), self.assertRaises(main.HTTPException) as caught:
                    self._run(route, accept)
                self.assertEqual(caught.exception.status_code, 404)
                self.assertEqual(caught.exception.detail, title)

    def test_the_way_out_of_a_post_is_escaped(self) -> None:
        response = self._run(lambda request: main.post_detail_page(request, 'x"><script>', "nope"), "text/html")
        self.assertNotIn(b"<script>", response.body)


class ShortsPlayerTests(unittest.TestCase):
    """A pause landing on a pending play() rejected it, and the catch restarted the Short."""

    def test_only_a_refused_autoplay_is_retried(self) -> None:
        self.assertIn("error?.name !== 'NotAllowedError' || hostWants !== 'play'", SOURCE)

    def test_a_pause_is_kept_for_a_video_that_has_not_arrived(self) -> None:
        kept = SOURCE.index("if (type === 'openfeed:pause') hostWants = 'pause';")
        self.assertLess(kept, SOURCE.index("if (!seekTarget) {", kept))
        self.assertIn("if (hostWants === 'pause') video.pause();", SOURCE)

    def test_the_stage_stops_any_short_that_reports_itself_running_off_screen(self) -> None:
        self.assertIn("if (slide !== activeSlide && !data.paused) tell(", main._home_script())


class InfiniteScrollTests(unittest.TestCase):
    """An observer reports changes only, so a short page left the sentinel in view for good."""

    def test_search_and_channel_loaders_look_again_after_each_page(self) -> None:
        script = main._home_script()
        self.assertEqual(script.count("if (!done) observeAgain();"), 2)
        self.assertEqual(script.count("observer.unobserve(sentinel);"), 2)
        self.assertIn("feedObserver?.unobserve(sentinel);", script)

    def test_the_channel_loader_stops_when_the_offset_cannot_move(self) -> None:
        self.assertIn("if (!data.has_more || (offset === from && !data.html)) done = true;", main._home_script())


class ChannelShortsPagingTests(unittest.TestCase):
    """The tab rendered one short with offset 1, and the scan at offset 1 found it again."""

    def _row(self, youtube_video_id: str, duration: int) -> dict:
        return {"youtube_video_id": youtube_video_id, "title": youtube_video_id, "duration_seconds": duration,
                "url": f"https://www.youtube.com/watch?v={youtube_video_id}", "channel_title": "Chan"}

    def test_the_offset_counts_rows_scanned_not_shorts_kept(self) -> None:
        rows = [self._row("short0", 30)] + [self._row(f"long{i}", 600) for i in range(9)]
        shorts, used = main._shorts_from_scan(rows, 80)
        self.assertEqual([row["youtube_video_id"] for row in shorts], ["short0"])
        self.assertEqual(used, 10)

    def test_a_full_page_stops_at_its_last_short_so_nothing_is_skipped(self) -> None:
        rows = [self._row(f"short{i}", 30) for i in range(5)]
        shorts, used = main._shorts_from_scan(rows, 2)
        self.assertEqual(len(shorts), 2)
        self.assertEqual(used, 2)

    def test_a_window_with_more_shorts_than_a_page_keeps_the_rest_for_the_next_request(self) -> None:
        rows = [self._row(f"short{i}", 30) for i in range(72)]
        with patch.object(db, "list_channel_videos", return_value=rows), \
             patch.object(main, "_ingest_channel_uploads", return_value=0), \
             patch.object(main, "get_settings", return_value=SimpleNamespace(invidious_public_url="")):
            out = main.api_channel_videos_html(None, "UC_x", offset=0, limit=24, kind="shorts")
        self.assertEqual(out["count"], 24)
        self.assertEqual(out["next_offset"], 24)
        self.assertTrue(out["has_more"])

    def test_the_rendered_grid_starts_paging_after_what_it_scanned(self) -> None:
        self.assertIn('data-next-offset="{next_offset}">{video_grid}', SOURCE)
        self.assertNotIn('data-next-offset="{len(videos)}">{video_grid}', SOURCE)

    def test_the_grid_shows_a_video_once_and_placeholders_never_sit_under_the_empty_state(self) -> None:
        script = main._home_script()
        self.assertIn("const seen = new Set(Array.from(grid.querySelectorAll('[data-video-card]'))", script)
        self.assertIn("if (spinner && !empty) spinner.hidden = false;", script)


class InstanceAddressTests(unittest.TestCase):
    def test_bad_addresses_are_named(self) -> None:
        for value in ("not a url !!", "https://exa mple.com", "ftp://yewtu.be", "http://", "https://", "http:",
                      "bad_host!.com", "-leading.example", "https://example.com:99999", "user:pw@example.com",
                      "https://example.com?x=1", "HTTPS://example.com"):
            with self.subTest(value=value):
                self.assertTrue(db.instance_url_problem(value))

    def test_good_addresses_pass(self) -> None:
        for value in ("yewtu.be", "yewtu.be/", "https://yewtu.be", "http://192.0.2.10:3000/", "localhost:3000",
                      "https://invidious.example.org/sub/path", "http://[::1]:3000", "münchen.example", ""):
            with self.subTest(value=value):
                self.assertEqual(db.instance_url_problem(value), "")

    def _post(self, urls: list[str], stored: list[str], allowed: bool = True):
        with patch.dict("os.environ", {"OPENFEED_ALLOW_PUBLIC_INSTANCES": "true" if allowed else "false"}), \
             patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="local"), \
             patch.object(db, "get_playback_preferences", return_value={"public_invidious_urls": stored}), \
             patch.object(db, "set_playback_preferences", return_value={"public_invidious_urls": urls}) as saved:
            payload = main.PlaybackPreferencesRequest(public_invidious_urls=urls)
            try:
                return main.api_update_playback_preferences(_request(""), payload), saved
            except main.HTTPException as exc:
                return exc, saved

    def test_the_api_refuses_a_bad_address_and_saves_nothing(self) -> None:
        answer, saved = self._post(["not a url !!"], [])
        self.assertEqual(answer.status_code, 400)
        self.assertIn("not a url !!", answer.detail)
        saved.assert_not_called()

    def test_the_api_saves_a_good_address(self) -> None:
        _, saved = self._post(["yewtu.be"], [])
        saved.assert_called_once()

    def test_adding_is_refused_when_the_admin_has_not_allowed_public_instances(self) -> None:
        answer, saved = self._post(["yewtu.be"], [], allowed=False)
        self.assertEqual(answer.status_code, 400)
        saved.assert_not_called()

    def test_removing_still_works_when_public_instances_are_not_allowed(self) -> None:
        _, saved = self._post([], ["https://yewtu.be"], allowed=False)
        saved.assert_called_once()

    def test_bad_addresses_already_stored_can_still_be_removed_one_at_a_time(self) -> None:
        _, saved = self._post(["https://not a url !!"], ["https://not a url !!", "https://other bad"])
        saved.assert_called_once()


class CountLabelTests(unittest.TestCase):
    def test_one_is_singular(self) -> None:
        self.assertEqual(main._count_label("1", "view"), "1 view")
        self.assertEqual(main._count_label(1, "video"), "1 video")

    def test_everything_else_is_plural(self) -> None:
        for count in ("0", "2", "1.2K", "1M", 1000):
            self.assertTrue(main._count_label(count, "view").endswith(" views"), count)

    def test_card_meta_says_one_view(self) -> None:
        self.assertEqual(main._video_meta_parts({"view_count": 1}), ["1 view"])

    def test_no_view_or_subscriber_string_is_left_unconditional(self) -> None:
        self.assertIsNone(re.search(r'\}\s(views|subscribers)["\'<]', SOURCE))


class ThirdPartyPlayerCopyTests(unittest.TestCase):
    """The YouTube player is off unless the admin allows it, so the copy must not lean on it."""

    def _settings(self, youtube: bool) -> SimpleNamespace:
        return SimpleNamespace(invidious_base_url="http://invidious", invidious_fallback_list=[],
                               ytdlp_enabled=False, ytdlp_playback_fallback=False, youtube_fallback_enabled=youtube)

    def test_the_admin_switch_is_what_the_copy_reads(self) -> None:
        with patch.object(main.privacy, "get_settings", return_value=self._settings(False)), \
             patch.dict("os.environ", {"OPENFEED_ALLOW_PUBLIC_INSTANCES": ""}):
            self.assertTrue(main._other_site_players_blocked())
        with patch.object(main.privacy, "get_settings", return_value=self._settings(True)):
            self.assertFalse(main._other_site_players_blocked())
            self.assertEqual(main._fallback_remedy("Keep it on."), "Keep it on.")

    def test_the_remedy_names_the_cause(self) -> None:
        with patch.object(main.privacy, "get_settings", return_value=self._settings(False)):
            text = main._fallback_remedy("Leave the YouTube fallback enabled.")
        self.assertIn("turned off on this server", text)
        self.assertNotIn("enabled", text)

    def test_third_party_players_are_offered_only_when_allowed_and_opted_in(self) -> None:
        preferences = {"public_invidious_urls": ["https://yewtu.be"], "disabled_sources": [], "source_order": []}
        cases = [
            (False, {"youtube_player": True, "public_instances": True}, set()),
            (True, {"youtube_player": False, "public_instances": False}, set()),
            (True, {"youtube_player": True, "public_instances": True}, {"youtube", "invidious:yewtu.be"}),
        ]
        for allowed, choices, expected in cases:
            with self.subTest(allowed=allowed, choices=choices), \
                 patch.object(main, "get_settings", return_value=self._settings(allowed)), \
                 patch.object(main.privacy, "get_settings", return_value=self._settings(allowed)), \
                 patch.dict("os.environ", {"OPENFEED_ALLOW_PUBLIC_INSTANCES": "true" if allowed else ""}), \
                 patch.object(main.privacy, "get_choices", lambda user_key="local", c=choices: dict(c)), \
                 patch.object(db, "get_playback_preferences", return_value=preferences):
                offered = main._on_request_sources()
                self.assertEqual({source["id"] for source in offered}, expected)
                for source in offered:
                    self.assertTrue(source["warning"].startswith("Plays from"))
                    self.assertTrue(source["origin"].startswith("https://"))

    def test_no_copy_advises_the_fallback_outside_the_policy_check(self) -> None:
        for sentence in ("Meanwhile, the YouTube player under Settings", "you can turn on the YouTube player under Settings"):
            self.assertEqual(SOURCE.count(sentence), 1)
            self.assertIn('_fallback_remedy("', SOURCE[SOURCE.index(sentence) - 60:SOURCE.index(sentence)])


class DesignTokenTests(unittest.TestCase):
    """Smoke, Deep Smoke and the focus ring were measured at 3.4:1 to 3.7:1 and 2.7:1."""

    def test_the_tokens_hold_the_measured_values(self) -> None:
        tokens = (ASSETS / "css" / "00-tokens.css").read_text(encoding="utf-8")
        self.assertIn("--muted: #9a9aa3;", tokens)
        self.assertIn("--muted-2: #84848d;", tokens)
        self.assertIn("--focus: 0 0 0 2px rgba(255,255,255,.5);", tokens)

    def test_no_copy_of_an_old_value_survives(self) -> None:
        files = [APP / "main.py", *ASSETS.glob("css/*.css"), *ASSETS.glob("js/*.js"), *(path for path in ASSETS.glob("player/**/*") if path.is_file()),
                 APP.parents[1] / "DESIGN.md", APP.parents[1] / ".impeccable" / "design.json"]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            with self.subTest(path=path.name):
                self.assertNotIn("8b8b93", text)
                self.assertNotIn("6a6a72", text)
                self.assertIsNone(re.search(r"rgba\(255,\s*255,\s*255,\s*0?\.3\)", text))

    def test_pill_search_fields_draw_the_ring_on_the_pill(self) -> None:
        css = main._home_styles()
        self.assertIn(".toolbar-search:has(input:focus-visible)", css)
        self.assertIn(".toolbar-search input:focus-visible, .page-search input:focus-visible { box-shadow: none; }", css)


class StructureTests(unittest.TestCase):
    def test_shorts_has_an_h1_with_slides_and_without(self) -> None:
        self.assertIn('<h1 class="sr-only">Shorts</h1>', main._shorts_page_html([], "local"))

    def test_home_has_an_h1(self) -> None:
        self.assertIn('<h1 class="sr-only">Home</h1>', SOURCE)

    def test_timeline_days_are_headings(self) -> None:
        settings = SimpleNamespace(invidious_public_url="")
        video = {"youtube_video_id": "abc123def45", "title": "t", "channel_title": "c", "youtube_channel_id": "UCabc",
                 "published_at": "2026-01-02T00:00:00+00:00"}
        self.assertIn('<h2 class="timeline-date">', main._subscription_timeline([video], settings))

    def test_the_details_dialog_and_channel_menu_are_named(self) -> None:
        self.assertIn('data-details-dialog aria-labelledby="details-dialog-title"', SOURCE)
        self.assertIn('<h2 id="details-dialog-title">Details</h2>', SOURCE)
        self.assertIn('<div class="menu-panel" role="menu" aria-label="More channel actions">', SOURCE)


class DescriptionCardTests(unittest.TestCase):
    def test_the_card_closes_too_but_never_on_a_link_or_a_selection(self) -> None:
        script = main._home_script()
        self.assertIn("event.target.closest('[data-description-box]');", script)
        self.assertIn("!event.target.closest('a, button') && !String(window.getSelection())", script)
        self.assertIn("if (event.detail > 1) return;", script)


class SourceSwitchTests(unittest.TestCase):
    def test_a_failed_save_puts_the_switch_back(self) -> None:
        script = main._home_script()
        self.assertIn("if (!await setEnabled(source, toggle.checked)) toggle.checked = source.enabled !== false;", script)
        self.assertIn("return save({ disabled_sources: [...disabled] });", script)


class ShellRailTests(unittest.TestCase):
    def test_the_rail_keeps_show_more(self) -> None:
        rail_hides = re.search(r"html\.nav-rail[^{]*:is\(([^)]*)\)\s*\{\s*display: none", main._home_styles())
        self.assertNotIn(".nav-more", rail_hides.group(1))


class _Upstream:
    """Just enough of an httpx response for the proxy and the playback diagnosis."""

    def __init__(self, status_code: int = 200, text: str = "", headers: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = httpx.Headers(headers or {})
        self.closed = False

    def read(self) -> bytes:
        return self.text.encode()

    def close(self) -> None:
        self.closed = True

    def iter_bytes(self):
        yield self.text.encode()


def _error_page(sentence: str) -> str:
    return f'<title>Error - Invidious</title><div class="pure-g navbar h-box">x</div>\n<div class="h-box">\n    {sentence}\n</div>'


class QuickActionTests(unittest.TestCase):
    """Reported as a real click navigating, which was the test's selector and not the page.

    `button[data-save-to-playlist]` first matches the row in the closed overflow menu, whose
    box still reports a rect over the thumbnail, so the coordinates landed on the thumb.
    Real clicks on the visible button open the picker. What keeps that true is here.
    """

    def test_quick_actions_are_not_inside_the_thumbnail_link(self) -> None:
        video = {"youtube_video_id": "abc123def45", "title": "t", "channel_title": "c", "youtube_channel_id": "UCabc"}
        html = main._home_video_card(video, 1, SimpleNamespace(invidious_public_url=""))
        self.assertGreater(html.index('class="card-quick"'), html.index("</a>", html.index('class="thumb"')))

    def test_quick_actions_sit_above_the_card(self) -> None:
        self.assertIn(".video-card .card-quick { position: absolute; top: 8px; right: 46px; z-index: 3;", main._home_styles())


class EndOfListTests(unittest.TestCase):
    """Hiding the loader shortened the page and the browser moved the viewport to fit."""

    def test_every_list_hides_its_loader_through_the_shared_helper(self) -> None:
        script = main._home_script()
        self.assertEqual(script.count("window.ofHideLoader(spinner);"), 2)
        self.assertEqual(script.count("window.ofHideLoader(loading);"), 1)
        self.assertNotIn("spinner.hidden = true", script)

    def test_the_helper_restores_the_position_and_gives_the_space_back(self) -> None:
        kit = (ASSETS / "js" / "ui-kit.js").read_text(encoding="utf-8")
        self.assertIn("window.scrollTo({ top, behavior: 'instant' });", kit)
        self.assertIn("window.removeEventListener('scroll', release);", kit)

    def test_one_column_shows_one_placeholder(self) -> None:
        self.assertIn(".feed-loading .video-card ~ .video-card, .feed-loading .media-row ~ .media-row", main._home_styles())


class ShortsPhoneTabTests(unittest.TestCase):
    def test_the_storyboard_track_reaches_invidious_and_never_the_home_page(self) -> None:
        with patch.object(main, "proxy_invidious", return_value=Response()) as proxied:
            self.assertEqual(TestClient(main.app).get("/embed/api/v1/storyboards/abc?height=90").status_code, 200)
        self.assertEqual(proxied.call_args[0][1], "api/v1/storyboards/abc")

    def test_an_ordinary_embed_still_goes_to_the_embed(self) -> None:
        with patch.object(main, "proxy_invidious", return_value=Response()) as proxied:
            TestClient(main.app).get("/embed/abc")
        self.assertEqual(proxied.call_args[0][1], "embed/abc")

    def test_the_header_stays_rendered_for_the_sheets_it_holds(self) -> None:
        css = main._home_styles()
        self.assertNotIn("body:has(.shorts-main) .app-header { display: none; }", css)
        self.assertIn("body:has(.shorts-main) .app-header .yt-menu[open] .menu-panel { visibility: visible; }", css)


class EmptyManifestTests(unittest.TestCase):
    """An MPD with no AdaptationSet made video.js throw instead of reporting a failure."""

    def _proxy(self, body: str, method: str = "GET", require_media: bool = True) -> tuple[Response, _Upstream]:
        upstream = _Upstream(200, body, {"content-type": "application/dash+xml"})
        client = SimpleNamespace(build_request=lambda *a, **k: None, send=lambda *a, **k: upstream)
        request = Request({"type": "http", "method": method, "path": "/companion/x", "query_string": b"", "headers": [],
                           "scheme": "http", "server": ("testserver", 80)})
        settings = SimpleNamespace(invidious_base_url="http://invidious")
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(main, "_invidious_client", return_value=client):
            return main.proxy_invidious(request, "companion/x", require_media=require_media), upstream

    def test_a_manifest_with_no_media_is_a_failed_request(self) -> None:
        response, upstream = self._proxy('<MPD mediaPresentationDuration="PTNaNS"><Period/></MPD>')
        self.assertEqual(response.status_code, 502)
        self.assertTrue(upstream.closed)

    def test_a_real_manifest_passes_unchanged(self) -> None:
        body = "<MPD><Period><AdaptationSet/></Period></MPD>"
        response, _ = self._proxy(body)
        self.assertEqual((response.status_code, response.body.decode()), (200, body))

    def test_only_the_manifest_route_is_buffered(self) -> None:
        with patch.object(main, "proxy_invidious", return_value=Response()) as proxied:
            TestClient(main.app).get("/companion/videoplayback?x=1")
            self.assertFalse(proxied.call_args.kwargs["require_media"])
            TestClient(main.app).get("/companion/api/manifest/dash/id/abc")
            self.assertTrue(proxied.call_args.kwargs["require_media"])


class FailureCauseTests(unittest.TestCase):
    """Principle 4: the headline names the first real cause, from the evidence in hand."""

    def _diagnose(self, status_code: int, page: str) -> dict:
        def get(url: str, **_: object) -> _Upstream:
            return _Upstream(200, "[]") if "trending" in url else _Upstream(status_code, page)

        settings = SimpleNamespace(invidious_base_url="http://invidious")
        with patch.object(main, "get_settings", return_value=settings), \
             patch.object(main, "_invidious_client", return_value=SimpleNamespace(get=get)):
            return main._diagnose_playback("abc")

    def test_an_unknown_id_quotes_invidious(self) -> None:
        result = self._diagnose(404, _error_page("Video unavailable"))
        self.assertEqual(result["code"], "video-unavailable")
        self.assertIn("Video unavailable", result["headline"])
        self.assertNotIn("YouTube refused", result["headline"])
        self.assertIn("HTTP 404", result["steps"][-1]["detail"])

    def test_a_removed_video_says_so(self) -> None:
        self.assertIn("removed by the uploader", self._diagnose(500, _error_page("This video has been removed by the uploader"))["headline"])

    def test_youtube_is_blamed_only_when_the_page_says_so(self) -> None:
        self.assertEqual(self._diagnose(500, _error_page("Sign in to confirm you're not a bot"))["code"], "no-formats")

    def test_no_reason_means_no_claimed_reason(self) -> None:
        result = self._diagnose(500, "<html>nope</html>")
        self.assertEqual(result["code"], "no-player-page")
        self.assertIn("HTTP 500", result["explanation"])

    def test_the_panel_names_the_first_failure_and_passes_over_a_policy_block(self) -> None:
        self.assertIn("failed.find(([, item]) => item.why !== REASONS.csp) || failed[0]", SOURCE)
        self.assertNotIn("failed[failed.length - 1]", SOURCE)

    def test_the_hop_walk_only_speaks_when_invidious_was_tried(self) -> None:
        self.assertIn("if (!diagnosis.ok && attempts.has('invidious')) {", SOURCE)


class ShortsLeavingTests(unittest.TestCase):
    """A frame that started itself could not hear a pause until it had loaded."""

    def test_no_short_frame_autoplays_itself(self) -> None:
        script = main._home_script()
        self.assertIn("/player?autoplay=0&short=1`", script)
        self.assertNotIn("autoplay=${active ? 1 : 0}", script)

    def test_the_shell_asks_and_the_stage_answers_for_the_slide_on_screen(self) -> None:
        self.assertIn("postUp({ type: 'openfeed:shell-ready' });", SOURCE)
        self.assertIn("slide === activeSlide ? 'openfeed:play' : 'openfeed:pause'", main._home_script())

    def test_a_parked_short_stops_at_the_play_event(self) -> None:
        self.assertIn("video.addEventListener('play', () => { if (hostWants === 'pause') video.pause(); });", SOURCE)


class UpstreamCommentsTests(unittest.TestCase):
    """Invidious answers most ids with a 500 on top comments. The message is the true state."""

    def test_an_upstream_failure_keeps_the_plain_message(self) -> None:
        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def get(self, *_: object, **__: object) -> _Upstream:
                return _Upstream(500, '{"error":"Missing hash key: \\"commentRenderer\\""}')

        settings = SimpleNamespace(invidious_base_url="http://invidious", enrichment_timeout_seconds=5)
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="local"), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(db, "get_feed_preferences", return_value={"show_comments": True}), \
             patch.object(main.httpx, "Client", return_value=Client()), \
             self.assertRaises(main.HTTPException) as caught:
            main.api_comments(_request(""), "abc")
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (502, "Comments are unavailable for this video"))


class WatchTabTests(unittest.TestCase):
    def test_no_tab_is_current_without_an_active_page(self) -> None:
        self.assertNotIn("aria-current", main._mobile_nav(""))

    def test_the_watch_page_claims_no_tab(self) -> None:
        self.assertIn('_app_shell(title_text, "", content, main_class="watch-main"', SOURCE)


class NavigationFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kit = (ASSETS / "js" / "ui-kit.js").read_text(encoding="utf-8")

    def test_it_waits_a_quarter_second_and_clears_on_the_way_back(self) -> None:
        self.assertIn("const NAV_DELAY = 250;", self.kit)
        self.assertIn("window.addEventListener('pageshow', navDone);", self.kit)

    def test_it_leaves_alone_what_is_not_a_page_load(self) -> None:
        for rule in ("event.metaKey || event.ctrlKey || event.shiftKey || event.altKey", "link.hasAttribute('download')",
                     "link.target !== '_self'", "url.origin !== window.location.origin", "url.pathname.startsWith('/api/')",
                     "url.hash && url.pathname === window.location.pathname", "form.method !== 'get'"):
            self.assertIn(rule, self.kit)

    def test_it_runs_after_page_handlers_that_cancel_the_navigation(self) -> None:
        self.assertIn("window.addEventListener('click', (event) => {\n    if (event.defaultPrevented", self.kit)

    def test_it_is_a_bone_line_and_still_under_reduced_motion(self) -> None:
        css = main._home_styles()
        self.assertIn("background: var(--bone); animation: nav-line", css)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .nav-line::after { animation: none;", css)


class ChannelShortsEndTests(unittest.TestCase):
    def test_an_empty_scan_ends_the_list_even_after_a_fetch(self) -> None:
        with patch.object(db, "list_channel_videos", return_value=[]), \
             patch.object(main, "_ingest_channel_uploads", return_value=60), \
             patch.object(main, "get_settings", return_value=SimpleNamespace(invidious_public_url="")):
            out = main.api_channel_videos_html(None, "UC_x", offset=100, limit=24, kind="shorts")
        self.assertEqual((out["count"], out["has_more"]), (0, False))
