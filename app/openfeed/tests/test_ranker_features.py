"""Ranker v2 features: Up next, feed controls in graph mode, imported history, the Shorts and
deeper Home lanes, and the team-draft interleaving comparison.

No database: synthetic indexes and the fake cursor from test_ranker.
"""

from __future__ import annotations

import inspect
import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import numpy as np

from openfeed import db, ranker
from openfeed.tests.test_ranker import DAY, NOW, _records, _small_index, fake_v2


def _fingerprint() -> dict[str, str]:
    return {"events": "0:", "feedback": "0:", "progress": "0:", "subscriptions": "2:x", "profile": "p"}


# ---- Up next ---------------------------------------------------------------------------------
def _upnext_index(now: float = NOW) -> ranker.CorpusIndex:
    """Anchor 1 (channel ca) lists y2..y6 and an id not in the library, in that order.
    2 cb, 3 cc, 4 ca (a same-channel follow-up), 5 live, 6 cd dead; 7 lists y2 (one context away);
    8 and 9 are ca's newest and older uploads; 10 is a Short; 11.. filler that nothing links."""
    recs = []

    def add(vid, channel, related=(), age_days=30.0, views=50_000, duration=600, **extra):
        recs.append({"id": vid, "youtube_video_id": f"y{vid}", "channel_id": channel, "view_count": views,
                     "published_ts": now - age_days * DAY, "duration_seconds": duration, "related": list(related), **extra})

    add(1, "ca", ["y2", "y3", "y4", "y5", "y6", "ynot", "y10"])
    add(2, "cb")
    add(3, "cc")
    add(4, "ca")
    add(5, "ce", duration=0)
    add(6, "cd", views=10, age_days=60)
    add(7, "cf", ["y2"])
    add(8, "ca", age_days=1)
    add(9, "ca", age_days=3)
    add(10, "cg", duration=30)
    for vid in range(11, 31):
        add(vid, f"x{vid % 5}", age_days=vid)
    return ranker.index_from_records(recs, now)


def _upnext_entry(index, **kw):
    return ranker.prepare_user(index, ranker.UserSignals(subscribed_keys={"x1"}, mode="graph", **kw))


class UpNextTests(unittest.TestCase):
    def _ids(self, index, rows):
        return [int(index.ids[r]) for r in rows]

    def test_related_list_first_in_youtube_order_with_creator_slots(self) -> None:
        index = _upnext_index()
        rows, kinds = ranker.up_next_order(index, _upnext_entry(index), int(index.rows_for_ids([1])[0]), 10)
        ids = self._ids(index, rows)
        # 5 is live, 6 dead, 10 a Short: never Up next. 2, 3, 4 keep YouTube's order; 8 and 9 (the
        # creator's newest) take slots 2 and 5; 7 is reached through the shared context.
        self.assertEqual(ids[:2], [2, 3])
        self.assertEqual(ids[2], 8)
        self.assertEqual(ids[3], 4)
        self.assertEqual(ids[5], 9)
        self.assertIn(7, ids)
        self.assertNotIn(1, ids)
        self.assertFalse({5, 6, 10} & set(ids))
        self.assertEqual(kinds[0], "related")
        self.assertEqual(kinds[2], "creator")
        self.assertEqual(kinds[ids.index(7)], "walk")
        self.assertEqual(len(ids), len(set(ids)))

    def test_lane_exclusions_apply(self) -> None:
        index = _upnext_index()
        anchor = int(index.rows_for_ids([1])[0])
        entry = _upnext_entry(index, excluded={2}, muted_channels={"cc"}, muted_topic_ids={4})
        ids = self._ids(index, ranker.up_next_order(index, entry, anchor, 10)[0])
        self.assertFalse({2, 3, 4} & set(ids), "watched, muted creator and muted topic stay out")
        self.assertEqual(ids[0], 7, "the walk tier leads once the related list is used up")
        self.assertEqual(ids[2], 8, "the creator's newest keeps its slot")

    def test_channel_caps(self) -> None:
        recs = [{"id": 1, "youtube_video_id": "a", "channel_id": "ca", "view_count": 1e5, "published_ts": NOW - 9 * DAY,
                 "duration_seconds": 600, "related": [f"b{i}" for i in range(12)]}]
        for i in range(12):   # YouTube's list: twelve videos of one other channel
            recs.append({"id": 2 + i, "youtube_video_id": f"b{i}", "channel_id": "cb", "view_count": 1e5,
                         "published_ts": NOW - 9 * DAY, "duration_seconds": 600})
        for i in range(12):
            recs.append({"id": 20 + i, "youtube_video_id": f"f{i}", "channel_id": f"o{i}", "view_count": 1e5,
                         "published_ts": NOW - 9 * DAY, "duration_seconds": 600})
        index = ranker.index_from_records(recs, NOW)
        rows, _ = ranker.up_next_order(index, _upnext_entry(index), 0, 8)
        chans = [index.chan_keys[int(index.chan[r])] for r in rows]
        self.assertEqual(chans.count("cb"), ranker.UPNEXT_CHANNEL_CAP)
        self.assertEqual(len(rows), 8, "the rail still fills")

    def test_rows_through_the_fake_database(self) -> None:
        index = _upnext_index()
        state = {"fingerprint": _fingerprint(), "subs": ["x1"], "profile": {}}
        with fake_v2(index, state):
            rows = ranker.up_next_rows(1, 6, "u1")
            self.assertIsNone(ranker.up_next_rows(999, 6, "u1"), "an anchor not indexed yet falls back")
        self.assertEqual([r["id"] for r in rows][:2], [2, 3])
        self.assertEqual(rows[0]["recommendation_reason"], "Related to this video")
        self.assertEqual(rows[0]["ranker"]["model"], "v2-upnext")
        state["profile"] = {"ranker_mode": "classic"}
        state["fingerprint"] = {**_fingerprint(), "profile": "classic"}
        with fake_v2(index, state):
            self.assertIsNone(ranker.up_next_rows(1, 6, "u1"), "Classic keeps the old ordering")

    def test_list_related_videos_prefers_v2_and_falls_back(self) -> None:
        anchor = {"id": 1, "youtube_video_id": "y1", "title": "welding basics", "tags": []}
        v2 = [{"id": 2, "youtube_video_id": "y2"}]
        legacy = [{"id": 9, "youtube_video_id": "y9", "title": "welding again", "tags": []}]
        with patch.object(db, "get_video_by_youtube_id", return_value=anchor), \
             patch.object(db, "list_recommended_videos", return_value=legacy), \
             patch.object(db, "list_subscription_recommendations", return_value=[]):
            with patch.object(ranker, "up_next_rows", return_value=v2):
                self.assertEqual(db.list_related_videos("y1", 14, "u1"), v2)
            with patch.object(ranker, "up_next_rows", return_value=None):
                self.assertEqual([r["id"] for r in db.list_related_videos("y1", 14, "u1")], [9])
            with patch.object(ranker, "up_next_rows", side_effect=RuntimeError("boom")):
                self.assertEqual([r["id"] for r in db.list_related_videos("y1", 14, "u1")], [9])

    def test_index_keeps_related_lists_in_order(self) -> None:
        index = _upnext_index()
        row = int(index.rows_for_ids([1])[0])
        ctxs = index.rel_ctx[index.rel_ptr[row]:index.rel_ptr[row + 1]]
        owners = [int(index.ids[o]) if o >= 0 else None for o in index.self_owner[ctxs]]
        self.assertEqual(owners, [2, 3, 4, 5, 6, None, 10])


# ---- Feed controls in graph mode -------------------------------------------------------------
class FeedControlsTests(unittest.TestCase):
    def test_mapping_default_bounds_and_monotonic(self) -> None:
        self.assertEqual(ranker.feed_controls(), ranker.DEFAULT_CONTROLS)
        self.assertEqual(ranker.feed_controls(0.35, True), ranker.FeedControls(ranker.FRESH_SLOT_SHARE, 1.0, 0.0))
        self.assertAlmostEqual(ranker.feed_controls(0.0).fresh_share, ranker.FRESH_SHARE_MIN)
        top = ranker.feed_controls(1.0)
        self.assertAlmostEqual(top.fresh_share, ranker.FRESH_SHARE_MAX)
        self.assertAlmostEqual(top.wander_share, ranker.WANDER_SHARE_MAX)
        shares = [ranker.feed_controls(d / 20).fresh_share for d in range(21)]
        self.assertEqual(shares, sorted(shares))
        self.assertEqual(ranker.feed_controls(0.2).wander_share, 0.0, "no wandering at or below the default")
        self.assertEqual(ranker.feed_controls(7).fresh_share, ranker.FRESH_SHARE_MAX)
        self.assertEqual(ranker.feed_controls("x"), ranker.DEFAULT_CONTROLS)
        self.assertEqual(ranker.feed_controls(0.35, False).channel_factor, ranker.NO_PRIORITY_CHANNEL_FACTOR)

    def _mix(self, controls, watched_share=0.0):
        index = ranker.index_from_records(_records(n=600, seed=4, channels=40), NOW)
        s = ranker.seed_vector(index, {"c1", "c2", "c3"}, {})
        g = ranker.graph_scores(index, s)
        ok = ~index.blocked & (s == 0)
        mult = ranker.feel_multiplier(index, NOW)
        return ranker.graph_mix(index, g, np.zeros(index.n), ok, mult, watched_share, NOW, depth=200, controls=controls)

    def test_default_controls_are_the_measured_mix(self) -> None:
        index = ranker.index_from_records(_records(n=600, seed=4, channels=40), NOW)
        s = ranker.seed_vector(index, {"c1", "c2", "c3"}, {})
        g = ranker.graph_scores(index, s)
        ok = ~index.blocked & (s == 0)
        mult = ranker.feel_multiplier(index, NOW)
        plain = ranker.graph_mix(index, g, np.zeros(index.n), ok, mult, 0.0, NOW, depth=200)
        explicit = ranker.graph_mix(index, g, np.zeros(index.n), ok, mult, 0.0, NOW, depth=200, controls=ranker.feed_controls(0.35, True))
        np.testing.assert_array_equal(plain[0], explicit[0])
        np.testing.assert_array_equal(plain[1], explicit[1])

    def test_wander_and_priority_move_slots(self) -> None:
        base_rows, base_src = self._mix(ranker.DEFAULT_CONTROLS)
        far_rows, far_src = self._mix(ranker.feed_controls(1.0))
        low_rows, low_src = self._mix(ranker.feed_controls(0.35, False))
        top = slice(0, 60)
        self.assertGreater(int((far_src[top] == 1).sum()), int((base_src[top] == 1).sum()))
        self.assertGreater(int((far_src[top] == -1).sum()), int((base_src[top] == -1).sum()), "wandering reaches unreached videos")
        self.assertLess(int((low_src[top] == 2).sum()), int((base_src[top] == 2).sum()))
        for rows in (far_rows, low_rows):
            self.assertEqual(len(rows), len(set(rows.tolist())))

    def test_watched_share_is_untouched_by_controls(self) -> None:
        _, src = self._mix(ranker.feed_controls(1.0, False), watched_share=0.0)
        self.assertFalse((src == 0).any())

    def test_topics_in_signals_and_lanes(self) -> None:
        sig = ranker.signals_from_rows(["s1"], [], [], [], {"discovery_level": 0.8, "prioritize_subscriptions": False, "explore_enabled": True},
                                       topic_rows=[{"id": 10, "preferred": True, "muted": False}, {"id": 11, "preferred": False, "muted": True},
                                                   {"id": 12, "preferred": True, "muted": True}])
        self.assertEqual(sig.preferred_topic_ids, {10})
        self.assertEqual(sig.muted_topic_ids, {11, 12}, "muting wins over following")
        self.assertEqual((sig.discovery_level, sig.prioritize_subscriptions, sig.explore), (0.8, False, True))
        index = _small_index()
        entry = ranker.prepare_user(index, ranker.UserSignals(subscribed_keys={"s1", "s2"}, mode="graph",
                                                              preferred_topic_ids={14}, muted_topic_ids={11}))
        f, _ = ranker.lane_scores(index, entry, "recommended")
        self.assertFalse(np.isfinite(f[index.rows_for_ids([11])[0]]), "a muted topic is never a pick")
        self.assertEqual(entry.topic_mult[index.rows_for_ids([14])[0]], ranker.PREFERRED_TOPIC_BOOST)
        plain = ranker.rank_lane(index, ranker.prepare_user(index, ranker.UserSignals(subscribed_keys={"s1", "s2"}, mode="graph")), "recommended")
        boosted = ranker.rank_lane(index, entry, "recommended")
        row = int(index.rows_for_ids([14])[0])
        self.assertLessEqual(boosted.rows.tolist().index(row), plain.rows.tolist().index(row))

    def test_topic_terms_and_query(self) -> None:
        self.assertEqual(ranker.topic_terms("Machine Learning"), ["learning", "machine"])
        self.assertEqual(ranker.topic_terms("AI"), ["ai"])
        self.assertEqual(ranker.topic_terms("  "), [])

        class Cur:
            def __init__(self):
                self.calls = []

            def execute(self, q, p=None):
                self.calls.append((q, p))

            def fetchall(self):
                if len(self.calls) == 1:
                    return [{"topic": "cars", "preference": "follow"}, {"topic": "true crime", "preference": "mute"}]
                return [{"id": 3, "preferred": True, "muted": False}]

        cur = Cur()
        self.assertEqual(ranker._topic_rows(cur, "u1"), [{"id": 3, "preferred": True, "muted": False}])
        sql, params = cur.calls[1]
        self.assertEqual(sql.count("?&"), 4)
        self.assertEqual(params, [["cars"], ["crime", "true"], ["cars"], ["crime", "true"]])

    def test_caption_no_longer_says_classic_only(self) -> None:
        from openfeed import main

        caption = main._ranker_mode_caption()
        self.assertNotIn("Classic only", caption)
        self.assertIn("how far to wander", caption)


# ---- Imported Takeout history ------------------------------------------------------------------
class TakeoutSeedTests(unittest.TestCase):
    def test_weights_decay_and_stay_below_in_app_watches(self) -> None:
        import datetime as dt

        now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        rows = [{"id": 1, "watched_at": now}, {"id": 2, "watched_at": now - dt.timedelta(days=180)},
                {"id": 3, "watched_at": now - dt.timedelta(days=3000)}, {"id": 4, "watched_at": None}]
        weights, every = ranker.takeout_weights(rows, now.timestamp())
        self.assertEqual(every, {1, 2, 3, 4})
        self.assertAlmostEqual(weights[1], ranker.TAKEOUT_WEIGHT)
        self.assertAlmostEqual(weights[2], ranker.TAKEOUT_WEIGHT / 2)
        self.assertAlmostEqual(weights[3], ranker.TAKEOUT_WEIGHT * ranker.TAKEOUT_DECAY_FLOOR)
        self.assertAlmostEqual(weights[4], ranker.TAKEOUT_WEIGHT * ranker.TAKEOUT_DECAY_FLOOR)
        weakest_in_app = ranker.W_MID * ranker.EVENT_DECAY_FLOOR
        self.assertLess(max(weights.values()), weakest_in_app)

    def test_seed_cap_and_mass_cap(self) -> None:
        rows = [{"id": i, "watched_at": NOW - i} for i in range(1, ranker.TAKEOUT_MAX_SEEDS + 50)]
        weights, every = ranker.takeout_weights(rows, NOW)
        self.assertEqual(len(weights), ranker.TAKEOUT_MAX_SEEDS)
        self.assertEqual(len(every), ranker.TAKEOUT_MAX_SEEDS + 49)
        self.assertIn(1, weights, "the most recent seed")
        sig = ranker.UserSignals(positives={1: 1.0}, negatives={2}, takeout=weights)
        real, imported = ranker.event_seed_weights(sig)
        self.assertEqual(real, {1: 1.0})
        self.assertNotIn(1, imported, "an in-app positive keeps its own weight")
        self.assertNotIn(2, imported, "a video hidden here is not an imported positive")
        self.assertAlmostEqual(sum(imported.values()), min(ranker.TAKEOUT_MAX_MASS, sum(w for v, w in weights.items() if v not in (1, 2))))

    def test_imported_watches_seed_the_watched_walk_and_are_excluded(self) -> None:
        index = ranker.index_from_records(_records(n=400, seed=2, channels=40), NOW)
        imported_ids = [int(i) for i in index.ids[:30]]
        sig = ranker.signals_from_rows(["c1"], [], [], [], {}, takeout_rows=[{"id": v, "watched_at": NOW - 10 * DAY} for v in imported_ids])
        self.assertTrue(set(imported_ids) <= sig.excluded)
        entry = ranker.prepare_user(index, sig)
        self.assertTrue(entry.g_evt.any(), "the import seeds the watched walk")
        self.assertGreater(entry.watched_share, 0.0)
        self.assertLessEqual(entry.watched_share, ranker.TAKEOUT_MAX_MASS / (ranker.TAKEOUT_MAX_MASS + ranker.PERSONAL_SLOT_K) + 1e-9)
        f, _ = ranker.lane_scores(index, entry, "all")
        self.assertFalse(np.isfinite(f[index.rows_for_ids(imported_ids)]).any())
        none = ranker.prepare_user(index, ranker.signals_from_rows(["c1"], [], [], [], {}))
        self.assertFalse(none.g_evt.any())

    def test_an_imported_watch_weighs_less_than_an_in_app_one(self) -> None:
        index = ranker.index_from_records(_records(n=200, seed=3, channels=20), NOW)
        a, b = int(index.ids[5]), int(index.ids[6])
        sig = ranker.UserSignals(positives={a: ranker.W_MID}, positive_decay={a: ranker.EVENT_DECAY_FLOOR}, takeout={b: ranker.TAKEOUT_WEIGHT})
        entry = ranker.prepare_user(index, sig)
        self.assertGreater(entry.s_evt[index.rows_for_ids([a])[0]], entry.s_evt[index.rows_for_ids([b])[0]])


# ---- Shorts and the deeper Home lanes ---------------------------------------------------------
def _lane_index(now: float = NOW, n: int = 60) -> ranker.CorpusIndex:
    recs = []
    for vid in range(1, n + 1):
        channel = "s1" if vid <= 6 else f"o{vid % 7}"
        duration = 30 if vid % 5 == 0 else (2400 if vid % 3 == 0 else 600)
        age = vid if n <= 60 else (vid % 28) + 0.5
        recs.append({"id": vid, "youtube_video_id": f"v{vid}", "channel_id": channel, "view_count": 5000 + vid,
                     "published_ts": now - age * DAY, "duration_seconds": duration,
                     "related": ["v1", "v2"] if vid % 2 else ["v3"]})
    recs.append({"id": n + 1, "youtube_video_id": f"v{n + 1}", "channel_id": "o1", "duration_seconds": 0, "view_count": 9e5, "published_ts": now})
    return ranker.index_from_records(recs, now)


class ShortsAndDeepLaneTests(unittest.TestCase):
    def _entry(self, index, **kw):
        return ranker.prepare_user(index, ranker.UserSignals(subscribed_keys={"s1"}, mode="graph", **kw))

    def test_shorts_lane_holds_only_eligible_shorts(self) -> None:
        index = _lane_index()
        entry = self._entry(index, excluded={10})
        ranking = ranker.rank_lane(index, entry, "shorts")
        ids = [int(index.ids[r]) for r in ranking.rows]
        self.assertTrue(ids)
        self.assertTrue(all(i % 5 == 0 for i in ids), "Shorts only")
        self.assertNotIn(10, ids, "watched")
        self.assertNotIn(61, ids, "live")
        self.assertIn(5, ids, "a subscribed channel's Short is fine on the shelf")
        reached = [r for r, s in zip(ranking.rows.tolist(), ranking.source.tolist()) if s == 2]
        rest = [r for r, s in zip(ranking.rows.tolist(), ranking.source.tolist()) if s == -1]
        self.assertEqual(ranking.rows.tolist(), reached + rest, "walk-reached Shorts first")
        pub = index.pub_ts[rest]
        self.assertTrue(np.all(np.diff(pub) <= 0), "then newest first")

    def test_recent_and_long_lanes_filter(self) -> None:
        index = _lane_index()
        entry = self._entry(index)
        recent = ranker.lane_mask(index, entry, "recommended-recent", NOW)
        long_ = ranker.lane_mask(index, entry, "recommended-long", NOW)
        age = (NOW - index.pub_ts) / DAY
        self.assertTrue(recent.any() and long_.any())
        self.assertTrue(np.all(age[recent] <= ranker.RECENT_MAX_DAYS))
        self.assertTrue(np.all(index.dur[long_] >= ranker.LONG_FORM_MIN_SECONDS))
        self.assertFalse((recent | long_)[entry.sub_mask].any(), "subscriptions have their own shelves")
        self.assertFalse((recent | long_)[index.blocked].any())

    def test_home_partition_is_disjoint_and_keeps_order(self) -> None:
        rng = np.random.default_rng(5)
        pool = list(range(2000))
        orders = {"recommended": rng.permutation(pool).tolist(),
                  "recommended-recent": rng.permutation(pool[:700]).tolist(),
                  "recommended-long": rng.permutation(pool[300:1100]).tolist()}
        parts = ranker.home_partition(orders, depth=1500)
        self.assertEqual(parts["recommended"][:90], orders["recommended"][:90])
        for lane, rows in parts.items():
            pos = {r: i for i, r in enumerate(orders[lane])}
            self.assertEqual([pos[r] for r in rows], sorted(pos[r] for r in rows), lane)
        # Simulate Home's reads and check nothing is shown twice.
        shown: list[int] = []
        offsets = {lane: 0 for lane in orders}
        from openfeed import main

        for page in range(0, 40):
            if page == 0:
                lanes = [("recommended", 0, 60)]
            else:
                lane = {"recent": "recommended-recent", "long-form": "recommended-long"}.get(main._home_page_lane(page), main._home_page_lane(page))
                lanes = [(lane, main._home_lane_offset(page, main._home_page_lane(page)), main.HOME_PAGE_SIZE)]
            for lane, offset, size in lanes:
                shown += parts[lane][offset:offset + size]
                offsets[lane] = offset + size
        self.assertEqual(len(shown), len(set(shown)))

    def test_schedule_matches_the_home_page(self) -> None:
        from openfeed import main

        size = main.HOME_PAGE_SIZE
        self.assertEqual(ranker.HOME_SCHEDULE_HEAD, (("recommended", 3 * size),))
        lanes = [main._home_page_lane(p) for p in range(2, 6)]
        served = [{"recent": "recommended-recent", "long-form": "recommended-long"}.get(lane, lane) for lane in lanes]
        self.assertEqual(served, ["recommended-recent", "recommended-long", "recommended", "recommended"])
        self.assertEqual(ranker.HOME_SCHEDULE_CYCLE, (("recommended-recent", size), ("recommended-long", size), ("recommended", 2 * size)))
        source = inspect.getsource(main._compose_home_feed_sections)
        self.assertIn('"recommended-recent"', source)
        self.assertEqual(source.count("list_ranked_short_videos"), 2)
        self.assertNotIn("list_short_videos(", source)

    def test_lanes_through_the_fake_database(self) -> None:
        import time

        index = _lane_index(time.time(), n=400)
        state = {"fingerprint": _fingerprint(), "subs": ["s1"], "profile": {}}
        with fake_v2(index, state):
            shelf = db.list_recommended_videos(90, 0, "recommended", "u1")
            recent = db.list_recommended_videos(30, 0, "recommended-recent", "u1")
            long_ = db.list_recommended_videos(30, 0, "recommended-long", "u1")
            deeper = db.list_recommended_videos(60, 90, "recommended", "u1")
            shorts = db.list_ranked_short_videos(5, "u1")
        self.assertTrue(recent and long_ and deeper)
        self.assertTrue(all(r["ranker"]["lane"] == "recommended-recent" for r in recent))
        ids = [r["id"] for rows in (shelf, recent, long_, deeper) for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "Home never meets a video in two lanes")
        self.assertTrue(shorts and all(r["ranker"]["lane"] == "shorts" for r in shorts))

    def test_fallbacks_keep_the_old_code(self) -> None:
        with patch.object(ranker, "recommended_rows", return_value=None), \
             patch.object(db, "list_short_videos", return_value=[{"id": 1}]) as old:
            self.assertEqual(db.list_ranked_short_videos(14, "u1", 28), [{"id": 1}])
            old.assert_called_once_with(14, "u1", 28)
        rows = [{"id": 1, "duration_seconds": 600, "is_subscribed_channel": True}, {"id": 2, "duration_seconds": 600}]
        with patch.object(ranker, "recommended_rows", return_value=None), \
             patch.object(db, "_list_recommended_videos_classic", return_value=rows) as classic:
            self.assertEqual([r["id"] for r in db.list_recommended_videos(30, 0, "recommended-long", "u1")], [2])
            self.assertEqual(classic.call_args[0][2], "long-form")
        self.assertEqual(ranker.recommended_rows(5, 0, "recent", "u1"), None, "the filter chips stay classic")


# ---- Interleaving -------------------------------------------------------------------------------
class TeamDraftTests(unittest.TestCase):
    def test_properties(self) -> None:
        rng = np.random.default_rng(1)
        a = rng.permutation(300).tolist()
        b = rng.permutation(300).tolist()
        rows, teams = ranker.team_draft(a, b, 300, "u:2026-11-08")
        self.assertEqual((rows, teams), ranker.team_draft(a, b, 300, "u:2026-11-08"), "deterministic per seed")
        self.assertNotEqual(teams, ranker.team_draft(a, b, 300, "u:2026-11-09")[1], "a new day, a new draft")
        self.assertEqual(sorted(rows), list(range(300)))
        for prefix in range(1, 300):
            self.assertLessEqual(abs(teams[:prefix].count(0) - teams[:prefix].count(1)), 1)
        for team, source in ((0, a), (1, b)):
            mine = [r for r, t in zip(rows, teams) if t == team]
            pos = {r: i for i, r in enumerate(source)}
            self.assertEqual([pos[r] for r in mine], sorted(pos[r] for r in mine), "each team picks in its own order")

    def test_one_list_running_dry(self) -> None:
        rows, teams = ranker.team_draft([1, 2], [3, 4, 5, 6], 10, "s")
        self.assertEqual(sorted(rows), [1, 2, 3, 4, 5, 6])
        self.assertEqual(teams.count(0), 2)


class InterleaveServingTests(unittest.TestCase):
    def _state(self, profile):
        return {"fingerprint": {**_fingerprint(), "profile": str(sorted(profile.items()))}, "subs": ["c1", "c2", "c3"], "profile": profile}

    def test_pages_agree_and_never_show_excluded(self) -> None:
        index = ranker.index_from_records(_records(n=600, seed=9, channels=40), NOW)
        profile = {"explore_enabled": True, "muted_channels": ["c7"]}
        with fake_v2(index, self._state(profile)):
            whole = db.list_recommended_videos(120, 0, "recommended", "u1")
            pages = [row for offset in range(0, 120, 30) for row in db.list_recommended_videos(30, offset, "recommended", "u1")]
        self.assertEqual([r["id"] for r in whole], [r["id"] for r in pages])
        self.assertEqual(len({r["id"] for r in whole}), len(whole))
        self.assertTrue(all(r["ranker"]["model"] == "v2-tdi" for r in whole))
        self.assertEqual({r["ranker"]["team"] for r in whole}, set(ranker.INTERLEAVE_TEAMS))
        rows_c7 = {int(i) for i in index.ids[index.channel_rows("c7")]}
        subscribed = {int(i) for k in ("c1", "c2", "c3") for i in index.ids[index.channel_rows(k)]}
        self.assertFalse({r["id"] for r in whole} & (rows_c7 | subscribed))
        self.assertFalse(any(index.blocked[index.rows_for_ids([r["id"] for r in whole])]))

    def test_off_by_default_and_classic_untouched(self) -> None:
        index = ranker.index_from_records(_records(n=300, seed=9, channels=30), NOW)
        with fake_v2(index, self._state({})):
            rows = db.list_recommended_videos(10, 0, "recommended", "u1")
        self.assertTrue(all("team" not in r["ranker"] for r in rows))
        self.assertEqual(rows[0]["ranker"]["model"], "v2-graph")

    def test_impressions_carry_the_team(self) -> None:
        video = {"id": 5, "youtube_video_id": "y5", "ranker": {"model": "v2-tdi", "lane": "recommended", "rank": 0, "team": "blend",
                                                              "experiment": ranker.INTERLEAVE_EXPERIMENT}}
        plain = {"id": 6, "youtube_video_id": "y6", "ranker": {"model": "v2-graph", "lane": "recommended", "rank": 1}}
        records = ranker.impression_records([{"kind": "recommended", "id": "recommended", "videos": [video, plain]}], "u1", 0)
        self.assertEqual(records[0][5]["team"], "blend")
        self.assertEqual(records[0][5]["experiment"], ranker.INTERLEAVE_EXPERIMENT)
        self.assertNotIn("team", records[1][5])

    def test_summary_and_interval(self) -> None:
        lo, hi = ranker.wilson_interval(8, 10)
        self.assertAlmostEqual(lo, 0.490, places=2)
        self.assertAlmostEqual(hi, 0.943, places=2)
        rows = [
            {"user_key": "u", "day": "d1", "youtube_video_id": "a", "team": "graph", "clicked": True},
            {"user_key": "u", "day": "d1", "youtube_video_id": "b", "team": "blend", "clicked": False},
            {"user_key": "u", "day": "d2", "youtube_video_id": "c", "team": "blend", "clicked": True},
            {"user_key": "u", "day": "d2", "youtube_video_id": "d", "team": "blend", "clicked": True},
            {"user_key": "u", "day": "d3", "youtube_video_id": "e", "team": "graph", "clicked": True},
            {"user_key": "u", "day": "d3", "youtube_video_id": "f", "team": "blend", "clicked": True},
            {"user_key": "u", "day": "d4", "youtube_video_id": "g", "team": "graph", "clicked": False},
        ]
        out = ranker.summarize_interleave(rows)
        self.assertEqual(out["impressions"], {"graph": 3, "blend": 4})
        self.assertEqual(out["clicks"], {"graph": 2, "blend": 3})
        self.assertEqual(out["wins"], {"graph": 1, "blend": 1})
        self.assertEqual(out["ties"], 1)
        self.assertEqual(out["days_compared"], 3)
        self.assertAlmostEqual(out["blend_share"], 0.5)
        self.assertIsNone(ranker.summarize_interleave([])["blend_share"])

    def test_click_attribution(self) -> None:
        seen: list[Any] = []

        @contextmanager
        def connect():
            class Cur:
                rowcount = 1

                def execute(self, q, p=None):
                    seen.append((q, p))

            class Conn:
                @contextmanager
                def cursor(self):
                    yield Cur()

                def commit(self):
                    seen.append("commit")

            yield Conn()

        with patch.object(db, "connect", connect):
            self.assertTrue(ranker.record_interleave_click("u1", "y5"))
        self.assertIn("metadata ? 'team'", seen[0][0])
        self.assertIn("NOT metadata ? 'clicked_at'", seen[0][0])
        self.assertEqual(seen[0][1], ("y5", "u1", ranker.INTERLEAVE_CLICK_WINDOW_HOURS))
        with patch.object(db, "connect", side_effect=RuntimeError("db down")):
            self.assertFalse(ranker.record_interleave_click("u1", "y5"))
        self.assertFalse(ranker.record_interleave_click("u1", ""))
        from openfeed import main

        self.assertIn("_record_interleave_click(user_key, youtube_video_id)", inspect.getsource(main.watch_video))

    def test_settings_and_status(self) -> None:
        from openfeed import main

        self.assertTrue(main.FeedPreferencesRequest(explore_enabled=True).explore_enabled)
        self.assertEqual(main._interleave_status_metrics(None, False), [])
        tiles = "".join(main._interleave_status_metrics(ranker.summarize_interleave([
            {"user_key": "u", "day": "d", "youtube_video_id": "a", "team": "blend", "clicked": True},
            {"user_key": "u", "day": "d", "youtube_video_id": "b", "team": "graph", "clicked": False}]), True))
        for text in ("Tuning comparison", "Interleaved impressions", "Opens per side", "100% Graph + popularity", "95% CI"):
            self.assertIn(text, tiles)
        self.assertIn("No decided days", "".join(main._interleave_status_metrics(ranker.summarize_interleave([]), True)))
        self.assertIn("explore_enabled", inspect.signature(db.set_feed_preferences).parameters)


if __name__ == "__main__":
    unittest.main()
