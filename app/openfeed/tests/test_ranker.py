"""Ranker v2: graph parity, grading, eligibility, page step, fallbacks, caching, row shape.

No database: the index is built from synthetic records and every query is answered by a
fake cursor. The parity reference is a dense-matrix transliteration of the track A
reference implementation (exp/A/ref_A.py, UserScorer.graph), which uses scipy; the
production port uses np.bincount. Set OPENFEED_REF_A to that file to also compare
against the original.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import numpy as np

from openfeed import db, evaluation, ranker

NOW = 1.7e9
DAY = 86400.0


def _records(n: int = 400, seed: int = 0, channels: int = 60) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        related = [f"y{rng.randrange(n)}" if rng.random() < 0.35 else f"x{rng.randrange(n * 3)}" for _ in range(rng.randint(0, 12))]
        out.append({
            "id": 10 * (i + 1),
            "youtube_video_id": f"y{i}",
            "channel_id": f"c{rng.randrange(channels)}",
            "view_count": rng.randint(0, 10**6) if rng.random() > 0.1 else None,
            "published_ts": NOW - rng.randint(0, 300) * DAY if rng.random() > 0.1 else None,
            "duration_seconds": rng.randint(91, 4000),
            "live": False,
            "related": related,
        })
    # self-listing and a duplicated related id exercise the dedupe paths
    out[3]["related"] = list(out[3]["related"]) + ["y3", "y3"]
    out[7]["related"] = list(out[7]["related"]) + ["x1", "x1"]
    return out


def _dense_ref_graph(records: list[dict[str, Any]], s: np.ndarray, rounds: int = 2, beta: float = 0.0) -> np.ndarray:
    """ref_A.Corpus.build + UserScorer.graph, line for line, with dense matrices."""
    n = len(records)
    col: dict[Any, int] = {}
    pairs = []
    for i, v in enumerate(records):
        for y in v.get("related") or ():
            pairs.append((i, col.setdefault(("y", y), len(col))))
    self_ctx = np.array([col.setdefault(("y", v["youtube_video_id"]), len(col)) for v in records])
    A = np.zeros((n, max(len(col), 1)))
    for i, j in pairs:
        A[i, j] = 1.0                     # sum_duplicates then data[:] = 1
    deg0 = A.sum(axis=1)
    k0 = A.sum(axis=0)
    seeds = np.flatnonzero(s)
    S = np.zeros_like(A)
    S[seeds, self_ctx[seeds]] = 1.0
    A2 = ((A + S) > 0).astype(float)
    deg = deg0 + np.bincount(seeds, minlength=n)
    k = k0 + np.bincount(self_ctx[seeds], minlength=A.shape[1])
    w = 1 / np.maximum(k, 1)

    def step(x):
        return A2 @ (w * (A2.T @ (x / np.maximum(deg, 1))))

    y = step(s)
    acc = y / max(y.sum(), 1e-12)
    if rounds == 2:
        y2 = step(acc)
        acc = acc + 0.5 * y2 / max(y2.sum(), 1e-12)
    acc[seeds] = 0.0
    return acc / np.maximum(deg, 1) ** beta


def _ref_seeds(records, subscribed, positives) -> np.ndarray:
    s = np.zeros(len(records))
    by_channel: dict[str, list[int]] = {}
    for i, v in enumerate(records):
        by_channel.setdefault(v["channel_id"], []).append(i)
    for ch in subscribed:
        rows = by_channel.get(ch) or []
        for r in rows:
            s[r] += 1.0 / len(rows)
    row_of = {v["id"]: i for i, v in enumerate(records)}
    for vid in positives:
        s[row_of[vid]] += 1.0
    return s / max(s.sum(), 1e-12)


class GraphParityTests(unittest.TestCase):
    def test_graph_matches_reference_walk(self) -> None:
        for seed in (0, 1, 2):
            records = _records(seed=seed)
            index = ranker.index_from_records(records, NOW)
            subscribed = {f"c{i}" for i in range(6)} | {records[3]["channel_id"]}
            positives = [records[5]["id"], records[7]["id"]]
            # The walk itself is ref_A's, so it is compared on ref_A's seed vector. Seed mixing
            # differs from ref_A on purpose (n/(n+K) shrinkage), tested below.
            s = s_ref = _ref_seeds(records, subscribed, positives)
            got = ranker.graph_scores(index, s)
            want = _dense_ref_graph(records, s_ref)
            self.assertLess(float(np.abs(got - want).max()), 1e-9)
            self.assertTrue((got > 0).any())
            self.assertTrue(np.all(got[np.flatnonzero(s)] == 0), "seeds score 0")

    def test_seed_mix_shrinks_events_toward_subscriptions(self) -> None:
        records = _records(seed=0)
        index = ranker.index_from_records(records, NOW)
        subscribed = {f"c{i}" for i in range(6)}
        positives = {records[5]["id"]: 1.0, records[7]["id"]: 0.5}
        s = ranker.seed_vector(index, subscribed, positives)
        self.assertAlmostEqual(float(s.sum()), 1.0, places=12)
        w = ranker.personal_share(2)
        self.assertAlmostEqual(w, 2 / 22)
        event_rows = index.rows_for_ids(list(positives))
        subs_only = ranker.seed_vector(index, subscribed, {})
        events_only = ranker.seed_vector(index, set(), positives)
        np.testing.assert_allclose(s, (1 - w) * subs_only + w * events_only, atol=1e-12)
        self.assertAlmostEqual(float(events_only[event_rows].sum()), 1.0, places=12)
        self.assertAlmostEqual(float(events_only[event_rows[0]] / events_only[event_rows[1]]), 2.0, places=9)
        self.assertEqual(ranker.personal_share(0), 0.0)
        self.assertFalse(ranker.seed_vector(index, set(), {}).any())

    def test_modes_match_reference_formulas(self) -> None:
        records = _records(seed=4)
        index = ranker.index_from_records(records, NOW)
        s = ranker.seed_vector(index, {"c1", "c2"}, {})
        g = ranker.graph_scores(index, s)
        ok = (s == 0) & ~index.blocked
        mm = lambda x: (x - x[ok].min()) / max(x[ok].max() - x[ok].min(), 1e-12)
        blend = ranker.mode_scores("blend", g, index.prior, ok)
        np.testing.assert_allclose(blend[ok], (0.25 * mm(g) + 0.75 * mm(index.prior))[ok], atol=1e-12)
        graph = ranker.mode_scores("graph", g, index.prior, ok)
        expect = np.where(g > 0, 1.0 + g / g.max(), 0.0) + 1e-3 * mm(index.prior)
        np.testing.assert_allclose(graph[ok], expect[ok], atol=1e-12)
        # graph mode: every reached video outranks every unreached one
        reached = ok & (g > 0)
        self.assertGreater(graph[reached].min(), graph[ok & (g == 0)].max())

    def test_velocity_prior(self) -> None:
        prior = ranker.velocity_prior([1000.0, float("nan")], [NOW - 9 * DAY, float("nan")], NOW)
        self.assertAlmostEqual(prior[0], math.log1p(1000) - 1.5 * math.log(10))
        self.assertAlmostEqual(prior[1], -1.5 * math.log(3651))

    def test_against_original_reference_when_available(self) -> None:
        path = os.environ.get("OPENFEED_REF_A", "")
        if not path or not os.path.exists(path):
            self.skipTest("OPENFEED_REF_A not set")
        import sys

        spec = importlib.util.spec_from_file_location("ref_A", path)
        ref = importlib.util.module_from_spec(spec)
        sys.modules["ref_A"] = ref   # its dataclasses look themselves up there
        try:
            spec.loader.exec_module(ref)  # type: ignore[union-attr]
        except ImportError as exc:      # it needs scipy, which the app does not
            self.skipTest(f"reference needs {exc.name}")
        records = _records(seed=7, n=2000)
        corpus = ref.Corpus.build(records, (), NOW)
        scorer = ref.UserScorer(corpus)
        subs = {"c1", "c5", "c9"}
        s_ref = scorer.seeds(subs, [records[2]["id"]])
        index = ranker.index_from_records(records, NOW)
        s = ranker.seed_vector(index, subs, {records[2]["id"]: 1.0})
        self.assertLess(float(np.abs(ranker.graph_scores(index, s) - scorer.graph(s_ref)).max()), 1e-9)


class EventGradingTests(unittest.TestCase):
    def test_reference_self_test_cases(self) -> None:
        pos, neg = ranker.build_events(
            [(1, 600, 0.95, True, 630), (2, 20, 0.05, False, 400), (3, 0, 1.0, True, 300)],
            [(9, None, True)],
            [(10, "hide"), (11, "more_like_this"), (12, "watch_page_open")],
        )
        self.assertEqual(sorted(pos), [1, 3, 11])  # weak watch dropped, an open is not a positive
        self.assertEqual(neg, {9, 10})
        self.assertEqual(pos[1], 1.0)
        self.assertEqual(pos[3], 0.5)   # completed with 0 seconds (mark watched): mid
        self.assertEqual(pos[11], 1.0)

    def test_ratio_bands(self) -> None:
        pos, _ = ranker.build_events(
            [(1, 100, 0.49, False, 300), (2, 100, 0.5, False, 300), (3, 100, 0.89, False, 300),
             (4, 100, 0.9, False, 300), (5, 30, 0.2, True, 300), (6, 450, None, False, 500)],
            [], [],
        )
        self.assertNotIn(1, pos)
        self.assertEqual((pos[2], pos[3], pos[4]), (0.5, 0.5, 1.0))
        self.assertEqual(pos[5], 1.0)   # completed with seconds > 0 is high whatever the ratio
        self.assertEqual(pos[6], 1.0)   # no percent: seconds / duration = 0.9

    def test_like_counts_and_negatives_win(self) -> None:
        pos, neg = ranker.build_events([(1, 900, 1.0, True, 900)], [(1, "dislike", False), (2, "like", False)], [])
        self.assertEqual(pos, {2: 1.0})
        self.assertEqual(neg, {1})

    def test_zero_percent_falls_back_to_seconds(self) -> None:
        signals = ranker.signals_from_rows(
            [], [{"video_id": 5, "total_watch_seconds": 300, "percent_completed": 0, "completed": False,
                  "manually_marked_watched": False, "duration": 320}], [], [], {})
        self.assertEqual(signals.positives, {5: 1.0})


class SignalsTests(unittest.TestCase):
    def _signals(self, progress=(), feedback=(), events=(), profile=None, subs=()):
        return ranker.signals_from_rows(list(subs), list(progress), list(feedback), list(events), profile or {})

    def test_watched_predicate_matches_legacy(self) -> None:
        progress = [
            {"video_id": 1, "completed": True},
            {"video_id": 2, "manually_marked_watched": True},
            {"video_id": 3, "percent_completed": 0.01},
            {"video_id": 4, "total_watch_seconds": 3},
            {"video_id": 5},  # a progress row with nothing in it does not count
        ]
        events = [{"video_id": 6, "event_type": "mark_watched", "n": 1}, {"video_id": 7, "event_type": "watch_page_open", "n": 2}]
        signals = self._signals(progress=progress, events=events)
        self.assertEqual(signals.excluded, {1, 2, 3, 4, 6, 7})

    def test_hidden_uses_feedback_row_when_present(self) -> None:
        events = [{"video_id": 1, "event_type": "hide"}, {"video_id": 2, "event_type": "hide"}]
        feedback = [{"video_id": 2, "reaction": None, "not_interested": False},   # undone
                    {"video_id": 3, "reaction": None, "not_interested": True},
                    {"video_id": 4, "reaction": "dislike", "not_interested": False},
                    {"video_id": 5, "reaction": "like", "not_interested": False}]
        signals = self._signals(feedback=feedback, events=events)
        self.assertEqual(signals.negatives, {1, 3, 4})
        self.assertEqual(signals.excluded, {1, 3, 4})
        self.assertEqual(signals.positives, {5: 1.0})

    def test_profile_fields(self) -> None:
        signals = self._signals(profile={"muted_channels": ["ChanA", " "], "ranker_mode": "graph"}, subs=["UCx", ""])
        self.assertEqual(signals.muted_channels, {"chana"})
        self.assertEqual(signals.mode, "graph")
        self.assertEqual(signals.subscribed_keys, {"ucx"})
        self.assertEqual(self._signals(profile={"ranker_mode": "bogus"}).mode, ranker.DEFAULT_MODE)


def _small_index() -> ranker.CorpusIndex:
    """Two subscribed channels (s1, s2) whose related lists link to the others."""
    recs = []
    vid = 0

    def add(channel, related=(), **extra):
        nonlocal vid
        vid += 1
        recs.append({"id": vid, "youtube_video_id": f"v{vid}", "channel_id": channel, "view_count": 1000 * vid,
                     "published_ts": NOW - vid * DAY, "duration_seconds": 600, "related": list(related), **extra})
        return vid

    for _ in range(3):
        add("s1", ["v10", "v11", "v12", "v13"])
    for _ in range(3):
        add("s2", ["v10", "v14", "v15", "v16"])
    for i in range(7, 31):   # ids 7..30, other channels
        add(f"o{i % 6}", ["v1"] if i % 2 else [])
    return ranker.index_from_records(recs, NOW)


def _entry(index, mode="graph", **kw):
    signals = ranker.UserSignals(subscribed_keys={"s1", "s2"}, mode=mode, **kw)
    return ranker.prepare_user(index, signals)


class EligibilityTests(unittest.TestCase):
    def _eligible_ids(self, index, entry, lane):
        f, ok = ranker.lane_scores(index, entry, lane)
        return {int(i) for i in index.ids[np.isfinite(f)]}

    def test_each_exclusion(self) -> None:
        index = _small_index()
        base = self._eligible_ids(index, _entry(index), "recommended")
        self.assertIn(10, base)
        for name, kwargs in {
            "watched/hidden/disliked": {"excluded": {10}},
            "liked (a positive seed)": {"positives": {10: 1.0}},
        }.items():
            with self.subTest(name):
                self.assertNotIn(10, self._eligible_ids(index, _entry(index, **kwargs), "recommended"))
                self.assertNotIn(10, self._eligible_ids(index, _entry(index, **kwargs), "all"))
        muted_key = index.chan_keys[int(index.chan[index.rows_for_ids([10])[0]])]
        muted = self._eligible_ids(index, _entry(index, muted_channels={muted_key}), "all")
        self.assertFalse(any(index.chan_keys[int(index.chan[r])] == muted_key for r in index.rows_for_ids(sorted(muted))))

    def test_subscribed_channels_only_leave_the_recommended_lane(self) -> None:
        index = _small_index()
        entry = _entry(index)
        self.assertFalse({1, 2, 3, 4, 5, 6} & self._eligible_ids(index, entry, "recommended"))
        self.assertTrue({1, 2, 3, 4, 5, 6} <= self._eligible_ids(index, entry, "all"))

    def test_signals_excluded_from_real_rows(self) -> None:
        index = _small_index()
        signals = ranker.signals_from_rows(
            ["s1", "s2"],
            [{"video_id": 11, "percent_completed": 0.3}],
            [{"video_id": 12, "reaction": None, "not_interested": True}, {"video_id": 13, "reaction": "dislike"}],
            [{"video_id": 14, "event_type": "watch_page_open"}, {"video_id": 15, "event_type": "hide"}],
            {},
        )
        entry = ranker.prepare_user(index, signals)
        eligible = self._eligible_ids(index, entry, "recommended")
        self.assertFalse({11, 12, 13, 14, 15} & eligible)
        self.assertIn(16, eligible)


class LiveAndShortsTests(unittest.TestCase):
    def test_flags(self) -> None:
        recs = [
            {"id": 1, "youtube_video_id": "a", "channel_id": "c", "duration_seconds": 0},
            {"id": 2, "youtube_video_id": "b", "channel_id": "c", "duration_seconds": 3000, "live": True},
            {"id": 3, "youtube_video_id": "c", "channel_id": "c", "duration_seconds": 60},
            {"id": 4, "youtube_video_id": "d", "channel_id": "c", "duration_seconds": 90},
            {"id": 5, "youtube_video_id": "e", "channel_id": "c", "duration_seconds": 91},
            {"id": 6, "youtube_video_id": "f", "channel_id": "c", "duration_seconds": 300, "short_url": True},
            {"id": 7, "youtube_video_id": "g", "channel_id": "c", "duration_seconds": None},
        ]
        index = ranker.index_from_records(recs, NOW)
        self.assertEqual(index.live.tolist(), [True, True, False, False, False, False, False])
        self.assertEqual(index.short.tolist(), [False, False, True, True, False, True, False])
        entry = ranker.prepare_user(index, ranker.UserSignals(mode="blend"))
        for lane in ranker.V2_LANES:
            f, _ = ranker.lane_scores(index, entry, lane)
            self.assertEqual({int(i) for i in index.ids[np.isfinite(f)]}, {5, 7})


class ChannelDemotionTests(unittest.TestCase):
    def test_threshold_is_two_independent_negatives(self) -> None:
        index = _small_index()
        rows_o1 = [int(i) for i in index.ids if index.chan_keys[int(index.chan[index.rows_for_ids([i])[0]])] == "o1"]
        rows_o2 = [int(i) for i in index.ids if index.chan_keys[int(index.chan[index.rows_for_ids([i])[0]])] == "o2"]
        survivor = rows_o1[-1]
        undemoted, _ = ranker.lane_scores(index, _entry(index), "all")
        row = index.rows_for_ids([survivor])[0]
        one, _ = ranker.lane_scores(index, _entry(index, negatives={rows_o1[0]}, excluded={rows_o1[0]}), "all")
        self.assertEqual(one[row], undemoted[row])
        spread, _ = ranker.lane_scores(index, _entry(index, negatives={rows_o1[0], rows_o2[0]}, excluded={rows_o1[0], rows_o2[0]}), "all")
        self.assertEqual(spread[row], undemoted[row])
        two, _ = ranker.lane_scores(index, _entry(index, negatives=set(rows_o1[:2]), excluded=set(rows_o1[:2])), "all")
        self.assertAlmostEqual(two[row], undemoted[row] * ranker.CHANNEL_DEMOTION)


class MmrTests(unittest.TestCase):
    def test_deterministic_and_no_duplicates(self) -> None:
        rng = np.random.default_rng(3)
        scores = np.sort(rng.random(200))[::-1]
        channels = rng.integers(0, 20, 200).tolist()
        vectors = [{f"t{j}": float(rng.random()) for j in rng.integers(0, 40, 5)} if i % 7 else None for i in range(200)]
        first = ranker.mmr_page(scores, channels, vectors)
        second = ranker.mmr_page(scores, channels, vectors)
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), list(range(200)))
        self.assertEqual(first[50:], sorted(first[50:]), "after the MMR slots the rest keep score order")

    def test_channel_factor_and_topic_penalty(self) -> None:
        # Near-equal scores: the second pick avoids the first pick's topic (and channel),
        # the third prefers a new channel over a second video from the first one.
        scores = np.array([1.0, 1.0, 1.0, 0.99, 0.0])
        page = ranker.mmr_page(scores, ["a", "a", "b", "c", "d"], [{"x": 1.0}, {"x": 1.0}, {"x": 1.0}, {"y": 1.0}, None])
        self.assertEqual(page[:3], [0, 3, 2])

    def test_rank_lane_is_complete_and_unique(self) -> None:
        index = _small_index()
        entry = _entry(index)
        ranking = ranker.rank_lane(index, entry, "all")
        rows = ranking.rows.tolist()
        self.assertEqual(len(rows), len(set(rows)))
        f, ok = ranker.lane_scores(index, entry, "all")
        self.assertEqual(set(rows), set(np.flatnonzero(ok).tolist()))


class FakeCursor:
    """Answers the ranker's queries by matching their text."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self._rows: list[dict[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.state.setdefault("queries", []).append(query)
        if "WITH ORDINALITY" in query:   # first: it also mentions feeds
            self._rows = [_legacy_row(i) for i in params["ids"]]
        elif "concat_ws" in query:
            self._rows = [dict(self.state["fingerprint"])]
        elif "catalog_claims" in query and "AS through" in query:
            # The caller's part of the shared catalog (catalog_scope.scope_sets): by default
            # a claim on every id, so these tests rank the whole fake library as before.
            self._rows = [{"through": self.state.get("through", 2 ** 62)}]
        elif "openfeed_video_scope" in query:
            self._rows = [{"id": i} for i in self.state.get("scope_ids", [])]
        elif "FROM video_provenance WHERE user_key" in query:
            self._rows = [{"id": i} for i in self.state.get("own_ids", [])]
        elif "SELECT profile FROM users" in query:
            self._rows = [{"profile": self.state.get("profile", {})}]
        elif "FROM feeds" in query:
            self._rows = [{"k": k} for k in self.state.get("subs", [])]
        elif "FROM playback_progress pp LEFT JOIN" in query:
            self._rows = []
        elif "FROM user_video_feedback WHERE" in query:
            self._rows = []
        elif "GROUP BY 1, 2" in query:
            self._rows = []
        elif "SELECT id, topic_vector" in query:
            self._rows = [{"id": i, "topic_vector": {"t": 1.0}} for i in params[0]]
        elif "SELECT utp.topic" in query:
            self._rows = list(self.state.get("topic_prefs", []))
        elif "AS preferred" in query:
            self._rows = list(self.state.get("topic_matches", []))
        elif "FROM takeout_watched_videos t" in query:
            self._rows = list(self.state.get("takeout", []))
        else:
            raise AssertionError(f"unexpected query: {query[:80]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


LEGACY_COLUMNS = [
    "id", "youtube_video_id", "title", "url", "thumbnail_url", "author", "published_at", "created_at",
    "duration_seconds", "view_count", "tags", "topic_vector", "channel_title", "channel_avatar_url",
    "youtube_channel_id", "source_kind", "discovery_source", "is_subscribed_channel",
    "reaction", "not_interested", "percent_completed", "progress_completed", "manually_marked_watched",
    "resume_seconds", "feedback_score", "hide_count", "watched_count", "open_count", "recommendation_score",
]


def _legacy_row(video_id: int) -> dict[str, Any]:
    row = {name: None for name in LEGACY_COLUMNS}
    row.update({"id": video_id, "youtube_video_id": f"v{video_id}", "title": f"Video {video_id} about welding",
                "tags": [], "source_kind": "discovery", "is_subscribed_channel": False, "recommendation_score": 0.0})
    return row


@contextmanager
def fake_v2(index: ranker.CorpusIndex, state: dict[str, Any]):
    @contextmanager
    def connect():
        cursor = FakeCursor(state)

        class Conn:
            @contextmanager
            def cursor(self, *a, **k):
                yield cursor

        yield Conn()

    class Settings:
        database_url = "postgresql://fake"

    ranker.clear_user_cache()
    with patch.object(ranker.INDEX, "index", index), \
         patch.object(ranker.INDEX, "maybe_refresh", lambda force=False: None), \
         patch.object(db, "connect", connect), \
         patch("openfeed.config.get_settings", return_value=Settings()):
        yield
    ranker.clear_user_cache()


class RowShapeTests(unittest.TestCase):
    def test_v2_rows_keep_the_legacy_shape(self) -> None:
        index = _small_index()
        state = {"fingerprint": {"events": "0:", "feedback": "0:", "progress": "0:", "subscriptions": "2:x", "profile": "p"},
                 "subs": ["s1", "s2"], "profile": {"ranker_mode": "graph"}}
        with fake_v2(index, state):
            rows = db.list_recommended_videos(5, 0, "recommended", "u1")
            more = db.list_recommended_videos(5, 5, "recommended", "u1")
        self.assertEqual(len(rows), 5)
        for row in rows:
            for name in LEGACY_COLUMNS:
                self.assertIn(name, row)
            self.assertIsInstance(row["recommendation_score"], float)
            self.assertIsInstance(row["score_debug"], dict)
            self.assertTrue(row["recommendation_reason"])
            self.assertIn("welding", row["topic_tokens"])
            self.assertEqual(row["ranker"]["model"], "v2-graph")
        self.assertFalse({r["id"] for r in rows} & {r["id"] for r in more}, "pages do not overlap")
        self.assertEqual([r["ranker"]["rank"] for r in more], list(range(5, 10)))
        # Reached videos come first in graph mode and say why.
        self.assertTrue(rows[0]["recommendation_reason"].startswith("Linked from"))
        from openfeed import main

        json.dumps(main._json_ready(rows))

    def test_row_sql_selects_every_legacy_column(self) -> None:
        sql = ranker._row_sql()
        for name in LEGACY_COLUMNS:
            self.assertRegex(sql, rf"(v\.{name}\b|AS {name}\b|c\.{name}\b|fe\.{name}\b|pp\.{name}\b|uvf\.{name}\b)")


class FallbackTests(unittest.TestCase):
    def _legacy_called(self, **patches) -> bool:
        seen: list[str] = []
        with patch.object(db, "_apply_recommendation_scores", side_effect=lambda *a, **k: seen.append("legacy") or []), \
             patch.object(db, "_diversify_recommendations", return_value=[]):
            @contextmanager
            def connect():
                class Cur:
                    def execute(self, q, p=None):
                        seen.append(q)

                    def fetchall(self):
                        return []

                class Conn:
                    @contextmanager
                    def cursor(self, *a, **k):
                        yield Cur()

                yield Conn()

            with patch.object(db, "connect", connect):
                db.list_recommended_videos(10, 0, "recommended", "u1")
        return "legacy" in seen

    def test_numpy_missing_falls_back(self) -> None:
        with patch.object(ranker, "NUMPY_AVAILABLE", False):
            self.assertIsNone(ranker.recommended_rows(10, 0, "recommended", "u1"))
            self.assertTrue(self._legacy_called())
        self.assertEqual(ranker._LAST_FALLBACK["reason"], "numpy is not installed")

    def test_index_not_ready_falls_back_and_schedules_a_build(self) -> None:
        class Settings:
            database_url = "postgresql://fake"

        calls: list[bool] = []
        with patch.object(ranker.INDEX, "index", None), \
             patch.object(ranker.INDEX, "maybe_refresh", lambda force=False: calls.append(force)), \
             patch("openfeed.config.get_settings", return_value=Settings()):
            self.assertIsNone(ranker.recommended_rows(10, 0, "all", "u1"))
        self.assertTrue(calls)
        self.assertIn("not built", ranker._LAST_FALLBACK["reason"])

    def test_exception_falls_back(self) -> None:
        with patch.object(ranker, "recommended_rows", side_effect=RuntimeError("boom")):
            self.assertTrue(self._legacy_called())
        self.assertIn("boom", ranker._LAST_FALLBACK["reason"])

    def test_classic_mode_and_other_lanes_use_legacy(self) -> None:
        index = _small_index()
        state = {"fingerprint": {"events": "0:"}, "subs": ["s1"], "profile": {"ranker_mode": "classic"}}
        with fake_v2(index, state):
            self.assertIsNone(ranker.recommended_rows(5, 0, "recommended", "u1"))
        self.assertIsNone(ranker.recommended_rows(5, 0, "continue", "u1"))
        with ranker.forced_mode("classic"):
            self.assertIsNone(ranker.recommended_rows(5, 0, "all", "u1"))

    def test_fallback_reason_is_logged_once(self) -> None:
        reason = "unit-test reason"
        ranker._FALLBACK_LOGGED.discard(reason)
        with self.assertLogs("openfeed.ranker", level="WARNING") as logs:
            ranker.note_fallback(reason)
            ranker.note_fallback(reason)
            ranker.logger.warning("sentinel")
        self.assertEqual(sum(reason in line for line in logs.output), 1)


class CacheTests(unittest.TestCase):
    def test_fingerprint_change_invalidates(self) -> None:
        index = _small_index()
        state = {"fingerprint": {"events": "1:5", "feedback": "", "progress": "", "subscriptions": "2:a", "profile": "p"},
                 "subs": ["s1", "s2"], "profile": {}}
        loads: list[str] = []
        real = ranker.load_signals

        def counting(cur, user_key):
            loads.append(user_key)
            return real(cur, user_key)

        with fake_v2(index, state), patch.object(ranker, "load_signals", counting):
            db.list_recommended_videos(5, 0, "recommended", "u1")
            db.list_recommended_videos(5, 0, "all", "u1")
            db.list_recommended_videos(5, 5, "recommended", "u1")
            self.assertEqual(len(loads), 1, "one ranking serves every lane and page")
            state["fingerprint"] = {**state["fingerprint"], "events": "2:6"}
            db.list_recommended_videos(5, 0, "recommended", "u1")
            self.assertEqual(len(loads), 2)
            state["subs"] = ["s1"]
            state["fingerprint"] = {**state["fingerprint"], "subscriptions": "1:b"}
            rows = db.list_recommended_videos(30, 0, "recommended", "u1")
            self.assertEqual(len(loads), 3)
            # s2 is no longer followed, so its videos may now be recommended
            self.assertTrue({4, 5, 6} & {r["id"] for r in rows})

    def test_index_version_change_invalidates(self) -> None:
        state = {"fingerprint": {"events": "1"}, "subs": ["s1"], "profile": {}}
        loads: list[str] = []
        real = ranker.load_signals
        with patch.object(ranker, "load_signals", lambda cur, u: loads.append(u) or real(cur, u)):
            first = _small_index()
            with fake_v2(first, state):
                db.list_recommended_videos(5, 0, "all", "u1")
            second = _small_index()
            second.version = first.version + 1
            with fake_v2(second, state):
                db.list_recommended_videos(5, 0, "all", "u1")
        self.assertEqual(len(loads), 2)


class ExplainTests(unittest.TestCase):
    def test_reasons(self) -> None:
        # Odd ids 7..29 list "v1", a subscribed video, so they share its context (a seed sits
        # in its own id's context). A video a seed merely lists is not reached: that is the
        # reference walk's semantics, kept exactly.
        index = _small_index()
        row_9 = int(index.rows_for_ids([9])[0])
        self.assertEqual(ranker.explain(index, _entry(index), row_9), "Linked from 1 video of a channel you follow")
        entry = _entry(index, positives={7: 1.0})
        # One event gets 1/21 of the seed mass (n/(n+20)), less than the subscribed video it
        # shares the context with, so the reason names the bigger contributor.
        self.assertEqual(ranker.explain(index, entry, row_9), "Linked from 1 video of a channel you follow")
        events_only = ranker.prepare_user(index, ranker.UserSignals(subscribed_keys=set(), positives={7: 1.0}, mode="graph"))
        self.assertEqual(ranker.explain(index, events_only, row_9), "Linked from 1 video you watched or liked")
        row_1 = int(index.rows_for_ids([1])[0])
        self.assertEqual(ranker.explain(index, entry, row_1), "From a channel you follow")
        unreached = [int(r) for r in np.flatnonzero(entry.graph == 0) if not entry.sub_mask[r]]
        self.assertEqual(ranker.explain(index, entry, unreached[0]), "Popular and recent")


class ImpressionTests(unittest.TestCase):
    def test_only_v2_rows_in_recommendation_shelves(self) -> None:
        v2 = {"id": 1, "youtube_video_id": "a", "ranker": {"model": "v2-blend", "lane": "recommended", "rank": 0, "score": 0.9, "graph": 0.1, "prior": 3.0}}
        classic = {"id": 2, "youtube_video_id": "b"}
        sections = [
            {"id": "recommended", "kind": "recommended", "videos": [v2, classic]},
            {"id": "new-subscriptions", "kind": "subscription", "videos": [v2]},
        ]
        records = ranker.impression_records(sections, "u1", 0)
        self.assertEqual(len(records), 1)
        video_id, youtube_id, lane, position, model, meta = records[0]
        self.assertEqual((video_id, youtube_id, lane, position, model), (1, "a", "recommended", 1, "v2-blend"))
        self.assertEqual({k: meta[k] for k in ("score", "graph", "prior", "user_key")}, {"score": 0.9, "graph": 0.1, "prior": 3.0, "user_key": "u1"})

    def test_a_failing_writer_never_raises(self) -> None:
        with patch.object(ranker, "_impression_writer", lambda: None):
            ranker.log_impressions([(1, "a", "recommended", 1, "v2-graph", {})])


class EvaluationTests(unittest.TestCase):
    def test_metrics(self) -> None:
        perfect = ranker.loco_metrics(np.array([True] * 5 + [False] * 95), n_candidates=1000, n_positive=5)
        self.assertAlmostEqual(perfect["ndcg@20"], 1.0)
        self.assertAlmostEqual(perfect["recall@100"], 1.0)
        self.assertAlmostEqual(perfect["random_recall@100"], 0.1)
        self.assertAlmostEqual(perfect["random_ndcg@20"], 5 / 1000 * sum(1 / math.log2(i + 2) for i in range(20))
                               / sum(1 / math.log2(i + 2) for i in range(5)))
        none = ranker.loco_metrics(np.zeros(100, bool), 1000, 5)
        self.assertEqual((none["ndcg@20"], none["recall@100"]), (0.0, 0.0))

    def test_leave_one_channel_out_runs_per_channel(self) -> None:
        records = _records(n=600, seed=11, channels=30)
        index = ranker.index_from_records(records, NOW)
        subscribed = [f"c{i}" for i in range(8)]
        result = evaluation.leave_one_channel_out(index=index, subscribed=subscribed, include_classic=False, min_videos=5)
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["summary"]), {"graph", "blend"})
        self.assertEqual(result["units"] + result["skipped_small"], len(subscribed))
        for unit in result["channels"]:
            self.assertGreaterEqual(unit["positives"], 5)
            self.assertLessEqual(unit["graph"]["recall@100"], 1.0)

    def test_classic_is_scored_on_the_same_candidates(self) -> None:
        index = _small_index()
        held = [1, 2, 3]
        result = ranker.loco_evaluate(index, ["s1", "s2"], ("graph",), min_videos=3, classic_ranked_ids=[4, 1, 10, 2])
        unit = next(u for u in result["channels"] if u["channel"] == "s1")
        # 4 is s2's (subscribed, not a candidate) and is dropped; 1 and 2 are hits at ranks 1 and 3
        self.assertAlmostEqual(unit["classic"]["recall@100"], 2 / len(held))


class ModeTests(unittest.TestCase):
    def test_default_and_override(self) -> None:
        self.assertIn(ranker.DEFAULT_MODE, ranker.MODES)
        self.assertEqual(ranker.resolve_mode(""), ranker.DEFAULT_MODE)
        self.assertEqual(ranker.resolve_mode("CLASSIC"), "classic")
        with ranker.forced_mode("graph"):
            self.assertEqual(ranker.resolve_mode("classic"), "graph")
        self.assertEqual(db.RANKER_MODES, ranker.MODES)

    def test_settings_select(self) -> None:
        from openfeed import main

        html = main._ranker_mode_select("graph", "data-setting-ranker")
        self.assertIn('name="ranker_mode"', html)
        self.assertIn('<option value="graph" selected>', html)
        self.assertIn(f"Automatic ({ranker.MODE_LABELS[ranker.DEFAULT_MODE]})", html)
        self.assertIn('<option value="" selected>', main._ranker_mode_select("nonsense"))
        self.assertEqual(main.FeedPreferencesRequest(ranker_mode="blend").ranker_mode, "blend")


if __name__ == "__main__":
    unittest.main()


class FeedPageFollowUpTests(unittest.TestCase):
    """Found by the ranker v2 QA pass."""

    def test_deep_home_grids_drop_live_streams(self) -> None:
        from openfeed import main

        rows = [{"id": 1, "duration_seconds": 0}, {"id": 2, "duration_seconds": 600}, {"id": 3, "duration_seconds": None}]
        self.assertEqual([r["id"] for r in main._without_live(rows)], [2, 3])

    def test_muted_creators_stay_listed_so_they_can_be_unmuted(self) -> None:
        from types import SimpleNamespace

        from openfeed import main

        request = SimpleNamespace()
        with patch.object(main, "_login_required_redirect", lambda r: None), \
             patch.object(main, "_user_key", lambda r: "u1"), \
             patch.object(evaluation, "coverage_report", lambda u, d: {}), \
             patch.object(db, "get_feed_preferences", lambda u: {"muted_channels": ["UCmuted", "ucshown"]}), \
             patch.object(db, "top_feed_channels", lambda u, n: [{"key": "ucshown", "title": "Shown", "count": 4}, {"key": "ucother", "title": "Other", "count": 2}]), \
             patch.object(db, "channel_titles_for_keys", lambda keys: {"ucmuted": "Muted One"}):
            out = main.api_feed_composition(request)
        chips = {row["key"]: row for row in out["channels"]}
        self.assertEqual(set(chips), {"ucshown", "ucother", "ucmuted"})
        self.assertTrue(chips["ucshown"]["muted"])
        self.assertFalse(chips["ucother"]["muted"])
        self.assertEqual(chips["ucmuted"], {"key": "ucmuted", "title": "Muted One", "count": 0, "muted": True})


class FeelAuditTests(unittest.TestCase):
    """docs/ranker-v2/feel-preregistration.md: slot mix, quality, dead floor, rotation, reasons."""

    def test_slot_mix_follows_shares_without_repeats(self) -> None:
        a = list(range(0, 100))
        b = list(range(1000, 1100))
        rows, src = ranker.slot_mix([a, b], [0.25, 0.75], 40)
        self.assertEqual(len(rows), len(set(rows)))
        self.assertEqual(src.count(0), 10)
        self.assertEqual(src.count(1), 30)
        # a dry source drops out and the other fills the rest
        rows, src = ranker.slot_mix([[1, 2], list(range(10, 30))], [0.5, 0.5], 10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(src.count(0), 2)
        # a row shared by two sources is taken once
        rows, _ = ranker.slot_mix([[1, 2, 3], [1, 2, 4]], [0.5, 0.5], 4)
        self.assertEqual(sorted(rows), [1, 2, 3, 4])

    def test_graph_mix_shares_and_remainder(self) -> None:
        records = _records(seed=5)
        index = ranker.index_from_records(records, NOW)
        subs = {f"c{i}" for i in range(8)}
        g_sub = ranker.graph_scores(index, ranker.seed_vector(index, subs, {}))
        ok = ~index.blocked & (g_sub >= 0)
        mult = np.ones(index.n)
        rows, src = ranker.graph_mix(index, g_sub, np.zeros(index.n), ok, mult, 0.0, now_ts=NOW, depth=200)
        self.assertNotIn(0, src.tolist(), "no events, no watched slots")
        channel_rows = [int(r) for r, s in zip(rows, src) if s == 2]
        self.assertTrue(channel_rows and all(g_sub[r] > 0 for r in channel_rows), "the channels source holds reached videos only")
        self.assertTrue(all(g_sub[int(r)] == 0 for r, s in zip(rows, src) if s == -1), "the remainder is unreached")
        affinity, best = ranker.creator_affinity(index, g_sub / g_sub.max(), ok)
        fresh_rows = [int(r) for r, s in zip(rows, src) if s == 1]
        self.assertTrue(fresh_rows, "new uploads from linked creators get slots")
        for r in fresh_rows:
            self.assertLessEqual((NOW - index.pub_ts[r]) / DAY, ranker.FRESH_MAX_DAYS)
            self.assertGreater(affinity[index.chan[r]], 0)
        first10 = [int(index.chan[r]) for r in fresh_rows[:10]]
        self.assertEqual(len(first10), len(set(first10)), "creator slots rotate across creators")
        g_evt = ranker.graph_scores(index, ranker.seed_vector(index, set(), {records[9]["id"]: 1.0}))
        rows, src = ranker.graph_mix(index, g_sub, g_evt, ok, mult, 0.25, now_ts=NOW, depth=40)
        self.assertEqual(int((src == 0).sum()), 10, "watched gets its slot share")

    def test_dead_videos_never_recommended(self) -> None:
        recs = [{"id": i + 1, "youtube_video_id": f"v{i + 1}", "channel_id": "s1" if i < 3 else "o", "view_count": 50 if i == 5 else 5000,
                 "published_ts": NOW - 30 * DAY, "duration_seconds": 600, "related": ["v1"] if i >= 3 else []} for i in range(10)]
        index = ranker.index_from_records(recs, NOW)
        self.assertTrue(bool(index.dead[5]))
        entry = ranker.prepare_user(index, ranker.UserSignals(subscribed_keys={"s1"}, mode="graph"))
        lane = ranker.rank_lane(index, entry, "recommended")
        self.assertNotIn(6, [int(i) for i in index.ids[lane.rows]])

    def test_rotation_moves_already_shown_videos_down(self) -> None:
        index = _small_index()
        first = ranker.rank_lane(index, _entry(index), "recommended")
        top = int(index.ids[first.rows[0]])
        again = ranker.rank_lane(index, _entry(index, impression_days={top: 3}), "recommended")
        self.assertGreater([int(i) for i in index.ids[again.rows]].index(top), 0)

    def test_quality_multiplier_range_and_freshness_off(self) -> None:
        index = ranker.index_from_records(_records(seed=2), NOW)
        m = ranker.feel_multiplier(index, NOW)
        self.assertGreaterEqual(float(m.min()), 0.5 - 1e-12)
        self.assertLessEqual(float(m.max()), 1.0 + 1e-12)
        np.testing.assert_allclose(m, 0.5 + 0.5 * index.prior_pct)
        self.assertTrue(np.all(ranker.feel_multiplier(index, NOW, {}) == 1.0))

    def test_reasons_name_the_seed(self) -> None:
        index = _small_index()
        row_9 = int(index.rows_for_ids([9])[0])
        entry = _entry(index)
        self.assertEqual(ranker.explain_parts(index, entry, row_9, 2), ("follow", "s1"))
        events_only = ranker.prepare_user(index, ranker.UserSignals(subscribed_keys=set(), positives={7: 1.0}, mode="graph"))
        self.assertEqual(ranker.explain_parts(index, events_only, row_9, 0), ("watched", 7))
        self.assertEqual(ranker.reason_text("watched", "A very good video"), "Because you watched “A very good video”")
        self.assertEqual(ranker.reason_text("follow", "Some Channel", fresh=True), "New · Because you follow Some Channel")
        self.assertTrue(ranker.reason_text("watched", "x" * 100).endswith("…”"))
        self.assertEqual(ranker.explain_parts(index, entry, row_9, -1), ("popular", None))

    def test_age_matched_loco_restricts_negatives(self) -> None:
        index = ranker.index_from_records(_records(seed=3, n=600, channels=12), NOW)
        subs = [f"c{i}" for i in range(6)]
        full = ranker.loco_evaluate(index, subs, ("graph",), 3, 2, now_ts=NOW)
        aged = ranker.loco_evaluate(index, subs, ("graph",), 3, 2, age_matched=True, now_ts=NOW)
        self.assertTrue(full["channels"] and aged["channels"])
        self.assertLess(aged["channels"][0]["candidates"], full["channels"][0]["candidates"])


class AuditFixTests(unittest.TestCase):
    """Defects found by the independent audit (2026-11-08)."""

    def _signals(self, progress=(), feedback=(), events=()):
        return ranker.signals_from_rows([], list(progress), list(feedback), list(events), {})

    def test_percent_on_a_0_to_100_scale_is_normalised(self) -> None:
        sig = self._signals(progress=[
            {"video_id": 1, "total_watch_seconds": 60, "percent_completed": 30, "completed": False, "duration": 600},
            {"video_id": 2, "total_watch_seconds": 400, "percent_completed": 70, "completed": False, "duration": 600},
            {"video_id": 3, "total_watch_seconds": 590, "percent_completed": 0.98, "completed": False, "duration": 600},
        ])
        self.assertNotIn(1, sig.positives, "30% is a weak watch")
        self.assertEqual(sig.positives[2], ranker.W_MID)
        self.assertEqual(sig.positives[3], ranker.W_HIGH)

    def test_less_like_this_is_a_negative(self) -> None:
        sig = self._signals(events=[{"video_id": 5, "event_type": "less_like_this", "n": 1}])
        self.assertIn(5, sig.negatives)
        self.assertIn(5, sig.excluded)

    def test_undone_hide_does_not_cancel_a_like(self) -> None:
        sig = self._signals(feedback=[{"video_id": 7, "reaction": "like", "not_interested": False}],
                            events=[{"video_id": 7, "event_type": "hide", "n": 1}])
        self.assertNotIn(7, sig.negatives)
        self.assertIn(7, sig.positives)

    def test_old_events_decay_and_the_watched_share_is_capped(self) -> None:
        import datetime as dt

        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120)
        sig = self._signals(progress=[{"video_id": 9, "total_watch_seconds": 600, "percent_completed": 1.0, "completed": True,
                                       "duration": 600, "last_watched_at": old}])
        self.assertAlmostEqual(sig.positive_decay[9], 0.25, places=2)
        index = ranker.index_from_records(_records(seed=1), NOW)
        many = {int(r["id"]): 1.0 for r in _records(seed=1)[:80]}
        entry = ranker.prepare_user(index, ranker.UserSignals(subscribed_keys={"c1", "c2"}, positives=many, mode="graph"))
        self.assertLessEqual(entry.watched_share, ranker.WATCHED_SHARE_MAX)

    def test_watched_slots_rotate_across_seeds(self) -> None:
        seed_of = np.array([5, 5, 5, 8, 8, 9])
        self.assertEqual(ranker._round_robin_by_seed([0, 1, 2, 3, 4, 5], seed_of), [0, 3, 5, 1, 4, 2])

    def test_home_lanes_are_read_contiguously(self) -> None:
        from openfeed import main

        size = main.HOME_PAGE_SIZE
        rec = [main._home_lane_offset(p, "recommended") for p in range(1, 10) if main._home_page_lane(p) == "recommended"]
        self.assertEqual(rec, [2 * size + i * size for i in range(len(rec))])
        recent = [main._home_lane_offset(p, "recent") for p in range(1, 12) if main._home_page_lane(p) == "recent"]
        self.assertEqual(recent, [i * size for i in range(len(recent))])

    def test_classic_fallback_keeps_the_lane_rules(self) -> None:
        rows = [{"id": 1, "duration_seconds": 0}, {"id": 2, "duration_seconds": 60}, {"id": 3, "duration_seconds": 600, "is_subscribed_channel": True},
                {"id": 4, "duration_seconds": 600}, {"id": 5, "duration_seconds": 600, "url": "https://www.youtube.com/shorts/x"}]
        self.assertEqual([r["id"] for r in db._apply_v2_lane_rules(rows, "recommended")], [4])
        self.assertEqual([r["id"] for r in db._apply_v2_lane_rules(rows, "all")], [3, 4])

    def test_related_cache_round_trip(self) -> None:
        import tempfile

        related = {"ids": np.array([1, 5, 9]), "upd": np.array([1.0, 2.0, 3.0]), "live": np.array([False, True, False]),
                   "ptr": np.array([0, 2, 2, 3]), "keys": np.array([11, 12, 13])}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"OPENFEED_RANKER_CACHE": os.path.join(tmp, "r.npz")}):
                ranker._save_related_cache(related)
                back = ranker._load_related_cache()
                self.assertIsNotNone(back)
                for key, value in related.items():
                    np.testing.assert_array_equal(back[key], value)
                with open(os.path.join(tmp, "r.npz"), "wb") as fh:
                    fh.write(b"not a cache")
                self.assertIsNone(ranker._load_related_cache())
