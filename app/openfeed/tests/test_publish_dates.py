from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from openfeed import discovery


def _seconds(moment: datetime) -> int:
    return int(moment.timestamp())


class FabricatedDateTests(unittest.TestCase):
    """Invidious list endpoints date a video to the fetch when they cannot read its age.

    They carry YouTube's relative text ("2 days ago") and decode it at request time, so a
    missing or unrecognised string decodes to *now*. Stored, that made a January episode
    the newest video on the channel and handed it the feed's recency boost as well.
    """

    def test_a_timestamp_equal_to_now_is_not_a_publish_date(self) -> None:
        now = datetime.now(tz=timezone.utc)
        self.assertIsNone(discovery._published_at({"published": _seconds(now)}))

    def test_nor_is_one_from_a_minute_ago(self) -> None:
        recent = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        self.assertIsNone(discovery._published_at({"published": _seconds(recent)}))

    def test_a_future_timestamp_is_rejected_too(self) -> None:
        ahead = datetime.now(tz=timezone.utc) + timedelta(hours=3)
        self.assertIsNone(discovery._published_at({"published": _seconds(ahead)}))

    def test_a_real_date_still_comes_through(self) -> None:
        # The case that actually broke: EP 174 published in January, served by the channel
        # listing as though it went up during the fetch.
        january = datetime(2026, 1, 5, tzinfo=timezone.utc)
        self.assertEqual(discovery._published_at({"published": _seconds(january)}), january)

    def test_yesterday_is_recent_but_not_suspicious(self) -> None:
        yesterday = datetime.now(tz=timezone.utc) - timedelta(days=1)
        parsed = discovery._published_at({"published": _seconds(yesterday)})
        self.assertIsNotNone(parsed)

    def test_missing_and_zero_stay_unknown(self) -> None:
        self.assertIsNone(discovery._published_at({}))
        self.assertIsNone(discovery._published_at({"published": 0}))
        self.assertIsNone(discovery._published_at({"published": "not a number"}))

    def test_the_window_is_short_enough_to_keep_genuine_uploads(self) -> None:
        # Wide enough to catch the fabrication, narrow enough that a real upload from an
        # hour ago is still dated.
        self.assertLessEqual(discovery._FABRICATED_DATE_WINDOW, timedelta(minutes=15))


class MissingViewCountTests(unittest.TestCase):
    """A list entry reports 0 views when YouTube rendered no count in that row."""

    def test_zero_views_from_a_listing_means_unknown(self) -> None:
        video = discovery._normalize_related({"videoId": "abc12345678", "viewCount": 0})
        self.assertIsNone(video["view_count"])

    def test_a_real_count_is_kept(self) -> None:
        video = discovery._normalize_related({"videoId": "abc12345678", "viewCount": 371790})
        self.assertEqual(video["view_count"], 371790)


class EnrichmentIsAuthoritativeTests(unittest.TestCase):
    """The single-video endpoint is the only route that reports a real timestamp."""

    def test_enrichment_overwrites_a_stored_date_rather_than_deferring_to_it(self) -> None:
        # Filling only when missing made a date fabricated at discovery permanent: the one
        # call able to correct it was the one forbidden from doing so.
        import inspect

        from openfeed import db

        source = inspect.getsource(db.update_video_metadata)
        self.assertIn("published_at = COALESCE(to_timestamp(NULLIF(%s, 0)), published_at)", source)
        self.assertNotIn("published_at = COALESCE(published_at, to_timestamp", source)


class DecodedDateDetectionTests(unittest.TestCase):
    """A decoded date carries the wall-clock of the fetch; a real one is midnight UTC."""

    def test_the_query_selects_on_the_wall_clock_signature(self) -> None:
        import inspect

        from openfeed import db

        source = inspect.getsource(db.list_videos_with_decoded_dates)
        self.assertIn("(published_at AT TIME ZONE 'UTC')::time <> '00:00:00'", source)

    def test_it_does_not_defer_to_the_ordinary_enrichment_queue(self) -> None:
        # That queue moves a handful per cycle and is shared with everything else needing
        # metadata, which left the backlog about two weeks out. Excluding the rows it
        # would eventually reach is what made this pass too slow to matter.
        import inspect

        from openfeed import db

        self.assertNotIn("metadata_fetched_at IS NOT NULL", inspect.getsource(db.list_videos_with_decoded_dates))

    def test_the_newest_are_corrected_first(self) -> None:
        # A decoded date reads as "just published", so those are the rows the recency
        # boost is actively promoting and the ones worth fixing soonest.
        import inspect

        from openfeed import db

        self.assertIn("ORDER BY published_at DESC", inspect.getsource(db.list_videos_with_decoded_dates))


class ContinuousRepairTests(unittest.TestCase):
    """The correction runs with the feed instead of being a one-off script."""

    def test_the_worker_runs_a_batch_each_cycle(self) -> None:
        import inspect

        from openfeed import worker

        self.assertIn("verify_publish_dates", inspect.getsource(worker.main))

    def test_refreshing_the_feed_also_re_reads_dates(self) -> None:
        import inspect

        from openfeed import main

        self.assertIn("verify_publish_dates", inspect.getsource(main.api_refresh_feed))

    def test_the_batch_is_bounded_and_paced(self) -> None:
        # Every lookup is a request Invidious forwards to YouTube. An unpaced loop makes
        # it answer 500 for everything, which reads as "video gone" and produced hundreds
        # of false failures before the pacing existed.
        from openfeed import enrichment

        self.assertLessEqual(enrichment.PUBLISH_DATE_BATCH, 100)
        self.assertGreaterEqual(enrichment.PUBLISH_DATE_GAP_SECONDS, 0.5)

    def test_an_unreachable_video_keeps_its_row_rather_than_being_guessed(self) -> None:
        from unittest.mock import patch

        from openfeed import db, enrichment

        rows = [{"id": 1, "youtube_video_id": "abc12345678", "published_at": None}]
        with patch.object(db, "list_videos_with_decoded_dates", return_value=rows), \
             patch.object(enrichment, "enrich_video", return_value={"ok": False}):
            result = enrichment.verify_publish_dates(1)
        self.assertEqual(result["corrected"], 0)
        self.assertEqual(result["candidates"], 1)


class TestSuitePortabilityTests(unittest.TestCase):
    """The suite has to run from a fresh clone, using the command the docs give."""

    def test_the_offline_helper_is_imported_absolutely(self) -> None:
        # A relative import of the helper only resolves when discovery is rooted at the
        # package (-t /). The README and CONTRIBUTING both document the plain
        # `discover -s openfeed/tests`, where tests load as top-level modules and it
        # raises ImportError. An absolute import works under both.
        #
        # Assembled rather than written out, so this file does not match itself.
        from pathlib import Path

        needle = "from ." + "support import"
        for path in Path(__file__).parent.glob("test_*.py"):
            self.assertNotIn(
                needle,
                path.read_text(),
                f"{path.name} uses a relative import that breaks the documented test command",
            )


if __name__ == "__main__":
    unittest.main()


class DatePrecedenceTests(unittest.TestCase):
    """Who is allowed to overwrite a stored publish date, and who only fills gaps."""

    def _sql(self, fn) -> str:
        import inspect

        return inspect.getsource(fn)

    def test_discovery_never_overwrites_a_stored_date(self) -> None:
        # Discovery reads list endpoints, where the date is decoded from relative text.
        # Letting it win meant a subscription backfill silently undid a completed repair,
        # rewriting thousands of corrected rows with approximations.
        from openfeed import db

        sql = self._sql(db.upsert_discovered_video)
        self.assertIn("published_at = COALESCE(videos.published_at, EXCLUDED.published_at)", sql)
        self.assertNotIn("published_at = COALESCE(EXCLUDED.published_at, videos.published_at)", sql)

    def test_enrichment_does_overwrite(self) -> None:
        # The opposite rule, for the opposite reason: this one reads the single-video
        # endpoint, the only route that reports a real timestamp.
        from openfeed import db

        self.assertIn(
            "published_at = COALESCE(to_timestamp(NULLIF(%s, 0)), published_at)",
            self._sql(db.update_video_metadata),
        )

    def test_the_two_rules_point_opposite_ways_on_purpose(self) -> None:
        # If these ever match, one of them is wrong: a fill-only enrichment cannot correct
        # anything, and an overwriting discovery undoes every correction.
        from openfeed import db

        discovery = "COALESCE(videos.published_at, EXCLUDED.published_at)" in self._sql(db.upsert_discovered_video)
        enrichment = "COALESCE(to_timestamp(NULLIF(%s, 0)), published_at)" in self._sql(db.update_video_metadata)
        self.assertTrue(discovery and enrichment)
