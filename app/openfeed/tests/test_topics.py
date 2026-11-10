from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import topics


class TokenizerTests(unittest.TestCase):
    def test_stopwords_and_short_tokens_are_dropped(self) -> None:
        # "the", "best", "video" and "subscribe" are all stopwords; only the subject survives.
        self.assertEqual(topics.tokenize("The best video about SUBSCRIBE dozers"), ["dozers"])

    def test_lists_are_accepted_alongside_strings(self) -> None:
        self.assertIn("dozer", topics.tokenize(["Dozer", "Restoration"]))

    def test_digits_alone_are_not_terms(self) -> None:
        self.assertNotIn("2024", topics.tokenize("built in 2024"))


class BoilerplateTests(unittest.TestCase):
    """Promo lines poison the profile in a way IDF cannot catch."""

    DESC = (
        "Restoring a CAT D8 dozer that sat for thirty years.\n"
        "Check Out the Merch Store! https://DieselCreek.com/\n"
        "Follow me on Instagram @dieselcreek\n"
        "We dig out the tracks and get it running."
    )

    def test_lines_carrying_urls_or_handles_are_removed(self) -> None:
        prose = topics._description_prose(self.DESC)
        self.assertIn("Restoring a CAT D8 dozer", prose)
        self.assertIn("dig out the tracks", prose)
        self.assertNotIn("Merch Store", prose)
        self.assertNotIn("Instagram", prose)

    def test_subject_terms_survive_and_promo_terms_do_not(self) -> None:
        terms = set(topics.document_terms({"title": "CAT D8", "summary": self.DESC, "tags": [], "author": "Diesel Creek"}))
        self.assertIn("dozer", terms)
        self.assertNotIn("dieselcreek", terms)


class VectorTests(unittest.TestCase):
    IDF = {"dozer": 4.0, "restoration": 3.0, "cat": 2.0, "space": 4.0, "physics": 3.5}

    def _vector(self, title: str, tags: list[str] | None = None) -> dict[str, float]:
        row = {"title": title, "summary": "", "tags": tags or [], "author": ""}
        return topics.compute_vector(row, self.IDF)

    def test_a_vector_is_unit_length(self) -> None:
        vector = self._vector("Dozer restoration")
        norm = sum(value * value for value in vector.values()) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=3)

    def test_terms_outside_the_vocabulary_are_ignored(self) -> None:
        self.assertNotIn("zzzz", self._vector("Dozer zzzz"))

    def test_identical_documents_are_maximally_similar(self) -> None:
        left = self._vector("Dozer restoration")
        self.assertAlmostEqual(topics.cosine(left, left), 1.0, places=3)

    def test_unrelated_documents_do_not_match(self) -> None:
        self.assertEqual(topics.cosine(self._vector("Dozer restoration"), self._vector("Space physics")), 0.0)

    def test_partial_overlap_lands_between(self) -> None:
        score = topics.cosine(self._vector("Dozer restoration"), self._vector("Dozer cat"))
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_an_empty_side_is_not_a_match(self) -> None:
        self.assertEqual(topics.cosine({}, self._vector("Dozer")), 0.0)

    def test_a_long_document_cannot_outweigh_a_short_one_by_length(self) -> None:
        # Normalisation is what makes cosine meaningful across different description sizes.
        short = self._vector("Dozer")
        long = self._vector("Dozer " * 40)
        self.assertAlmostEqual(sum(v * v for v in long.values()) ** 0.5, 1.0, places=3)
        self.assertAlmostEqual(topics.cosine(short, long), 1.0, places=3)

    def test_explain_names_the_terms_that_drove_the_match(self) -> None:
        profile = {"dozer": 0.8, "cat": 0.2}
        self.assertEqual(topics.explain(self._vector("Dozer restoration"), profile)[:1], ["dozer"])


class ProfileTests(unittest.TestCase):
    ROWS = [
        {"topic_vector": {"dozer": 1.0}, "total_watch_seconds": 3600, "completed": True},
        {"topic_vector": {"space": 1.0}, "total_watch_seconds": 30, "completed": False},
    ]

    def test_watch_time_decides_the_profile(self) -> None:
        # An hour-long watch must outweigh a thirty-second bounce.
        with patch.object(topics.db, "watched_topic_vectors", return_value=self.ROWS):
            profile = topics.profile_vector("local")
        self.assertGreater(profile["dozer"], profile["space"])

    def test_the_profile_is_unit_length(self) -> None:
        with patch.object(topics.db, "watched_topic_vectors", return_value=self.ROWS):
            profile = topics.profile_vector("local")
        self.assertAlmostEqual(sum(v * v for v in profile.values()) ** 0.5, 1.0, places=3)

    def test_no_history_means_no_profile(self) -> None:
        with patch.object(topics.db, "watched_topic_vectors", return_value=[]):
            self.assertEqual(topics.profile_vector("local"), {})

    def test_a_string_encoded_vector_is_parsed(self) -> None:
        rows = [{"topic_vector": '{"dozer": 1.0}', "total_watch_seconds": 600, "completed": False}]
        with patch.object(topics.db, "watched_topic_vectors", return_value=rows):
            self.assertIn("dozer", topics.profile_vector("local"))


class AcquisitionTests(unittest.TestCase):
    """The library has to be able to grow beyond what it already contains."""

    def test_topic_search_uses_both_relevance_and_recency(self) -> None:
        # Popular and niche are different queries, not different rankings of one query.
        from openfeed import discovery

        sorts: list[str] = []
        with patch.object(discovery, "search_youtube", lambda q, page=1, sort="relevance": sorts.append(sort) or []), \
             patch.object(topics, "profile_vector", return_value={"dozer": 0.9, "welding": 0.5, "diesel": 0.4}):
            discovery.seed_from_topics("local", terms=2, per_term=2)
        self.assertIn("relevance", sorts)
        self.assertIn("upload_date", sorts)

    def test_topic_search_does_nothing_without_a_profile(self) -> None:
        from openfeed import discovery

        with patch.object(topics, "profile_vector", return_value={}):
            result = discovery.seed_from_topics("local")
        self.assertEqual(result["reason"], "no_profile")

    def test_channel_ids_keep_their_case(self) -> None:
        # YouTube channel ids are case-sensitive; the cached profile lowercases them, which
        # made every seeded fetch 404.
        from openfeed import discovery

        seen: list[str] = []
        rows = [{"youtube_channel_id": "UCkdgAA0rfK7lG5dv4o__Paw", "title": "Alberta Tech", "watch_count": 9, "last_watched_at": None}]
        with patch.object(discovery.db, "takeout_channels_by_affinity", return_value=rows), \
             patch.object(discovery.db, "channel_video_counts", return_value={}), \
             patch.object(discovery, "fetch_channel_uploads", lambda cid, page=1: seen.append(cid) or []):
            discovery.seed_from_takeout("local", channels=1)
        self.assertEqual(seen, ["UCkdgAA0rfK7lG5dv4o__Paw"])

    def test_well_stocked_channels_are_skipped(self) -> None:
        from openfeed import discovery

        rows = [{"youtube_channel_id": "UCabc", "title": "x", "watch_count": 9, "last_watched_at": None}]
        with patch.object(discovery.db, "takeout_channels_by_affinity", return_value=rows), \
             patch.object(discovery.db, "channel_video_counts", return_value={"ucabc": 20}), \
             patch.object(discovery, "fetch_channel_uploads", side_effect=AssertionError("should not fetch")):
            result = discovery.seed_from_takeout("local", channels=5)
        self.assertEqual(result["channels"], 0)

    def test_one_dead_source_does_not_abort_the_growth_pass(self) -> None:
        from openfeed import discovery

        with patch.object(discovery, "seed_from_takeout", side_effect=RuntimeError("boom")), \
             patch.object(discovery, "seed_from_topics", return_value={"ok": True, "stored": 4}), \
             patch.object(discovery, "discover_related_videos", return_value={"ok": True, "stored": 2}), \
             patch.object(discovery, "seed_trending", return_value={"ok": True, "stored": 1}):
            report = discovery.grow_library("local")
        self.assertFalse(report["takeout_channels"]["ok"])
        self.assertEqual(report["stored_total"], 7)


class SubscriptionFeedTests(unittest.TestCase):
    """A date-ordered feed only works if the videos carry dates."""

    def test_publish_time_survives_normalisation(self) -> None:
        from openfeed import discovery

        video = discovery._normalize_related({"videoId": "dQw4w9WgXcQ", "title": "x", "published": 1750000000})
        self.assertIsNotNone(video["published_at"])
        self.assertEqual(video["published_at"].year, 2025)

    def test_it_is_a_datetime_not_a_unix_int(self) -> None:
        # upsert binds this straight into a TIMESTAMPTZ; an int there is a type error.
        from datetime import datetime
        from openfeed import discovery

        video = discovery._normalize_related({"videoId": "dQw4w9WgXcQ", "title": "x", "published": 1750000000})
        self.assertIsInstance(video["published_at"], datetime)

    def test_a_missing_or_junk_date_is_none_rather_than_epoch_zero(self) -> None:
        from openfeed import discovery

        for payload in ({"videoId": "a" * 11, "title": "x"},
                        {"videoId": "a" * 11, "title": "x", "published": 0},
                        {"videoId": "a" * 11, "title": "x", "published": "soon"}):
            self.assertIsNone(discovery._normalize_related(payload)["published_at"])

    def test_backfill_walks_every_subscription(self) -> None:
        from openfeed import discovery

        seen: list[str] = []
        subs = [{"youtube_channel_id": f"UC{i:022d}"} for i in range(3)]
        with patch.object(discovery.db, "list_followed_feeds", return_value=subs), \
             patch.object(discovery, "fetch_channel_uploads", lambda cid, page=1: seen.append(cid) or []):
            result = discovery.backfill_subscription_uploads(10)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result["channels"], 3)

    def test_one_dead_channel_does_not_stop_the_backfill(self) -> None:
        from openfeed import discovery

        subs = [{"youtube_channel_id": "UCbad"}, {"youtube_channel_id": "UCgood"}]

        def _fetch(cid, page=1):
            if cid == "UCbad":
                raise RuntimeError("404")
            return []

        with patch.object(discovery.db, "list_followed_feeds", return_value=subs), \
             patch.object(discovery, "fetch_channel_uploads", _fetch):
            result = discovery.backfill_subscription_uploads(10)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["channels"], 2)
