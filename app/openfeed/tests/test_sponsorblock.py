from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from openfeed import db, main, sponsorblock
from openfeed import sponsorblock as sb


class PrivacyTests(unittest.TestCase):
    """The hashed lookup is the entire reason this is acceptable at all."""

    def test_only_a_four_character_hash_prefix_is_sent(self) -> None:
        prefix = sb.hash_prefix("dQw4w9WgXcQ")
        self.assertEqual(len(prefix), 4)
        self.assertTrue(hashlib.sha256(b"dQw4w9WgXcQ").hexdigest().startswith(prefix))

    def test_the_video_id_never_appears_in_the_request_url(self) -> None:
        seen: dict[str, object] = {}

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, params=None):
                seen["url"] = url
                seen["params"] = params
                return httpx.Response(200, json=[], request=httpx.Request("GET", url))

        with patch.object(sb.httpx, "Client", _Client), \
             patch.object(sb, "get_settings", lambda: SimpleNamespace(sponsorblock_cache_seconds=0)):
            sb.fetch_segments("dQw4w9WgXcQ", ["sponsor"])
        self.assertNotIn("dQw4w9WgXcQ", str(seen["url"]))
        self.assertNotIn("dQw4w9WgXcQ", str(seen["params"]))

    def test_the_endpoint_is_a_constant(self) -> None:
        self.assertEqual(sb.SPONSORBLOCK_ENDPOINT, "https://sponsor.ajay.app/api/skipSegments")

    def test_the_module_is_read_only(self) -> None:
        # No submissions, votes or reports: all of them would require identifying a user.
        with open(sb.__file__ or "", encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in ("client.post(", "client.put(", '"POST"', "voteOnSponsorTime", "skipSegments/vote"):
            self.assertNotIn(forbidden, text)


class OptInTests(unittest.TestCase):
    def _enabled(self, deployment: bool, user: bool) -> bool:
        with patch.object(sb, "get_settings", lambda: SimpleNamespace(sponsorblock_enabled=deployment)), \
             patch.object(db, "get_feed_preferences", return_value={"sponsorblock_enabled": user}):
            return sb.enabled()

    def test_off_by_default_for_the_user(self) -> None:
        self.assertFalse(self._enabled(True, False))

    def test_the_deployment_can_veto_it(self) -> None:
        self.assertFalse(self._enabled(False, True))

    def test_both_together_enable_it(self) -> None:
        self.assertTrue(self._enabled(True, True))

    def test_a_preference_failure_fails_closed(self) -> None:
        with patch.object(sb, "get_settings", lambda: SimpleNamespace(sponsorblock_enabled=True)), \
             patch.object(db, "get_feed_preferences", side_effect=RuntimeError("db down")):
            self.assertFalse(sb.enabled())

    def test_nothing_is_fetched_while_disabled(self) -> None:
        with patch.object(sb, "enabled", lambda user_key="local": False):
            self.assertEqual(sb.segments_for("dQw4w9WgXcQ")["reason"], "disabled")


class DefaultsTests(unittest.TestCase):
    def test_only_sponsor_and_selfpromo_auto_skip(self) -> None:
        skipping = {k for k, v in sb.DEFAULT_ACTIONS.items() if v == "skip"}
        self.assertEqual(skipping, {"sponsor", "selfpromo"})

    def test_every_requested_category_exists(self) -> None:
        for key in ("sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "filler"):
            self.assertIn(key, sb.CATEGORY_KEYS)

    def test_unknown_categories_and_actions_are_rejected(self) -> None:
        actions = sb.normalize_actions({"sponsor": "nonsense", "not_a_category": "skip"})
        self.assertEqual(actions["sponsor"], "skip")  # falls back to the default
        self.assertNotIn("not_a_category", actions)

    def test_a_category_set_to_off_is_never_requested(self) -> None:
        # Not asking about it at all is stronger than asking and ignoring the answer.
        prefs = {"sponsorblock_actions": {k: "off" for k in sb.CATEGORY_KEYS} | {"sponsor": "skip"}}
        with patch.object(db, "get_feed_preferences", return_value=prefs):
            self.assertEqual(sb.active_categories(), ["sponsor"])


class ParsingTests(unittest.TestCase):
    def _payload(self, video_id="dQw4w9WgXcQ", **over):
        segment = {"category": "sponsor", "segment": [10.0, 25.0], "UUID": "abc", "votes": 3}
        segment.update(over)
        return [{"videoID": video_id, "segments": [segment]}]

    def test_segments_for_other_videos_in_the_hash_bucket_are_discarded(self) -> None:
        # The whole point of the prefix endpoint is that it returns other videos too.
        payload = self._payload() + [{"videoID": "someotherid", "segments": [{"category": "sponsor", "segment": [1, 2]}]}]
        parsed = sb.parse_segments(payload, "dQw4w9WgXcQ", ["sponsor"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["start"], 10.0)

    def test_categories_not_asked_for_are_dropped(self) -> None:
        self.assertEqual(sb.parse_segments(self._payload(category="filler"), "dQw4w9WgXcQ", ["sponsor"]), [])

    def test_reversed_and_zero_length_segments_are_refused(self) -> None:
        self.assertEqual(sb.parse_segments(self._payload(segment=[30, 10]), "dQw4w9WgXcQ", ["sponsor"]), [])
        self.assertEqual(sb.parse_segments(self._payload(segment=[10, 10]), "dQw4w9WgXcQ", ["sponsor"]), [])

    def test_absurd_segments_are_refused(self) -> None:
        self.assertEqual(sb.parse_segments(self._payload(segment=[0, 999999]), "dQw4w9WgXcQ", ["sponsor"]), [])

    def test_non_numeric_bounds_do_not_raise(self) -> None:
        self.assertEqual(sb.parse_segments(self._payload(segment=["a", "b"]), "dQw4w9WgXcQ", ["sponsor"]), [])

    def test_a_non_list_response_yields_nothing(self) -> None:
        for bad in ({}, "text", None, 42):
            self.assertEqual(sb.parse_segments(bad, "dQw4w9WgXcQ", ["sponsor"]), [])

    def test_overlapping_segments_of_one_category_are_merged(self) -> None:
        # Contributors submit slightly different bounds; skipping into the middle of an
        # overlapping pair produces a visible stutter.
        payload = [{"videoID": "dQw4w9WgXcQ", "segments": [
            {"category": "sponsor", "segment": [10, 25], "UUID": "a"},
            {"category": "sponsor", "segment": [24, 40], "UUID": "b"},
        ]}]
        parsed = sb.parse_segments(payload, "dQw4w9WgXcQ", ["sponsor"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual((parsed[0]["start"], parsed[0]["end"]), (10.0, 40.0))

    def test_different_categories_are_not_merged(self) -> None:
        payload = [{"videoID": "dQw4w9WgXcQ", "segments": [
            {"category": "sponsor", "segment": [10, 25], "UUID": "a"},
            {"category": "intro", "segment": [24, 40], "UUID": "b"},
        ]}]
        self.assertEqual(len(sb.parse_segments(payload, "dQw4w9WgXcQ", ["sponsor", "intro"])), 2)

    def test_segments_come_back_in_playback_order(self) -> None:
        payload = [{"videoID": "dQw4w9WgXcQ", "segments": [
            {"category": "sponsor", "segment": [90, 100], "UUID": "b"},
            {"category": "sponsor", "segment": [10, 20], "UUID": "a"},
        ]}]
        starts = [s["start"] for s in sb.parse_segments(payload, "dQw4w9WgXcQ", ["sponsor"])]
        self.assertEqual(starts, sorted(starts))


class FetchTests(unittest.TestCase):
    def _client(self, status: int, payload=None):
        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, params=None):
                return httpx.Response(status, json=payload if payload is not None else [],
                                      request=httpx.Request("GET", url))
        return _Client

    def test_a_404_means_no_segments_rather_than_an_error(self) -> None:
        with patch.object(sb.httpx, "Client", self._client(404)), \
             patch.object(sb, "get_settings", lambda: SimpleNamespace(sponsorblock_cache_seconds=0)):
            self.assertEqual(sb.fetch_segments("dQw4w9WgXcQ", ["sponsor"]), [])

    def test_a_server_error_is_raised(self) -> None:
        with patch.object(sb.httpx, "Client", self._client(503)), \
             patch.object(sb, "get_settings", lambda: SimpleNamespace(sponsorblock_cache_seconds=0)):
            with self.assertRaises(sb.SponsorBlockError):
                sb.fetch_segments("dQw4w9WgXcQ", ["sponsor"])

    def test_an_invalid_video_id_never_leaves_the_process(self) -> None:
        with patch.object(sb.httpx, "Client", self._client(200)):
            self.assertEqual(sb.fetch_segments("../../etc/passwd", ["sponsor"]), [])

    def test_no_categories_means_no_request(self) -> None:
        with patch.object(sb.httpx, "Client", self._client(200)):
            self.assertEqual(sb.fetch_segments("dQw4w9WgXcQ", []), [])

    def test_results_are_cached(self) -> None:
        calls = {"n": 0}

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, params=None):
                calls["n"] += 1
                return httpx.Response(200, json=[], request=httpx.Request("GET", url))

        sb.clear_cache()
        with patch.object(sb.httpx, "Client", _Client), \
             patch.object(sb, "get_settings", lambda: SimpleNamespace(sponsorblock_cache_seconds=600)):
            sb.fetch_segments("dQw4w9WgXcQ", ["sponsor"])
            sb.fetch_segments("dQw4w9WgXcQ", ["sponsor"])
        sb.clear_cache()
        self.assertEqual(calls["n"], 1)


class EmbedStylingTests(unittest.TestCase):
    """The markers live in the player embed, which is a different document."""

    def test_marker_styles_travel_into_the_embed(self) -> None:
        # The embed never loads the app stylesheet, so rules defined only there produced
        # marks that were in the DOM with the right positions and no colour or height.
        script = main._player_script()
        self.assertIn("EMBED_CSS", script)
        for rule in (".sb-marks {", ".sb-mark {", '.sb-mark[data-category="selfpromo"]', ".sb-skip {"):
            self.assertIn(rule, script)

    def test_the_app_stylesheet_no_longer_pretends_to_cover_them(self) -> None:
        css = main._home_styles()
        self.assertNotIn(".sb-mark {", css)
        self.assertNotIn(".sb-marks {", css)
        self.assertNotIn(".sb-skip {", css)

    def test_every_category_has_its_own_colour(self) -> None:
        script = main._player_script()
        for key in sponsorblock.CATEGORY_KEYS:
            if key == "sponsor":
                continue  # the base .sb-mark colour
            self.assertIn(f'[data-category="{key}"]', script)

    def test_invidious_internal_markers_stay_hidden(self) -> None:
        self.assertIn(".vjs-marker { display: none !important; }", main._player_script())

    def test_marks_sit_on_the_scrubber_not_the_hit_area(self) -> None:
        self.assertIn("vjs-progress-holder", main._player_script())


class CrossOriginHonestyTests(unittest.TestCase):
    """A third-party player cannot be skipped inside, and must not pretend otherwise."""

    def test_the_note_admits_sponsorblock_cannot_act(self) -> None:
        self.assertIn("SponsorBlock cannot skip in this player", main._player_script())

    def test_segments_are_fetched_even_when_the_player_is_cross_origin(self) -> None:
        # The note can only be honest if we know there were segments to skip.
        script = main._player_script()
        self.assertIn("function sbFetch()", script)
        self.assertIn("if (!source.same_origin)", script)

    def test_the_dead_in_frame_toast_is_gone(self) -> None:
        # The skip notice is raised by the watch page; a second implementation inside the
        # shell iframe had no room to render and was never called.
        self.assertNotIn("function sbToast", main._player_script())
