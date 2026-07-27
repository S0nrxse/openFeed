from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from openfeed import db, main


def _prefs(**overrides: object) -> dict[str, object]:
    """Build preferences through the same normalisation the database read uses."""
    return db.normalize_playback_preferences(overrides)


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "<video></video>") -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def get(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


def _reset_playback_state() -> None:
    main._probe_cache.clear()
    main._backend_state["failures"] = 0.0
    main._backend_state["degraded_until"] = 0.0
    main._source_health.clear()


class PlaybackProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_playback_state()

    tearDown = setUp

    def test_probe_result_is_cached_instead_of_re_requested(self) -> None:
        client = _FakeClient(_FakeResponse())
        with patch.object(main, "_invidious_client", return_value=client):
            self.assertEqual(main._embed_playable("abc"), (True, ""))
            self.assertEqual(main._embed_playable("abc"), (True, ""))
        self.assertEqual(client.calls, 1)

    def test_invidious_error_page_is_not_treated_as_playable(self) -> None:
        client = _FakeClient(_FakeResponse(200, "<html>Video unavailable</html>"))
        with patch.object(main, "_invidious_client", return_value=client):
            playable, reason = main._embed_playable("abc")
        self.assertFalse(playable)
        self.assertTrue(reason)

    def test_repeated_failures_short_circuit_further_probes(self) -> None:
        client = _FakeClient(error=RuntimeError("connection refused"))
        with patch.object(main, "_invidious_client", return_value=client):
            for index in range(main._BACKEND_TRIP_AFTER):
                main._embed_playable(f"video-{index}")
            self.assertTrue(main.playback_backend_degraded())
            calls_after_trip = client.calls
            # A brand new video must not pay for a request that is certain to fail.
            self.assertEqual(main._embed_playable("video-new")[0], False)
            self.assertEqual(client.calls, calls_after_trip)

    def test_a_success_closes_the_circuit_again(self) -> None:
        failing = _FakeClient(error=RuntimeError("down"))
        with patch.object(main, "_invidious_client", return_value=failing):
            for index in range(main._BACKEND_TRIP_AFTER):
                main._embed_playable(f"video-{index}")
        self.assertTrue(main.playback_backend_degraded())

        working = _FakeClient(_FakeResponse())
        with patch.object(main, "_invidious_client", return_value=working):
            self.assertEqual(main._embed_playable("fresh", use_cache=False), (True, ""))
        self.assertFalse(main.playback_backend_degraded())


class SourceHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_playback_state()

    tearDown = setUp

    def test_failures_back_off_exponentially(self) -> None:
        main.mark_source_result("invidious", False)
        first = main.source_health_snapshot("invidious")["cooldown_seconds"]
        main.mark_source_result("invidious", False)
        second = main.source_health_snapshot("invidious")["cooldown_seconds"]
        self.assertGreater(second, first)
        self.assertTrue(main.source_in_cooldown("invidious"))

    def test_cooldown_is_capped(self) -> None:
        for _ in range(20):
            main.mark_source_result("invidious", False)
        self.assertLessEqual(main.source_health_snapshot("invidious")["cooldown_seconds"], int(main._SOURCE_COOLDOWN_MAX))

    def test_a_success_clears_the_cooldown(self) -> None:
        main.mark_source_result("youtube", False)
        self.assertTrue(main.source_in_cooldown("youtube"))
        main.mark_source_result("youtube", True)
        self.assertFalse(main.source_in_cooldown("youtube"))
        self.assertEqual(main.source_health_snapshot("youtube")["status"], "healthy")

    def test_expired_cooldown_lets_the_source_back_in(self) -> None:
        main.mark_source_result("invidious", False)
        main._source_health["invidious"]["cooldown_until"] = 0.0
        self.assertFalse(main.source_in_cooldown("invidious"))


class PlaybackLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_playback_state()

    tearDown = setUp

    def _sources(self, video: str = "abc", *, response: _FakeResponse | None = None, ytdlp: bool = False, **prefs: object):
        """The ladder for one video.

        yt-dlp is held off by default so these cases keep testing the Invidious/YouTube
        ordering they were written for; YtdlpLadderTests covers the extra rung on its own.
        """
        client = _FakeClient(response or _FakeResponse())
        with patch.object(db, "get_playback_preferences", return_value=_prefs(**prefs)):
            with patch.object(main, "_invidious_client", return_value=client):
                with patch.object(main.ytdlp, "available", lambda: ytdlp):
                    return main._playback_sources(video)

    def test_invidious_leads_when_it_can_serve_the_video(self) -> None:
        sources, _ = self._sources()
        self.assertEqual(sources[0]["id"], "invidious")
        self.assertEqual(sources[-1]["id"], "youtube")

    def test_failed_probe_puts_the_fallback_first_and_keeps_invidious_last(self) -> None:
        sources, reason = self._sources(response=_FakeResponse(404, "not found"))
        self.assertEqual([source["id"] for source in sources], ["youtube", "invidious"])
        self.assertTrue(reason)

    def test_a_source_in_cooldown_sinks_but_is_never_dropped(self) -> None:
        main.mark_source_result("youtube", False)
        sources, _ = self._sources()
        ids = [source["id"] for source in sources]
        self.assertIn("youtube", ids)
        self.assertEqual(ids[-1], "youtube")

    def test_disabled_sources_are_left_out_entirely(self) -> None:
        sources, _ = self._sources(youtube_fallback=False, response=_FakeResponse(404, "nope"))
        self.assertEqual([source["id"] for source in sources], ["invidious"])

    def test_no_enabled_sources_reports_a_reason(self) -> None:
        sources, reason = self._sources(prefer_local=False, youtube_fallback=False)
        self.assertEqual(sources, [])
        self.assertIn("settings", reason)

    def test_public_instances_are_tried_before_youtube(self) -> None:
        sources, _ = self._sources(
            response=_FakeResponse(404, "not found"),
            public_invidious_enabled=True,
            public_invidious_urls=["https://yewtu.be"],
        )
        ids = [source["id"] for source in sources]
        self.assertLess(ids.index("invidious:yewtu.be"), ids.index("youtube"))

    def test_video_ids_are_url_encoded_into_source_urls(self) -> None:
        sources, _ = self._sources("a/b?c")
        self.assertTrue(all("a%2Fb%3Fc" in source["url"] for source in sources))


class InstanceUrlTests(unittest.TestCase):
    def test_bare_hosts_get_https_and_duplicates_collapse(self) -> None:
        self.assertEqual(
            db.normalize_instance_urls(["yewtu.be", "yewtu.be/", "https://yewtu.be", " "]),
            ["https://yewtu.be"],
        )

    def test_explicit_scheme_is_kept(self) -> None:
        self.assertEqual(db.normalize_instance_urls(["http://192.0.2.10:3000/"]), ["http://192.0.2.10:3000"])

    def test_the_list_is_bounded(self) -> None:
        many = [f"inst{index}.example" for index in range(20)]
        self.assertEqual(len(db.normalize_instance_urls(many)), 6)

    def test_junk_input_is_ignored(self) -> None:
        self.assertEqual(db.normalize_instance_urls(None), [])
        self.assertEqual(db.normalize_instance_urls(["", "   "]), [])


if __name__ == "__main__":
    unittest.main()


class SourceOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_playback_state()

    tearDown = setUp

    def _all(self, ytdlp: bool = False, **prefs: object):
        with patch.object(db, "get_playback_preferences", return_value=_prefs(**prefs)):
            with patch.object(main.ytdlp, "available", lambda: ytdlp):
                return main._all_playback_sources()

    def test_disabled_sources_are_still_listed_so_they_can_be_switched_back_on(self) -> None:
        sources = self._all(disabled_sources=["youtube"])
        youtube = next(source for source in sources if source["id"] == "youtube")
        self.assertFalse(youtube["enabled"])

    def test_only_enabled_sources_reach_the_player(self) -> None:
        with patch.object(db, "get_playback_preferences", return_value=_prefs(disabled_sources=["youtube"])):
            with patch.object(main.ytdlp, "available", lambda: False):
                ids = [source["id"] for source in main._configured_playback_sources()]
        self.assertNotIn("youtube", ids)

    def test_dragged_order_is_honoured(self) -> None:
        ids = [source["id"] for source in self._all(source_order=["youtube", "invidious"])]
        self.assertEqual(ids[:2], ["youtube", "invidious"])

    def test_unranked_sources_keep_their_natural_place(self) -> None:
        ids = [source["id"] for source in self._all(
            source_order=["youtube"],
            public_invidious_urls=["https://yewtu.be"],
        )]
        self.assertEqual(ids[0], "youtube")
        self.assertIn("invidious:yewtu.be", ids)

    def test_legacy_flags_migrate_into_the_disabled_list(self) -> None:
        preferences = db.normalize_playback_preferences({"youtube_fallback": False, "prefer_local": False})
        self.assertEqual(preferences["disabled_sources"], ["invidious", "youtube"])
        # The legacy flags predate yt-dlp and say nothing about it, so this asserts the
        # two sources they *do* cover are gone rather than that nothing is left.
        with patch.object(db, "get_playback_preferences", return_value=preferences):
            with patch.object(main.ytdlp, "available", lambda: False):
                self.assertEqual(main._configured_playback_sources(), [])


class YtdlpLadderTests(unittest.TestCase):
    """The yt-dlp rung: present only when the binary is, and demotable like any other."""

    def setUp(self) -> None:
        _reset_playback_state()

    tearDown = setUp

    def _sources(self, *, response: _FakeResponse | None = None, **prefs: object):
        client = _FakeClient(response or _FakeResponse())
        with patch.object(db, "get_playback_preferences", return_value=_prefs(**prefs)):
            with patch.object(main, "_invidious_client", return_value=client):
                with patch.object(main.ytdlp, "available", lambda: True):
                    return main._playback_sources("abc")

    def test_it_is_absent_when_yt_dlp_is_not_installed(self) -> None:
        client = _FakeClient(_FakeResponse())
        with patch.object(db, "get_playback_preferences", return_value=_prefs()):
            with patch.object(main, "_invidious_client", return_value=client):
                with patch.object(main.ytdlp, "available", lambda: False):
                    sources, _ = main._playback_sources("abc")
        self.assertNotIn("ytdlp", [source["id"] for source in sources])

    def test_invidious_still_leads_when_it_works(self) -> None:
        sources, _ = self._sources()
        self.assertEqual(sources[0]["id"], "invidious")

    def test_it_sits_between_invidious_and_youtube(self) -> None:
        # Extracting the stream ourselves keeps the viewer private, so it is tried before
        # handing them to YouTube's own embed.
        ids = [source["id"] for source in self._sources()[0]]
        self.assertLess(ids.index("ytdlp"), ids.index("youtube"))

    def test_it_leads_once_invidious_cannot_serve_the_video(self) -> None:
        ids = [source["id"] for source in self._sources(response=_FakeResponse(404, "nope"))[0]]
        self.assertEqual(ids[0], "ytdlp")
        self.assertEqual(ids[-1], "invidious")

    def test_it_can_be_switched_off_like_any_other_source(self) -> None:
        ids = [source["id"] for source in self._sources(disabled_sources=["ytdlp"])[0]]
        self.assertNotIn("ytdlp", ids)

    def test_it_points_at_our_own_player_and_is_same_origin(self) -> None:
        source = next(item for item in self._sources()[0] if item["id"] == "ytdlp")
        self.assertTrue(source["url"].startswith("/watch/abc/ytdlp"))
        self.assertEqual(source["same_origin"], "1")

    def test_a_cooled_down_ytdlp_sinks_but_is_not_dropped(self) -> None:
        main.mark_source_result("ytdlp", False)
        ids = [source["id"] for source in self._sources()[0]]
        self.assertIn("ytdlp", ids)
        self.assertGreater(ids.index("ytdlp"), ids.index("youtube"))


class DownloadedSourceTests(unittest.TestCase):
    """A file on disk leads the ladder, and only exists for videos that have one."""

    def setUp(self) -> None:
        _reset_playback_state()

    tearDown = setUp

    def _sources(self, downloaded: bool, **prefs: object):
        client = _FakeClient(_FakeResponse())
        with patch.object(db, "get_playback_preferences", return_value=_prefs(**prefs)):
            with patch.object(main, "_invidious_client", return_value=client):
                with patch.object(main.ytdlp, "available", lambda: True):
                    return main._playback_sources("abc", downloaded=downloaded)[0]

    def test_it_is_absent_when_nothing_was_downloaded(self) -> None:
        self.assertNotIn("downloaded", [s["id"] for s in self._sources(False)])

    def test_it_leads_when_a_file_exists(self) -> None:
        self.assertEqual(self._sources(True)[0]["id"], "downloaded")

    def test_a_saved_source_order_cannot_bury_it(self) -> None:
        # It is not a source anyone ever ranked, so it would otherwise sort to the end.
        ids = [s["id"] for s in self._sources(True, source_order=["youtube", "invidious", "ytdlp"])]
        self.assertEqual(ids[0], "downloaded")

    def test_it_points_at_the_local_player_and_is_same_origin(self) -> None:
        source = self._sources(True)[0]
        self.assertTrue(source["url"].startswith("/watch/abc/local"))
        self.assertEqual(source["same_origin"], "1")
