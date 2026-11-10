from __future__ import annotations

import unittest

from openfeed import db, main


def _video(video_id: str, channel: str, source: str = "subscription", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "youtube_video_id": video_id,
        "youtube_channel_id": channel,
        "source_kind": source,
    }
    row.update(extra)
    return row


class SpreadChannelsTests(unittest.TestCase):
    """One channel must not clog neighboring slots in a ranked shelf."""

    def _positions(self, ordered: list[dict[str, object]], channel: str) -> list[int]:
        return [index for index, row in enumerate(ordered) if row["youtube_channel_id"] == channel]

    def test_repeats_are_pushed_apart(self) -> None:
        ranked = [
            _video("f0", "fireship"), _video("a0", "ai-rev"), _video("o0", "openai"),
            _video("a1", "ai-exp"), _video("f1", "fireship"), _video("t0", "tldr"),
            _video("m0", "mkbhd"), _video("v0", "veritas"), _video("f2", "fireship"),
            _video("c0", "coldcut"), _video("l0", "linus"), _video("s0", "stuff"),
        ]
        ordered = main._spread_channels(ranked)
        self.assertEqual(len(ordered), len(ranked), "spreading reorders, it never drops")
        positions = self._positions(ordered, "fireship")
        self.assertEqual(len(positions), 3)
        for first, second in zip(positions, positions[1:]):
            self.assertGreaterEqual(second - first, 5)

    def test_top_pick_survives_and_order_stays_merit_led(self) -> None:
        ranked = [_video("f0", "fireship"), _video("a0", "ai-rev"), _video("f1", "fireship")]
        ordered = main._spread_channels(ranked)
        self.assertEqual(ordered[0]["youtube_video_id"], "f0")

    def test_single_channel_pool_still_fills(self) -> None:
        ranked = [_video(f"f{index}", "fireship") for index in range(6)]
        ordered = main._spread_channels(ranked)
        self.assertEqual([row["youtube_video_id"] for row in ordered], [f"f{index}" for index in range(6)])

    def test_videos_without_a_channel_are_unconstrained(self) -> None:
        ranked = [_video("u0", ""), _video("u1", ""), _video("f0", "fireship")]
        ordered = main._spread_channels(ranked)
        self.assertEqual([row["youtube_video_id"] for row in ordered], ["u0", "u1", "f0"])

    def test_even_split_relaxes_into_alternation(self) -> None:
        ranked = [_video(f"a{index}", "chan-a") for index in range(3)]
        ranked += [_video(f"b{index}", "chan-b") for index in range(3)]
        ordered = main._spread_channels(ranked)
        channels = [row["youtube_channel_id"] for row in ordered]
        self.assertEqual(len(ordered), 6, "a dominated pool still fills, nothing is dropped")
        for first, second in zip(channels, channels[1:]):
            self.assertNotEqual(first, second, "a 50/50 pool should alternate, never sit adjacent")

    def test_dedupe_spreads_recommended_shelves_by_default(self) -> None:
        videos = [
            _video("f0", "fireship"), _video("a0", "ai-rev"), _video("o0", "openai"),
            _video("a1", "ai-exp"), _video("f1", "fireship"), _video("t0", "tldr"),
            _video("m0", "mkbhd"), _video("v0", "veritas"), _video("f2", "fireship"),
            _video("c0", "coldcut"), _video("l0", "linus"), _video("s0", "stuff"),
        ]
        ordered = main._dedupe_section_videos(videos, set(), 12)
        positions = self._positions(ordered, "fireship")
        for first, second in zip(positions, positions[1:]):
            self.assertGreaterEqual(second - first, 5)

    def test_dedupe_leaves_chronological_shelves_alone_on_request(self) -> None:
        videos = [_video("f0", "fireship"), _video("f1", "fireship"), _video("a0", "ai-rev")]
        ordered = main._dedupe_section_videos(videos, set(), 3, spread_channels=False)
        self.assertEqual(
            [row["youtube_video_id"] for row in ordered], ["f0", "f1", "a0"]
        )


class BlendSubscriptionsTests(unittest.TestCase):
    def test_recommendations_lead_and_subscriptions_are_woven_in(self) -> None:
        primary = [_video(f"r{index}", f"chan-r{index}", "discovery") for index in range(9)]
        subs = [_video(f"s{index}", f"chan-s{index}") for index in range(9)]

        blended = main._blend_subscription_videos(primary, subs, 9, every=3)
        ids = [row["youtube_video_id"] for row in blended]

        self.assertEqual(ids[0], "r0", "the top of the feed must stay recommendation-led")
        self.assertEqual([ids[2], ids[5], ids[8]], ["s0", "s1", "s2"])

    def test_no_duplicates_when_a_recommendation_is_already_a_subscription_upload(self) -> None:
        shared = _video("s0", "chan-a")
        primary = [shared, _video("r1", "chan-b", "discovery"), _video("r2", "chan-c", "discovery")]
        subs = [shared, _video("s1", "chan-d")]

        ids = [row["youtube_video_id"] for row in main._blend_subscription_videos(primary, subs, 4, every=3)]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("s1", ids)

    def test_injected_uploads_are_capped_per_channel(self) -> None:
        primary = [_video(f"r{index}", f"chan-r{index}", "discovery") for index in range(12)]
        subs = [_video(f"s{index}", "chan-loud") for index in range(12)]

        blended = main._blend_subscription_videos(primary, subs, 18, every=2)
        loud = [row for row in blended if row["youtube_channel_id"] == "chan-loud"]

        self.assertLessEqual(len(loud), 3)

    def test_blend_survives_an_empty_subscription_pool(self) -> None:
        primary = [_video(f"r{index}", f"chan-r{index}", "discovery") for index in range(4)]

        ids = [row["youtube_video_id"] for row in main._blend_subscription_videos(primary, [], 4, every=3)]

        self.assertEqual(ids, ["r0", "r1", "r2", "r3"])

    def test_cadence_follows_feed_preferences(self) -> None:
        self.assertEqual(main._blend_cadence({"prioritize_subscriptions": True, "discovery_level": 0.35}), 3)
        self.assertEqual(main._blend_cadence({"prioritize_subscriptions": False, "discovery_level": 0.35}), 4)
        self.assertEqual(main._blend_cadence({"prioritize_subscriptions": False, "discovery_level": 0.9}), 5)


class CatchUpShelfTests(unittest.TestCase):
    def test_variants_slice_the_pool_differently(self) -> None:
        pool = [
            _video("quick", "chan-a", duration_seconds=300),
            _video("long", "chan-b", duration_seconds=2400),
            _video("watched", "chan-c", duration_seconds=300, percent_completed=0.5),
        ]

        self.assertEqual([row["youtube_video_id"] for row in main._catch_up_videos("quick", pool, "local")], ["quick"])
        self.assertEqual([row["youtube_video_id"] for row in main._catch_up_videos("long", pool, "local")], ["long"])

    def test_rotation_picks_a_variant_with_enough_videos(self) -> None:
        pool = [_video(f"v{index}", f"chan-{index}", duration_seconds=300, is_read=False) for index in range(6)]

        section = main._catch_up_section(pool, set(), "local", seed=0, page=0)

        self.assertIsNotNone(section)
        self.assertEqual(section["kind"], "catch-up")
        self.assertGreaterEqual(len(section["videos"]), 3)

    def test_no_shelf_without_a_subscription_pool(self) -> None:
        self.assertIsNone(main._catch_up_section([], set(), "local", seed=0, page=0))

    def _deep_pool(self) -> list[dict[str, object]]:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        pool = []
        for index in range(160):
            pool.append(_video(
                f"v{index:03d}", f"chan-{index % 23}", duration_seconds=[300, 900, 2400][index % 3],
                is_read=index % 2 == 0, published_at=now - timedelta(days=index % 30, hours=1),
            ))
        return pool

    def test_catch_up_shelves_never_repeat_a_video_across_pages(self) -> None:
        pool = self._deep_pool()
        original = main._favorite_channel_keys
        main._favorite_channel_keys = lambda user_key, limit=6: ["chan-1", "chan-2"]
        try:
            for seed in range(6):
                shown: dict[str, int] = {}
                titles = []
                for page in range(0, 25):
                    section = main._catch_up_section(pool, set(), "local", seed=seed, page=page)
                    if not main._is_catch_up_page(page, seed):
                        continue
                    if section is None:
                        continue
                    titles.append(section["title"])
                    for row in section["videos"]:
                        video_id = row["youtube_video_id"]
                        self.assertNotIn(video_id, shown, f"seed {seed}: {video_id} on page {shown.get(video_id)} and {page}")
                        shown[video_id] = page
                # Consecutive catch-up shelves are different variants, not the same one
                # every third page.
                self.assertGreater(len(set(titles[:4])), 1, f"seed {seed}: {titles}")
        finally:
            main._favorite_channel_keys = original

    def test_later_catch_up_shelves_skip_the_subscription_shelf_head(self) -> None:
        pool = self._deep_pool()
        head = {row["youtube_video_id"] for row in pool[:main._CATCH_UP_SUBSCRIPTION_HEAD]}
        plan = main._catch_up_plan(pool, "local", seed=1, page=12)
        for page, entry in plan.items():
            if page == 0:
                continue
            self.assertFalse(head & {row["youtube_video_id"] for row in entry["videos"]}, page)

    def test_a_page_duplicate_is_dropped_without_changing_the_plan(self) -> None:
        pool = self._deep_pool()
        planned = main._catch_up_plan(pool, "local", seed=0, page=0)[0]["videos"]
        seen = {planned[0]["youtube_video_id"]}
        section = main._catch_up_section(pool, seen, "local", seed=0, page=0)
        self.assertEqual([row["youtube_video_id"] for row in section["videos"]], [row["youtube_video_id"] for row in planned[1:]])


class SubscriptionScopingTests(unittest.TestCase):
    def test_subscription_filter_requires_a_live_subscription(self) -> None:
        row = {"source_kind": "subscription", "is_subscribed_channel": False}

        self.assertFalse(db._row_matches_feed_filter(row, "subscription"))
        self.assertTrue(db._row_matches_feed_filter({"source_kind": "subscription", "is_subscribed_channel": True}, "subscription"))

    def test_subscription_queries_check_this_users_subscriptions(self) -> None:
        self.assertIn("feeds", db.ACTIVE_SUBSCRIPTION_SQL)
        self.assertIn("user_subscriptions", db.ACTIVE_SUBSCRIPTION_SQL)
        self.assertEqual(db.ACTIVE_SUBSCRIPTION_SQL.count("%s"), 1)
        # Never NULL, and no "nobody subscribed to anything, so show everything" branch:
        # that would hand one person's feed to a new account with no subscriptions.
        self.assertTrue(db.ACTIVE_SUBSCRIPTION_SQL.startswith("COALESCE("))
        self.assertNotIn("NOT EXISTS", db.ACTIVE_SUBSCRIPTION_SQL)


if __name__ == "__main__":
    unittest.main()
