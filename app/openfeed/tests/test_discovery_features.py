"""DeArrow, search filters, and "Don't recommend channel"."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openfeed import db, dearrow, discovery, main
from openfeed.tests.support import offline_db


def _settings(**extra: object) -> SimpleNamespace:
    base = dict(dearrow_enabled=True, dearrow_cache_seconds=3600, dearrow_cache_dir="/nonexistent", dearrow_cache_max_frames=100)
    base.update(extra)
    return SimpleNamespace(**base)


class _Client:
    """httpx.Client stand-in that records every GET and answers with `payload`."""

    calls: list[dict[str, object]] = []
    payload: object = {}
    status = 200

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url, params=None):
        type(self).calls.append({"url": url, "params": params})
        return httpx.Response(type(self).status, json=type(self).payload, request=httpx.Request("GET", url))


class DeArrowPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        dearrow.clear_cache()
        _Client.calls = []
        _Client.status = 200

    def test_only_a_four_character_hash_prefix_is_sent(self) -> None:
        _Client.payload = {}
        with patch.object(dearrow.httpx, "Client", _Client), patch.object(dearrow, "get_settings", lambda: _settings()):
            dearrow.lookup(["dQw4w9WgXcQ"])
        self.assertEqual(len(_Client.calls), 1)
        url = str(_Client.calls[0]["url"])
        self.assertNotIn("dQw4w9WgXcQ", url)
        self.assertNotIn("dQw4w9WgXcQ", str(_Client.calls[0]["params"]))
        self.assertTrue(url.endswith("/" + hashlib.sha256(b"dQw4w9WgXcQ").hexdigest()[:4]))

    def test_the_endpoint_is_a_constant_and_the_module_is_read_only(self) -> None:
        self.assertEqual(dearrow.DEARROW_ENDPOINT, "https://sponsor.ajay.app/api/branding")
        text = Path(dearrow.__file__ or "").read_text(encoding="utf-8")
        for forbidden in ("client.post(", "client.put(", '"POST"', "dearrow-thumb.ajay.app", "getThumbnail"):
            self.assertNotIn(forbidden, text)

    def test_a_bucket_is_fetched_once_for_every_video_in_it(self) -> None:
        _Client.payload = {}
        ids = ["dQw4w9WgXcQ", "dQw4w9WgXcQ", "jNQXAC9IVRw"]
        with patch.object(dearrow.httpx, "Client", _Client), patch.object(dearrow, "get_settings", lambda: _settings()):
            dearrow.lookup(ids)
            dearrow.lookup(ids)
        prefixes = {dearrow.hash_prefix(vid) for vid in ids}
        self.assertEqual(len(_Client.calls), len(prefixes))

    def test_a_failure_is_not_fatal_and_backs_off(self) -> None:
        _Client.status = 503
        with patch.object(dearrow.httpx, "Client", _Client), patch.object(dearrow, "get_settings", lambda: _settings()):
            self.assertEqual(dearrow.lookup(["dQw4w9WgXcQ"]), {})
            self.assertEqual(dearrow.lookup(["dQw4w9WgXcQ"]), {})
        self.assertEqual(len(_Client.calls), 1)

    def test_a_slow_bucket_never_holds_the_page_past_the_budget(self) -> None:
        import time

        def slow(prefix: str) -> dict:
            time.sleep(1.0)
            return {"dQw4w9WgXcQ": {"title": "late", "thumbnail": None}}

        with patch.object(dearrow, "fetch_prefix", slow), patch.object(dearrow, "LOOKUP_BUDGET_SECONDS", 0.1), \
             patch.object(dearrow, "get_settings", lambda: _settings()):
            started = time.monotonic()
            self.assertEqual(dearrow.lookup(["dQw4w9WgXcQ"]), {})
            self.assertLess(time.monotonic() - started, 0.8)
            time.sleep(1.2)
            # The bucket finished in the background and is cached for the next page.
            self.assertEqual(dearrow.lookup(["dQw4w9WgXcQ"], cached_only=True)["dQw4w9WgXcQ"]["title"], "late")

    def test_off_unless_the_deployment_and_the_user_both_allow_it(self) -> None:
        for deployment, user, expected in ((True, False, False), (False, True, False), (True, True, True)):
            with patch.object(dearrow, "get_settings", lambda d=deployment: _settings(dearrow_enabled=d)), \
                 patch.object(db, "get_feed_preferences", return_value={"dearrow_enabled": user}):
                self.assertEqual(dearrow.enabled(), expected)
                self.assertEqual(dearrow.preferences()["enabled"], expected)

    def test_disabled_answers_without_any_request(self) -> None:
        with patch.object(dearrow.httpx, "Client", _Client), \
             patch.object(dearrow, "get_settings", lambda: _settings()), \
             patch.object(db, "get_feed_preferences", return_value={}):
            self.assertEqual(dearrow.branding_for(["dQw4w9WgXcQ"])["reason"], "disabled")
        self.assertEqual(_Client.calls, [])


class DeArrowChoiceTests(unittest.TestCase):
    def test_the_first_trusted_title_and_thumbnail_win(self) -> None:
        chosen = dearrow.pick_branding({
            "titles": [{"title": "Plain description", "original": False, "votes": 0, "locked": False}],
            "thumbnails": [{"timestamp": 12.5, "original": False, "votes": 1, "locked": False}],
        })
        self.assertEqual(chosen, {"title": "Plain description", "thumbnail": 12.5})

    def test_negative_scores_are_untrusted_unless_locked(self) -> None:
        untrusted = {"titles": [{"title": "x", "votes": -1, "locked": False}], "thumbnails": [{"timestamp": 3, "votes": -2}]}
        self.assertEqual(dearrow.pick_branding(untrusted), {"title": None, "thumbnail": None})
        locked = {"titles": [{"title": "x", "votes": -1, "locked": True}]}
        self.assertEqual(dearrow.pick_branding(locked)["title"], "x")

    def test_an_original_vote_means_no_replacement(self) -> None:
        entry = {
            "titles": [{"title": "Creator title", "original": True, "votes": 5}],
            "thumbnails": [{"timestamp": None, "original": True, "votes": 9}],
        }
        self.assertEqual(dearrow.pick_branding(entry), {"title": None, "thumbnail": None})

    def test_a_bucket_keeps_only_videos_with_something_to_show(self) -> None:
        payload = {
            "dQw4w9WgXcQ": {"titles": [{"title": "Rick Astley - Never gonna give you up", "votes": 1}], "thumbnails": []},
            "jNQXAC9IVRw": {"titles": [], "thumbnails": []},
            "not-a-video-id": {"titles": [{"title": "x", "votes": 3}]},
        }
        self.assertEqual(list(dearrow.parse_branding(payload)), ["dQw4w9WgXcQ"])


class DeArrowFormattingTests(unittest.TestCase):
    def test_the_keep_marker_is_removed(self) -> None:
        self.assertEqual(dearrow.format_title(">iPhone review, >CPU test"), "iPhone review, CPU test")
        self.assertEqual(dearrow.format_title("2 > 1 is true"), "2 > 1 is true")

    def test_lowercase_lowercases_every_word_like_dearrow(self) -> None:
        # Acronyms, custom capitalisation and ">"-marked words are all lowercased.
        self.assertEqual(dearrow.to_lowercase("NASA Tests the NEW >iPhone CPU"), "nasa tests the new iphone cpu")

    def test_lowercase_keeps_greek_symbols_in_a_latin_title(self) -> None:
        self.assertEqual(dearrow.to_lowercase("Why ΔT Matters"), "why ΔT matters")
        self.assertEqual(dearrow.to_lowercase("ΑΘΗΝΑ ΣΗΜΕΡΑ"), "αθηνα σημερα")

    def test_lowercase_uses_turkish_rules_for_a_dotted_i(self) -> None:
        self.assertEqual(dearrow.to_lowercase("İSTANBUL IŞIK"), "istanbul ışık")


class DeArrowFrameTests(unittest.TestCase):
    def test_frames_are_queued_not_cut_inline_and_never_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(dearrow, "get_settings", lambda: _settings(dearrow_cache_dir=tmp)), \
             patch.object(dearrow, "_ensure_worker", lambda: None):
            dearrow._pending.clear()
            self.assertTrue(dearrow.request_frame("dQw4w9WgXcQ", 3.9))
            self.assertFalse(dearrow.request_frame("dQw4w9WgXcQ", 3.9))
            self.assertFalse(dearrow.request_frame("../../etc", 1))
            dearrow._pending.clear()
            while not dearrow._frames.empty():
                dearrow._frames.get_nowait()

    def test_the_stream_always_goes_through_the_configured_invidious(self) -> None:
        payload = {"lengthSeconds": 212, "adaptiveFormats": [{"itag": "135", "url": "https://elsewhere.example/videoplayback?id=1&itag=135"}]}

        class _Inv(_Client):
            pass

        _Inv.payload, _Inv.status, _Inv.calls = payload, 200, []
        with patch.object(dearrow.httpx, "Client", _Inv), \
             patch.object(dearrow, "get_settings", lambda: _settings(invidious_base_url="http://invidious:3000")):
            url, length = dearrow.stream_url("dQw4w9WgXcQ")
        self.assertEqual(url, "http://invidious:3000/videoplayback?id=1&itag=135")
        self.assertEqual(length, 212)
        self.assertEqual(_Inv.calls[0]["params"]["local"], "true")

    def test_branding_reports_a_frame_only_once_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(dearrow, "get_settings", lambda: _settings(dearrow_cache_dir=tmp)), \
             patch.object(db, "get_feed_preferences", return_value={"dearrow_enabled": True}), \
             patch.object(dearrow, "lookup", return_value={"dQw4w9WgXcQ": {"title": "T", "thumbnail": 3.9}}), \
             patch.object(dearrow, "request_frame") as queued:
            first = dearrow.branding_for(["dQw4w9WgXcQ"])["items"]["dQw4w9WgXcQ"]
            self.assertNotIn("thumbnail", first)
            queued.assert_called_once()
            dearrow.frame_path("dQw4w9WgXcQ", 3.9).write_bytes(b"jpeg")
            second = dearrow.branding_for(["dQw4w9WgXcQ"])["items"]["dQw4w9WgXcQ"]
            self.assertEqual(second["thumbnail"], "/api/dearrow/thumbnail/dQw4w9WgXcQ.jpg")
            self.assertEqual(dearrow.current_frame("dQw4w9WgXcQ"), dearrow.frame_path("dQw4w9WgXcQ", 3.9))


class DeArrowPageTests(unittest.TestCase):
    def test_the_body_flag_is_set_only_when_enabled(self) -> None:
        with patch.object(dearrow, "preferences", return_value={"enabled": False, "casing": "", "thumbnails": True}):
            self.assertEqual(main._dearrow_body_attrs("local"), "")
        with patch.object(dearrow, "preferences", return_value={"enabled": True, "casing": "lowercase", "thumbnails": True}):
            self.assertEqual(main._dearrow_body_attrs("local"), ' data-dearrow="on" data-dearrow-casing="lowercase"')

    def test_settings_state_the_trade_and_default_off(self) -> None:
        with offline_db():
            html = main._dearrow_settings_html("local")
        self.assertIn("data-setting-dearrow", html)
        self.assertIn("first four characters", html)
        self.assertIn('data-dearrow-options hidden', html)
        self.assertNotIn('name="dearrow_enabled" checked', html)

    def test_the_shell_script_asks_this_server_never_dearrow(self) -> None:
        script = main._home_script()
        self.assertIn("/api/dearrow/branding", script)
        self.assertNotIn("sponsor.ajay.app", script)
        self.assertNotIn("dearrow-thumb.ajay.app", script)


class SearchFilterTests(unittest.TestCase):
    def test_filters_live_in_the_url(self) -> None:
        href = main._search_href("wood", "videos", "upload_date", "week", "long", "4k")
        self.assertEqual(href, "/search?q=wood&type=videos&sort=upload_date&date=week&duration=long&features=4k")

    def test_unknown_or_meaningless_values_fall_away(self) -> None:
        self.assertEqual(main._search_filters("all", "bogus", "decade", "huge", "smell")["sort"], "relevance")
        channels = main._search_filters("channels", "rating", "week", "long", "hd")
        self.assertEqual((channels["sort"], channels["date"], channels["duration"], channels["features"]), ("relevance", "", "", ""))
        self.assertEqual(main._search_filters("shorts", "rating", "", "", "hd")["features"], "")

    def test_every_sort_the_brief_names_is_offered(self) -> None:
        self.assertEqual([key for key, _ in main._SEARCH_SORTS], ["relevance", "upload_date", "view_count", "rating"])
        html = main._search_controls("wood", main._search_filters("all", "relevance", "", "", ""))
        for label in ("Sort", "Date", "Length", "Features"):
            self.assertIn(f'aria-label="{label}:', html)

    def test_sorts_youtube_ignores_are_done_here_and_said_so(self) -> None:
        from datetime import datetime, timezone

        rows = [
            {"youtube_video_id": "a", "published_at": datetime(2024, 1, 1, tzinfo=timezone.utc)},
            {"youtube_video_id": "b", "published_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"youtube_video_id": "c", "published_at": None},
        ]
        self.assertEqual([r["youtube_video_id"] for r in main._order_search_page(rows, "upload_date")], ["b", "a", "c"])
        self.assertIn("newest first", main._search_sort_note(main._search_filters("all", "upload_date", "", "")))
        self.assertIn("Return YouTube Dislike", main._search_sort_note(main._search_filters("all", "rating", "", "")))
        self.assertEqual(main._search_sort_note(main._search_filters("all", "view_count", "", "")), "")

    def test_features_reach_invidious_and_the_library_is_not_faked(self) -> None:
        _Client.payload, _Client.status, _Client.calls = [], 200, []
        with patch.object(discovery.httpx, "Client", _Client), \
             patch.object(discovery, "get_settings", lambda: SimpleNamespace(invidious_base_url="http://inv", enrichment_timeout_seconds=5)):
            discovery.search_youtube("wood", features="4k", date="hour")
            discovery.search_youtube("wood", features="purchased")
        self.assertEqual(_Client.calls[0]["params"]["features"], "4k")
        self.assertEqual(_Client.calls[0]["params"]["date"], "hour")
        self.assertNotIn("features", _Client.calls[1]["params"])
        with patch.object(db, "search_videos") as library:
            self.assertEqual(main._search_local_videos("wood", 10, main._search_filters("all", "relevance", "", "", "4k")), [])
        library.assert_not_called()

    def test_library_sorts_by_date_and_by_ryd_rating(self) -> None:
        captured: list[str] = []

        class _Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): captured.append(sql)
            def fetchall(self): return []

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cursor()

        with patch.object(db, "connect", lambda: _Conn()):
            db.search_videos("wood", sort="upload_date")
            db.search_videos("wood", sort="rating")
        self.assertIn("ORDER BY v.published_at DESC", captured[0])
        self.assertIn("v.like_count::float / (v.like_count + v.dislike_count)", captured[1])
        self.assertIn("v.dislike_source = 'ryd'", captured[1])
        self.assertNotIn("/ v.view_count", captured[1])


class DontRecommendChannelTests(unittest.TestCase):
    def test_every_card_menu_offers_it_next_to_not_interested(self) -> None:
        menu = main._video_action_menu("dQw4w9WgXcQ", "Title", "/watch/dQw4w9WgXcQ", "https://y", channel_id="UCabc", channel_title="Rick")
        self.assertIn("Don&#x27;t recommend channel", menu)
        self.assertIn('data-channel-key="ucabc"', menu)
        self.assertLess(menu.index("Not interested"), menu.index("recommend channel"))
        self.assertNotIn("data-mute-from-card", main._video_action_menu("x", "T", "/w", "https://y"))

    def test_the_endpoint_changes_one_key_of_the_same_storage(self) -> None:
        client = TestClient(main.app)
        with patch.object(db, "set_channel_muted", return_value=["ucabc"]) as setter:
            response = client.post("/api/feed/muted-channels", json={"channel_key": "UCabc", "muted": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["muted"])
        setter.assert_called_once_with("UCabc", True, "local")
        self.assertEqual(client.post("/api/feed/muted-channels", json={"channel_key": " "}).status_code, 400)

    def test_the_feed_dialog_lists_muted_creators_with_unmute(self) -> None:
        with offline_db():
            dialog = main._feed_dialog("local")
        self.assertIn("data-fc-muted", dialog)
        script = main._home_script()
        self.assertIn("data-unmute-channel", script)
        self.assertIn("/api/feed/muted-channels", script)


if __name__ == "__main__":
    unittest.main()
