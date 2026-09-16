from __future__ import annotations

import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from openfeed import db, ryd


class VideoIdValidationTests(unittest.TestCase):
    """Only real YouTube ids ever reach the outbound request."""

    def test_a_well_formed_id_is_accepted(self) -> None:
        self.assertTrue(ryd.valid_video_id("CASfTzI_ca4"))
        self.assertTrue(ryd.valid_video_id("dQw4w9WgXcQ"))

    def test_wrong_length_is_rejected(self) -> None:
        self.assertFalse(ryd.valid_video_id("abc"))
        self.assertFalse(ryd.valid_video_id("dQw4w9WgXcQextra"))

    def test_injection_shaped_input_is_rejected(self) -> None:
        for bad in ("../../etc/pw", "a b c d e f g", "'; DROP--", "<script>xx", ""):
            self.assertFalse(ryd.valid_video_id(bad), bad)

    def test_the_endpoint_is_a_constant_not_a_setting(self) -> None:
        # Making the host configurable would turn a privacy switch into a way to point
        # this deployment's viewing interest at an arbitrary third party.
        self.assertEqual(ryd.RYD_ENDPOINT, "https://returnyoutubedislikeapi.com/votes")
        self.assertNotIn("ryd_endpoint", {field for field in dir(ryd.get_settings())})


class ResponseValidationTests(unittest.TestCase):
    def _payload(self, **overrides: object) -> dict:
        base = {"id": "dQw4w9WgXcQ", "likes": 100, "dislikes": 7, "viewCount": 900, "deleted": False}
        base.update(overrides)
        return base

    def test_a_good_response_keeps_only_the_three_fields_we_store(self) -> None:
        parsed = ryd.parse_votes(self._payload(), "dQw4w9WgXcQ")
        self.assertEqual(parsed, {"youtube_video_id": "dQw4w9WgXcQ", "dislikes": 7, "likes": 100, "view_count": 900})

    def test_a_response_for_a_different_video_is_discarded(self) -> None:
        # Otherwise a confused or hostile answer writes a count onto the wrong video.
        self.assertIsNone(ryd.parse_votes(self._payload(id="someotherid"), "dQw4w9WgXcQ"))

    def test_a_deleted_video_yields_nothing(self) -> None:
        self.assertIsNone(ryd.parse_votes(self._payload(deleted=True), "dQw4w9WgXcQ"))

    def test_a_missing_dislike_count_yields_nothing(self) -> None:
        self.assertIsNone(ryd.parse_votes({"id": "dQw4w9WgXcQ"}, "dQw4w9WgXcQ"))

    def test_negative_and_absurd_counts_are_refused(self) -> None:
        self.assertIsNone(ryd.parse_votes(self._payload(dislikes=-5), "dQw4w9WgXcQ"))
        self.assertIsNone(ryd.parse_votes(self._payload(dislikes=10**15), "dQw4w9WgXcQ"))

    def test_a_boolean_is_not_mistaken_for_a_number(self) -> None:
        self.assertIsNone(ryd.parse_votes(self._payload(dislikes=True), "dQw4w9WgXcQ"))

    def test_a_string_count_is_refused_rather_than_coerced(self) -> None:
        self.assertIsNone(ryd.parse_votes(self._payload(dislikes="7"), "dQw4w9WgXcQ"))

    def test_a_non_object_response_yields_nothing(self) -> None:
        for bad in ([], "text", None, 42):
            self.assertIsNone(ryd.parse_votes(bad, "dQw4w9WgXcQ"))

    def test_an_unusable_secondary_field_does_not_lose_the_dislike_count(self) -> None:
        parsed = ryd.parse_votes(self._payload(likes="lots", viewCount=None), "dQw4w9WgXcQ")
        self.assertEqual(parsed["dislikes"], 7)
        self.assertIsNone(parsed["likes"])


class OptInTests(unittest.TestCase):
    """Nothing is fetched unless both the deployment and the user allow it."""

    def _enabled(self, deployment: bool, user: bool) -> bool:
        with patch.object(ryd, "get_settings", lambda: SimpleNamespace(ryd_enabled=deployment)), \
             patch.object(db, "get_feed_preferences", return_value={"ryd_enabled": user}):
            return ryd.enabled()

    def test_off_by_default_for_the_user(self) -> None:
        self.assertFalse(self._enabled(deployment=True, user=False))

    def test_a_deployment_can_veto_it_entirely(self) -> None:
        self.assertFalse(self._enabled(deployment=False, user=True))

    def test_both_together_enable_it(self) -> None:
        self.assertTrue(self._enabled(deployment=True, user=True))

    def test_a_preference_lookup_failure_fails_closed(self) -> None:
        with patch.object(ryd, "get_settings", lambda: SimpleNamespace(ryd_enabled=True)), \
             patch.object(db, "get_feed_preferences", side_effect=RuntimeError("db down")):
            self.assertFalse(ryd.enabled())

    def test_refresh_does_nothing_while_disabled(self) -> None:
        with patch.object(ryd, "enabled", lambda user_key="local": False):
            result = ryd.refresh_missing()
        self.assertEqual(result, {"ok": False, "reason": "disabled", "updated": 0})


class ReadOnlyTests(unittest.TestCase):
    def test_the_module_never_submits_a_vote(self) -> None:
        # Local ratings stay local; there is deliberately no write path to RYD.
        source = (ryd.__file__ or "")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in ("client.post(", "client.put(", '"POST"', "method='POST'"):
            self.assertNotIn(forbidden, text)


class BatchTests(unittest.TestCase):
    def test_ids_are_deduplicated_and_capped(self) -> None:
        calls: list[str] = []

        def _fetch(video_id, client):
            calls.append(video_id)
            return None

        many = [f"vid{index:08d}" for index in range(100)]
        with patch.object(ryd, "fetch_one", _fetch), patch.object(ryd, "valid_video_id", lambda v: True):
            ryd.fetch_batch(many + many)
        self.assertEqual(len(calls), ryd.RYD_MAX_BATCH)
        self.assertEqual(len(set(calls)), len(calls))

    def test_invalid_ids_never_leave_the_process(self) -> None:
        calls: list[str] = []
        with patch.object(ryd, "fetch_one", lambda video_id, client: calls.append(video_id)):
            ryd.fetch_batch(["../../etc", "not an id", "dQw4w9WgXcQ"])
        self.assertEqual(calls, ["dQw4w9WgXcQ"])

    def test_one_failure_does_not_abandon_the_rest_of_the_batch(self) -> None:
        def _fetch(video_id, client):
            if video_id == "dQw4w9WgXcQ":
                raise ryd.RydError("boom")
            return {"youtube_video_id": video_id, "dislikes": 1, "likes": 2, "view_count": 3}

        with patch.object(ryd, "fetch_one", _fetch):
            results = ryd.fetch_batch(["dQw4w9WgXcQ", "CASfTzI_ca4"])
        self.assertEqual([item["youtube_video_id"] for item in results], ["CASfTzI_ca4"])

    def test_a_404_means_no_estimate_rather_than_an_error(self) -> None:
        class _Client:
            def get(self, url, params=None):
                return httpx.Response(404, request=httpx.Request("GET", url))

        with patch.object(ryd, "_throttle", lambda: None):
            self.assertIsNone(ryd.fetch_one("dQw4w9WgXcQ", _Client()))

    def test_a_server_error_is_raised_so_the_id_is_retried_later(self) -> None:
        class _Client:
            def get(self, url, params=None):
                return httpx.Response(503, request=httpx.Request("GET", url))

        with patch.object(ryd, "_throttle", lambda: None):
            with self.assertRaises(ryd.RydError):
                ryd.fetch_one("dQw4w9WgXcQ", _Client())

    def test_videos_answered_and_unanswered_are_both_stamped(self) -> None:
        # Without stamping the misses, every pass would re-ask about the same videos.
        stamped: dict[str, list[str]] = {}
        with patch.object(ryd, "enabled", lambda user_key="local": True), \
             patch.object(db, "videos_missing_dislikes", return_value=["aaaaaaaaaaa", "bbbbbbbbbbb"]), \
             patch.object(ryd, "fetch_batch", return_value=[{"youtube_video_id": "aaaaaaaaaaa", "dislikes": 3}]), \
             patch.object(db, "store_dislike_counts", return_value=1), \
             patch.object(db, "mark_dislikes_checked", lambda ids: stamped.setdefault("ids", list(ids))):
            result = ryd.refresh_missing()
        self.assertEqual(stamped["ids"], ["bbbbbbbbbbb"])
        self.assertEqual(result["updated"], 1)


class EstimateLabellingTests(unittest.TestCase):
    """An estimate must never be presented as a figure YouTube published."""

    def test_the_watch_page_query_selects_the_source_column(self) -> None:
        # Without this the page falls back to the official-count wording for an estimate.
        import inspect

        source = inspect.getsource(db.get_video_by_youtube_id)
        self.assertIn("v.dislike_source", source)

    def test_the_watch_page_distinguishes_the_two_wordings(self) -> None:
        from openfeed import main

        import inspect

        page = inspect.getsource(main.watch_video)
        self.assertIn("Estimated by Return YouTube Dislike", page)
        self.assertIn("dislike_source", page)


_NOW = datetime.now(timezone.utc)


class WatchTimeProfileTests(unittest.TestCase):
    """Watch time is normalised into shares so heavy and light weeks rank alike."""

    ROWS = [
        {"youtube_channel_id": "UC_a", "channel_title": "A", "author": "A", "tags": ["space", "physics"], "seconds": 6000, "completions": 3, "last_watched_at": _NOW},
        {"youtube_channel_id": "UC_b", "channel_title": "B", "author": "B", "tags": ["cooking"], "seconds": 2000, "completions": 0, "last_watched_at": _NOW},
        {"youtube_channel_id": "UC_c", "channel_title": "C", "author": "C", "tags": [], "seconds": 2000, "completions": 1, "last_watched_at": _NOW},
    ]

    def _profile(self, rows=None):
        db.clear_watch_time_cache()
        with patch.object(db, "connect", self._fake_connect(rows if rows is not None else self.ROWS)):
            return db.watch_time_profile("local")

    def _fake_connect(self, rows):
        class _Cursor:
            def execute(self, *args, **kwargs):
                pass

            def fetchall(self):
                return rows

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Conn:
            def cursor(self):
                return _Cursor()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return lambda: _Conn()

    def test_channel_shares_sum_to_one(self) -> None:
        profile = self._profile()
        self.assertAlmostEqual(sum(profile["channels"].values()), 1.0)

    def test_the_most_watched_channel_gets_the_largest_share(self) -> None:
        profile = self._profile()
        self.assertAlmostEqual(profile["channels"]["uc_a"], 0.6)
        self.assertAlmostEqual(profile["channels"]["uc_b"], 0.2)

    def test_topics_inherit_the_watch_time_of_the_videos_carrying_them(self) -> None:
        profile = self._profile()
        self.assertAlmostEqual(profile["topics"]["space"], 0.6)
        self.assertAlmostEqual(profile["topics"]["cooking"], 0.2)

    def test_completions_are_tracked_separately_from_raw_minutes(self) -> None:
        # Finishing things is a stronger endorsement than accumulating abandoned minutes.
        self.assertEqual(self._profile()["completions"]["uc_a"], 3)

    def test_thin_history_is_discounted_so_one_video_cannot_take_over(self) -> None:
        # 10 minutes of history out of the hour needed for full weight.
        rows = [{"youtube_channel_id": "UC_a", "author": "A", "tags": ["space"], "seconds": 600, "completions": 0, "last_watched_at": _NOW}]
        self.assertAlmostEqual(self._profile(rows)["confidence"], 600 / db.WATCH_TIME_CONFIDENCE_SECONDS)

    def test_ample_history_earns_full_weight(self) -> None:
        rows = [{"youtube_channel_id": "UC_a", "author": "A", "tags": ["space"], "seconds": 99999, "completions": 4, "last_watched_at": _NOW}]
        self.assertEqual(self._profile(rows)["confidence"], 1.0)

    def test_no_history_means_no_confidence(self) -> None:
        self.assertEqual(self._profile(rows=[])["confidence"], 0.0)

    def test_no_watch_history_produces_empty_shares_not_a_divide_by_zero(self) -> None:
        profile = self._profile(rows=[])
        self.assertEqual(profile["channels"], {})
        self.assertEqual(profile["total_seconds"], 0)

    def test_rows_with_no_time_are_ignored(self) -> None:
        profile = self._profile(rows=[{"youtube_channel_id": "UC_x", "author": "X", "tags": [], "seconds": 0, "completions": 0, "last_watched_at": _NOW}])
        self.assertEqual(profile["channels"], {})

    def test_the_profile_is_cached_then_invalidated(self) -> None:
        calls = {"n": 0}
        rows = self.ROWS

        def _connect():
            calls["n"] += 1
            return self._fake_connect(rows)()

        db.clear_watch_time_cache()
        with patch.object(db, "connect", _connect):
            db.watch_time_profile("local")
            db.watch_time_profile("local")
            self.assertEqual(calls["n"], 1)
            db.clear_watch_time_cache("local")
            db.watch_time_profile("local")
        self.assertEqual(calls["n"], 2)


class WatchDeltaTests(unittest.TestCase):
    """The client sends elapsed time, because current_seconds restarts each page open."""

    def _stored_delta(self, delta: object) -> float:
        """Run update_playback_progress and return the delta it bound into the INSERT."""
        captured: dict[str, object] = {}

        class _Cursor:
            def execute(self, sql, params=None):
                if params and "INSERT INTO playback_progress" in sql:
                    captured["params"] = params

            def fetchone(self):
                return {"id": 1}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Conn:
            def cursor(self):
                return _Cursor()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(db, "connect", lambda: _Conn()), \
             patch.object(db, "_video_id_for_youtube", lambda cur, vid: 7):
            db.update_playback_progress("vid1", 10, 100, "progress", {}, "local", watch_delta_seconds=delta)
        params = list(captured["params"])
        # Anchored to the JSON metadata blob rather than counted from either end, so
        # adding a bind parameter does not silently point this at the wrong value.
        raw_index = params.index("{}")
        return float(params[raw_index - 2])

    def test_a_normal_delta_is_stored_as_sent(self) -> None:
        self.assertEqual(self._stored_delta(42.5), 42.5)

    def test_a_wild_delta_is_clamped_rather_than_trusted(self) -> None:
        # A single report cannot plausibly carry more than an hour, and this feeds ranking.
        self.assertEqual(self._stored_delta(99999), 3600.0)

    def test_a_negative_delta_cannot_subtract_watch_time(self) -> None:
        self.assertEqual(self._stored_delta(-500), 0.0)

    def test_an_omitted_delta_adds_nothing(self) -> None:
        self.assertEqual(self._stored_delta(None), 0.0)


class ResumePointTests(unittest.TestCase):
    """Only a real reading from the player becomes a resume point."""

    def _stored_resume(self, resume: object, duration: object = 600) -> object:
        captured: dict[str, object] = {}

        class _Cursor:
            def execute(self, sql, params=None):
                if params and "INSERT INTO playback_progress" in sql:
                    captured["params"] = list(params)

            def fetchone(self):
                return {"id": 1}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Conn:
            def cursor(self):
                return _Cursor()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(db, "connect", lambda: _Conn()), \
             patch.object(db, "_video_id_for_youtube", lambda cur, vid: 7):
            db.update_playback_progress("vid1", 10, duration, "position", {}, "local", resume_seconds=resume)
        params = captured["params"]
        return params[params.index("{}") - 1]

    def test_a_mid_video_position_is_stored(self) -> None:
        self.assertEqual(self._stored_resume(240), 240.0)

    def test_the_opening_seconds_are_not_worth_resuming(self) -> None:
        self.assertIsNone(self._stored_resume(4))

    def test_a_finished_video_drops_its_mark(self) -> None:
        # Otherwise reopening it drops you on the closing seconds.
        self.assertIsNone(self._stored_resume(595, duration=600))

    def test_a_negative_reading_cannot_become_a_mark(self) -> None:
        self.assertIsNone(self._stored_resume(-30))

    def test_a_report_with_no_reading_leaves_the_stored_mark_alone(self) -> None:
        # A dwell-only report from the watch page must not erase a real position.
        self.assertIsNone(self._stored_resume(None))


class SourceStartTests(unittest.TestCase):
    """Each backend spells the start offset differently."""

    def _url(self, kind: str, start: int) -> str:
        from openfeed import main

        return main._source_url({"kind": kind, "base": "https://yewtu.be"}, "abc123", "1", start)

    def test_invidious_uses_t(self) -> None:
        self.assertEqual(self._url("invidious-local", 90), "/embed/abc123?autoplay=1&t=90")

    def test_youtube_uses_start(self) -> None:
        self.assertIn("&start=90", self._url("youtube", 90))
        self.assertNotIn("&t=90", self._url("youtube", 90))

    def test_the_ytdlp_player_gets_it_too(self) -> None:
        self.assertEqual(self._url("ytdlp", 90), "/watch/abc123/ytdlp?autoplay=1&t=90")

    def test_zero_adds_nothing(self) -> None:
        self.assertEqual(self._url("invidious-local", 0), "/embed/abc123?autoplay=1")
        self.assertNotIn("start=", self._url("youtube", 0))


if __name__ == "__main__":
    unittest.main()


class CoverBatchTests(unittest.TestCase):
    """A lone lookup would point straight at what is being watched."""

    def _run(self, others, video_id="dQw4w9WgXcQ", **kw):
        sent = {}

        def _fetch(ids):
            sent["ids"] = list(ids)
            return []

        with patch.object(ryd, "enabled", lambda user_key="local": True), \
             patch.object(db, "videos_missing_dislikes", return_value=others), \
             patch.object(ryd, "fetch_batch", _fetch), \
             patch.object(db, "store_dislike_counts", return_value=0), \
             patch.object(db, "mark_dislikes_checked", lambda ids: None):
            result = ryd.cover_batch(video_id, kw.get("limit", 5))
        return result, sent.get("ids", [])

    def test_the_watched_video_is_included_in_the_batch(self) -> None:
        _, ids = self._run(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        self.assertIn("dQw4w9WgXcQ", ids)

    def test_it_is_hidden_among_other_videos(self) -> None:
        _, ids = self._run(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        self.assertGreater(len(ids), 1)
        self.assertTrue(set(ids) - {"dQw4w9WgXcQ"})

    def test_a_lone_request_is_refused_because_it_would_have_no_cover(self) -> None:
        result, ids = self._run([])
        self.assertEqual(result["reason"], "no_cover")
        self.assertEqual(ids, [])

    def test_the_batch_respects_the_size_limit(self) -> None:
        _, ids = self._run([f"vid{i:08d}" for i in range(50)], limit=5)
        self.assertEqual(len(ids), 5)

    def test_the_target_is_not_duplicated_by_the_pending_list(self) -> None:
        _, ids = self._run(["dQw4w9WgXcQ", "aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual(ids.count("dQw4w9WgXcQ"), 1)

    def test_the_order_is_not_predictable(self) -> None:
        # A fixed position in the sequence would give the game away.
        others = [f"vid{i:08d}" for i in range(30)]
        positions = {self._run(others, limit=12)[1].index("dQw4w9WgXcQ") for _ in range(12)}
        self.assertGreater(len(positions), 1)

    def test_nothing_runs_while_the_feature_is_off(self) -> None:
        with patch.object(ryd, "enabled", lambda user_key="local": False):
            self.assertEqual(ryd.cover_batch("dQw4w9WgXcQ")["reason"], "disabled")
            self.assertFalse(ryd.schedule_cover_batch("dQw4w9WgXcQ"))

    def test_an_invalid_id_never_schedules_anything(self) -> None:
        with patch.object(ryd, "enabled", lambda user_key="local": True):
            self.assertFalse(ryd.schedule_cover_batch("../../etc/passwd"))

    def test_the_same_video_cannot_start_two_batches_at_once(self) -> None:
        ryd._inflight.add("dQw4w9WgXcQ")
        try:
            with patch.object(ryd, "enabled", lambda user_key="local": True):
                self.assertEqual(ryd.cover_batch("dQw4w9WgXcQ")["reason"], "already_running")
                self.assertFalse(ryd.schedule_cover_batch("dQw4w9WgXcQ"))
        finally:
            ryd._inflight.discard("dQw4w9WgXcQ")

    def test_the_inflight_marker_is_released_even_when_the_fetch_raises(self) -> None:
        with patch.object(ryd, "enabled", lambda user_key="local": True), \
             patch.object(db, "videos_missing_dislikes", return_value=["aaaaaaaaaaa"]), \
             patch.object(ryd, "fetch_batch", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ryd.cover_batch("dQw4w9WgXcQ")
        self.assertNotIn("dQw4w9WgXcQ", ryd._inflight)


class DisclosureTests(unittest.TestCase):
    def test_the_settings_copy_admits_that_opening_a_video_triggers_a_lookup(self) -> None:
        # The note used to claim lookups never happen on open. That stopped being true.
        from openfeed import main

        from openfeed.tests.support import offline_db

        with offline_db():
            html = main._settings_dialog(user={"authenticated": False}, user_key="local")
        self.assertIn("shuffled into a batch", html)
        self.assertNotIn("never\nwhen you open one", html)


class PlaceholderZeroTests(unittest.TestCase):
    """A 0 from YouTube's metadata means "not published", not "nobody disliked it"."""

    def test_enrichment_does_not_store_a_zero_dislike_count(self) -> None:
        from openfeed import enrichment

        mapped = enrichment._normalize_invidious({"dislikeCount": 0, "title": "x"})
        self.assertIsNone(mapped["dislike_count"])

    def test_a_real_dislike_count_still_survives(self) -> None:
        from openfeed import enrichment

        mapped = enrichment._normalize_invidious({"dislikeCount": 42, "title": "x"})
        self.assertEqual(mapped["dislike_count"], 42)

    def test_the_backfill_query_treats_a_placeholder_zero_as_missing(self) -> None:
        import inspect

        sql = inspect.getsource(db.videos_missing_dislikes)
        self.assertIn("v.dislike_count = 0 AND v.dislike_source IS DISTINCT FROM 'ryd'", sql)

    def test_a_zero_that_ryd_reported_is_kept(self) -> None:
        import inspect

        sql = inspect.getsource(db.videos_missing_dislikes)
        self.assertIn("IS DISTINCT FROM 'ryd'", sql)

    def test_the_watch_page_schedules_on_a_falsy_count_not_just_null(self) -> None:
        from openfeed import main
        import inspect

        page = inspect.getsource(main.watch_video)
        self.assertIn("if not dislike_raw and not video.get(\"dislike_fetched_at\")", page)


class WatchPageWiringTests(unittest.TestCase):
    """Names the watch page reaches for at request time must actually be imported.

    A missing import here is invisible to every other test in this file, because none of
    them execute watch_video — it needs a live database. This one shipped a 500 to the
    browser while the suite stayed green.
    """

    def test_every_module_watch_video_calls_is_imported(self) -> None:
        import ast
        import inspect
        from openfeed import main

        tree = ast.parse(inspect.getsource(main.watch_video))
        # Anything bound inside the function is a local, not a module reference.
        bound = {
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, (ast.comprehension,)) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)

        used = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        for name in sorted(used - bound):
            self.assertTrue(
                hasattr(main, name) or name in dir(__builtins__),
                f"watch_video references `{name}.…` but main does not define or import it",
            )

    def test_ryd_is_reachable_from_main(self) -> None:
        from openfeed import main

        self.assertTrue(hasattr(main, "ryd"))
        self.assertTrue(callable(main.ryd.schedule_cover_batch))


class ChannelCollisionTests(unittest.TestCase):
    """A placeholder channel row must not abort enrichment for the whole video."""

    def test_metadata_update_repoints_instead_of_stamping_a_duplicate_id(self) -> None:
        import inspect

        source = inspect.getsource(db.update_video_metadata)
        # The old code stamped youtube_channel_id onto whatever row the video pointed at,
        # which raised a unique violation whenever the canonical row already existed.
        self.assertIn("SELECT id FROM channels WHERE youtube_channel_id = %s", source)
        self.assertIn("UPDATE videos SET channel_id = %s", source)

    def test_the_original_stamp_still_runs_when_there_is_no_collision(self) -> None:
        import inspect

        source = inspect.getsource(db.update_video_metadata)
        self.assertIn("SET youtube_channel_id = COALESCE(NULLIF(%s, ''), c.youtube_channel_id)", source)


class RecencyDecayTests(unittest.TestCase):
    """Interests fade. Ranking used to treat a two-year-old watch like yesterday's."""

    def test_a_watch_today_carries_full_weight(self) -> None:
        self.assertAlmostEqual(db.recency_weight(datetime.now(timezone.utc)), 1.0, places=2)

    def test_one_half_life_halves_it(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(days=db.INTEREST_HALF_LIFE_DAYS)
        self.assertAlmostEqual(db.recency_weight(when), 0.5, places=2)

    def test_two_half_lives_quarter_it(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(days=db.INTEREST_HALF_LIFE_DAYS * 2)
        self.assertAlmostEqual(db.recency_weight(when), 0.25, places=2)

    def test_it_decays_rather_than_cutting_off(self) -> None:
        # A hard window makes a signal vanish overnight; taste does not work that way.
        old = db.recency_weight(datetime.now(timezone.utc) - timedelta(days=900))
        self.assertGreater(old, 0.0)
        self.assertLess(old, 0.01)

    def test_undated_history_counts_but_counts_less(self) -> None:
        weight = db.recency_weight(None)
        self.assertGreater(weight, 0.0)
        self.assertLess(weight, 1.0)

    def test_unparseable_timestamps_do_not_raise(self) -> None:
        self.assertGreater(db.recency_weight("not a date"), 0.0)

    def test_a_naive_timestamp_is_treated_as_utc(self) -> None:
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual(db.recency_weight(naive), 1.0, places=2)

    def test_equal_watching_favours_the_recent_channel(self) -> None:
        # The case described: binged a year ago, no longer of interest.
        stale = db.recency_weight(datetime.now(timezone.utc) - timedelta(days=400)) * 50
        fresh = db.recency_weight(datetime.now(timezone.utc) - timedelta(days=5)) * 50
        self.assertGreater(fresh, stale * 8)

    def test_age_can_be_outweighed_by_enough_watching(self) -> None:
        # Decay discounts an old interest, it does not erase it: a channel watched far
        # more heavily still competes, which is why this is a weight and not a filter.
        stale = db.recency_weight(datetime.now(timezone.utc) - timedelta(days=400)) * 400
        fresh = db.recency_weight(datetime.now(timezone.utc) - timedelta(days=5)) * 20
        self.assertGreater(stale, fresh)
