"""One account's activity never shows up in another's experience (catalog_scope.py).

Two people share one catalog. Alice searches for and opens a few videos; they are stored
for everyone, but Bob's Home, Shorts, Up next, library search and suggestions must never
offer them unless Bob has his own reason (he follows the channel, the video is on the
related list of one of his own videos, or he was shown it himself). And with one person on
the install (sign-in off, no profiles), nothing changes at all.

DB-free: the ranker runs on a small in-memory index, the SQL is captured
(tests/sqlcapture.py), and the routes run with storage replaced by fakes.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

from openfeed.tests.sqlcapture import _capture_all_sql, _capture_sql, placeholder_count
from openfeed import catalog_scope, db, discovery, main, profiles, ranker

NOW = 1.7e9
DAY = 86400.0
ALICE, BOB = "u-alice", "u-bob"


# --- a small two-person library ------------------------------------------------------------
#
#   b1..b4    Bob's subscribed channel "bobchan"; b1 lists r1 (a video of "other") as related
#   r1        related to Bob's own video: Bob's to be offered
#   vA, vA2   Alice searched for these (channel "alicechan"); vA lists b1 and w1
#   w1, w2    reachable only through vA: w2 lists w1
#   sA        a Short Alice searched for
#   d1        discovered (SQL scope) from one of Bob's videos
#   f1        Bob's own provenance row (he opened it)

def _library() -> tuple[ranker.CorpusIndex, dict[str, int]]:
    recs: list[dict[str, Any]] = []
    ids: dict[str, int] = {}

    def add(name: str, channel: str, related=(), duration=600, **extra):
        vid = len(recs) + 1
        ids[name] = vid
        recs.append({"id": vid, "youtube_video_id": name, "channel_id": channel, "view_count": 5000 + 100 * vid,
                     "published_ts": NOW - vid * DAY, "duration_seconds": duration, "related": list(related), **extra})

    add("b1", "bobchan", ["r1", "b2"])
    add("b2", "bobchan", ["b1", "b3"])
    add("b3", "bobchan", ["b2"])
    add("b4", "bobchan", ["b1"])
    add("r1", "other", ["b1"])
    add("vA", "alicechan", ["b1", "w1"])
    add("vA2", "alicechan", ["vA"])
    add("w1", "wchan", [])
    add("w2", "wchan", ["w1"])
    add("sA", "alicechan", ["b1"], duration=40)
    add("d1", "dchan", [])
    add("f1", "fchan", ["vA2"])
    return ranker.index_from_records(recs, NOW), ids


def _bob(index, ids, **kw):
    signals = ranker.UserSignals(subscribed_keys={"bobchan"}, scoped=True,
                                 scope_ids={ids["d1"], ids["f1"]}, own_ids={ids["f1"]}, **kw)
    return ranker.prepare_user(index, signals)


def _alice(index, ids):
    signals = ranker.UserSignals(subscribed_keys=set(), scoped=True, scope_ids={ids["vA"], ids["vA2"], ids["sA"]},
                                 own_ids={ids["vA"], ids["vA2"], ids["sA"]})
    return ranker.prepare_user(index, signals)


def _names(index, ids, rows) -> set[str]:
    back = {v: k for k, v in ids.items()}
    return {back[int(index.ids[r])] for r in rows}


# vA2 is Alice's too, but it is on the related list of f1, which Bob opened himself: one
# link from his own video, so it is his to be offered as well (asserted below).
ALICE_ONLY = {"vA", "w1", "w2", "sA"}


class RankerScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.ids = _library()

    def test_bob_is_never_offered_what_alice_searched(self) -> None:
        bob = _bob(self.index, self.ids)
        for lane in ("recommended", "all", "recommended-recent", "recommended-long", "shorts"):
            with self.subTest(lane=lane):
                rows = ranker.rank_lane(self.index, bob, lane).rows
                self.assertFalse(_names(self.index, self.ids, rows) & ALICE_ONLY)
        allowed = _names(self.index, self.ids, np.flatnonzero(bob.scope))
        # His own reasons: subscriptions, related to his videos (one link), discovered, opened.
        self.assertTrue({"b1", "b2", "b3", "b4", "r1", "d1", "f1"} <= allowed)
        # vA2 is on the related list of f1, a video Bob opened: one link from his own.
        self.assertIn("vA2", allowed)
        self.assertFalse(allowed & {"vA", "w1", "w2", "sA"})

    def test_alice_is_offered_her_own(self) -> None:
        alice = _alice(self.index, self.ids)
        rows = _names(self.index, self.ids, ranker.rank_lane(self.index, alice, "all").rows)
        self.assertIn("vA", rows)
        self.assertIn("sA", _names(self.index, self.ids, ranker.rank_lane(self.index, alice, "shorts").rows))

    def test_a_video_outside_the_scope_is_not_a_bridge_in_the_walk(self) -> None:
        # Unscoped, Bob's walk reaches vA (it lists b1) and then w2 through it (vA and w2 both
        # list w1). Scoped, vA is not Bob's, so it is neither reached nor a bridge to w2.
        unscoped = ranker.prepare_user(self.index, ranker.UserSignals(subscribed_keys={"bobchan"}))
        for name in ("vA", "w2"):
            self.assertGreater(unscoped.graph[self.index.rows_for_ids([self.ids[name]])[0]], 0, name)
        bob = _bob(self.index, self.ids)
        for name in ("vA", "w1", "w2"):
            self.assertEqual(bob.graph[self.index.rows_for_ids([self.ids[name]])[0]], 0.0, name)
        r1 = self.index.rows_for_ids([self.ids["r1"]])[0]
        self.assertGreater(bob.graph[r1], 0)

    def test_his_own_reason_brings_a_video_in(self) -> None:
        # Bob subscribes to Alice's channel: its uploads are its public list, and his.
        signals = ranker.UserSignals(subscribed_keys={"bobchan", "alicechan"}, scoped=True)
        bob = ranker.prepare_user(self.index, signals)
        self.assertIn("vA", _names(self.index, self.ids, np.flatnonzero(bob.base_ok | bob.sub_mask)))
        # Bob was shown sA in his own search: a provenance row.
        signals = ranker.UserSignals(subscribed_keys={"bobchan"}, scoped=True, scope_ids={self.ids["sA"]}, own_ids={self.ids["sA"]})
        bob = ranker.prepare_user(self.index, signals)
        self.assertIn("sA", _names(self.index, self.ids, ranker.rank_lane(self.index, bob, "shorts").rows))

    def test_up_next_stays_inside_the_scope(self) -> None:
        bob = _bob(self.index, self.ids)
        rows, kinds = ranker.up_next_order(self.index, bob, int(self.index.rows_for_ids([self.ids["b2"]])[0]), 10)
        self.assertFalse(_names(self.index, self.ids, rows) & ALICE_ONLY)
        self.assertTrue(rows)

    def test_up_next_on_a_video_outside_the_cached_scope_offers_its_own_related_list(self) -> None:
        # Bob opens vA from a link (his scope was cached before the open was recorded): vA's
        # own related list is his to see, Alice's other videos are not.
        bob = _bob(self.index, self.ids)
        anchor = int(self.index.rows_for_ids([self.ids["vA"]])[0])
        rows, _ = ranker.up_next_order(self.index, bob, anchor, 10)
        names = _names(self.index, self.ids, rows)
        self.assertIn("w1", names)          # on vA's related list
        self.assertNotIn("w2", names)       # two links away through Alice's video
        self.assertNotIn("sA", names)

    def test_unscoped_and_a_claim_on_everything_rank_exactly_as_before(self) -> None:
        plain = ranker.prepare_user(self.index, ranker.UserSignals(subscribed_keys={"bobchan"}))
        self.assertIsNone(plain.scope)
        claimed = ranker.prepare_user(self.index, ranker.UserSignals(
            subscribed_keys={"bobchan"}, scoped=True, scope_through=int(self.index.ids.max())))
        self.assertTrue(claimed.scope.all())
        for lane in ("recommended", "all", "shorts"):
            with self.subTest(lane=lane):
                a, b = ranker.rank_lane(self.index, plain, lane), ranker.rank_lane(self.index, claimed, lane)
                self.assertEqual(a.rows.tolist(), b.rows.tolist())
                np.testing.assert_allclose(a.scores, b.scores)
        np.testing.assert_allclose(plain.graph, claimed.graph)

    def test_percentile_is_taken_within_the_scope(self) -> None:
        mask = np.zeros(self.index.n, dtype=bool)
        mask[:3] = True
        pct = ranker.scoped_percentile(self.index, mask)
        self.assertEqual(sorted(np.round(pct[:3], 4).tolist()), [round(x, 4) for x in (0.5 / 3, 1.5 / 3, 2.5 / 3)])

    def test_related_rows_follow_one_link_forward_only(self) -> None:
        own = np.zeros(self.index.n, dtype=bool)
        own[self.index.rows_for_ids([self.ids["w2"]])] = True
        self.assertEqual(_names(self.index, self.ids, np.flatnonzero(ranker.related_rows(self.index, own))), {"w1"})
        own[:] = False
        own[self.index.rows_for_ids([self.ids["w1"]])] = True   # w1 lists nothing; w2 listing w1 is not "related to w1"
        self.assertFalse(ranker.related_rows(self.index, own).any())


class RankerServingTests(unittest.TestCase):
    """The ranker's database reads: the scope is read with the signals, and the cache key moves with it."""

    def test_scope_is_read_only_when_it_applies(self) -> None:
        signals = ranker.UserSignals()
        cur = MagicMock()
        with patch.object(catalog_scope, "applies", return_value=False):
            self.assertFalse(ranker.with_scope(cur, "local", signals).scoped)
        cur.execute.assert_not_called()
        with patch.object(catalog_scope, "applies", return_value=True), \
                patch.object(catalog_scope, "scope_sets", return_value=(7, [9, 10], [9])) as sets:
            out = ranker.with_scope(cur, BOB, ranker.UserSignals())
        sets.assert_called_once_with(cur, BOB)
        self.assertEqual((out.scoped, out.scope_through, out.scope_ids, out.own_ids), (True, 7, {9, 10}, {9}))

    def test_a_failed_scope_read_is_not_an_unscoped_ranking(self) -> None:
        with patch.object(catalog_scope, "applies", return_value=True), \
                patch.object(catalog_scope, "scope_sets", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                ranker.with_scope(MagicMock(), BOB, ranker.UserSignals())

    def test_fingerprint_includes_the_scope(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {"scope": "3:2026"}
        with patch.object(catalog_scope, "applies", return_value=True):
            key = ranker.fingerprint(cur, BOB)
        self.assertIn("video_provenance", cur.execute.call_args.args[0])
        self.assertEqual(key[-1], "scoped:3:2026")
        with patch.object(catalog_scope, "applies", return_value=False):
            self.assertEqual(ranker.fingerprint(cur, "local")[-1], "unscoped")
        self.assertNotIn("video_provenance", cur.execute.call_args.args[0])


# --- the SQL side --------------------------------------------------------------------------


class ScopedSqlTests(unittest.TestCase):
    """Each per-person listing carries the scope predicate, with its params in the right place."""

    def setUp(self) -> None:
        self.enterContext(patch.object(catalog_scope, "applies", side_effect=lambda key: key != "local"))

    def _assert_scoped(self, query: str, params: Any, user_key: str) -> None:
        self.assertIn("openfeed_video_scope(%s)", query)
        self.assertIn("catalog_claims", query)
        self.assertEqual(placeholder_count(query), len(params))
        # Both scope placeholders carry the caller's key.
        before_scope = query[: query.index("catalog_claims")]
        position = placeholder_count(before_scope)
        self.assertEqual(params[position:position + 2], (user_key, user_key))

    def test_library_search_is_scoped_and_keeps_its_union(self) -> None:
        query, params = _capture_sql(lambda: db.search_videos("gardening", 40, user_key=BOB))
        self._assert_scoped(query, params, BOB)
        self.assertIn("WITH hits AS", query)
        self.assertIn("UNION", query)
        # The scope filters the UNION's few hits, not each branch: the branches keep their indexes.
        hits = query[query.index("WITH hits AS"): query.index("SELECT v.id, v.youtube_video_id")]
        self.assertNotIn("openfeed_video_scope", hits)

    def test_empty_search_lists_the_callers_recent_videos(self) -> None:
        query, params = _capture_sql(lambda: db.search_videos("  ", 20, user_key=BOB))
        self._assert_scoped(query, params, BOB)

    def test_each_listing(self) -> None:
        self.enterContext(patch.object(db, "_apply_recommendation_scores", side_effect=lambda rows, *a, **k: rows))
        cases = {
            "Shorts": lambda: db.list_short_videos(12, BOB, 0),
            "recent": lambda: db.list_recent_videos(30, BOB),
            "discovery": lambda: db.list_discovery_videos(10, BOB),
            "classic ranker": lambda: db._list_recommended_videos_classic(30, 0, "recommended", BOB),
            "topic chips": lambda: db.list_popular_tags(8, user_key=BOB),
            "library labels": lambda: db.video_ids_in_library(["abcdefghijk"], BOB),
        }
        for name, call in cases.items():
            with self.subTest(name):
                calls = _capture_all_sql(call)
                scoped = [(q, p) for q, p in calls if "openfeed_video_scope" in q]
                self.assertTrue(scoped, f"{name} is not scoped")
                query, params = scoped[0]
                self._assert_scoped(query, tuple(params), BOB)

    def test_channel_search_lists_followed_or_scoped_channels_only(self) -> None:
        query, params = _capture_sql(lambda: db.search_channels("garden", 10, user_key=BOB))
        self.assertIn("fs.id IS NOT NULL OR EXISTS", query)
        self._assert_scoped(query, tuple(params), BOB)

    def test_a_single_person_install_reads_the_whole_catalog_as_before(self) -> None:
        self.enterContext(patch.object(db, "_apply_recommendation_scores", side_effect=lambda rows, *a, **k: rows))
        for call in (lambda: db.search_videos("gardening", 40, user_key="local"),
                     lambda: db.list_short_videos(12, "local", 0),
                     lambda: db._list_recommended_videos_classic(30, 0, "recommended", "local"),
                     lambda: db.search_channels("garden", 10, user_key="local")):
            for query, params in _capture_all_sql(call):
                self.assertNotIn("openfeed_video_scope", query)
                self.assertNotIn("catalog_claims", query)

    def test_server_side_reads_stay_unscoped(self) -> None:
        for query, _ in _capture_all_sql(lambda: db.video_ids_in_library(["abcdefghijk"])) + \
                _capture_all_sql(lambda: db.list_recent_videos(8)):
            self.assertNotIn("openfeed_video_scope", query)

    def test_search_results_are_recorded_for_the_searcher_only(self) -> None:
        with patch.object(db, "upsert_discovered_video", return_value=5), \
                patch.object(catalog_scope, "record_rows") as record:
            db.store_search_results([{"youtube_video_id": "abcdefghijk"}], "q", BOB)
        record.assert_called_once_with(BOB, [{"youtube_video_id": "abcdefghijk"}], "search")

    def test_takeout_import_records_imported_watches(self) -> None:
        conn = MagicMock()
        conn.__enter__.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"id": "x"}
        with patch.object(db, "connect", return_value=conn), \
                patch.object(db, "store_takeout_subscriptions"), patch.object(db, "clear_takeout_cache"):
            db.store_takeout_import({"watched_videos": [{"youtube_video_id": "abcdefghijk"}]}, {}, BOB)
        insert = next(c for c in cur.execute.call_args_list if "INSERT INTO video_provenance" in c.args[0])
        self.assertIn("'takeout'", insert.args[0])
        self.assertIn("WHERE t.user_key = %s", insert.args[0])
        self.assertEqual(insert.args[1], (BOB,))


class FilterAndStateTests(unittest.TestCase):
    def _settings(self, sign_in: bool) -> Any:
        return SimpleNamespace(sign_in_enabled=sign_in)

    def test_applies(self) -> None:
        catalog_scope.clear_cache()
        self.addCleanup(catalog_scope.clear_cache)
        with patch("openfeed.config.get_settings", return_value=self._settings(True)):
            self.assertTrue(catalog_scope.applies("local"))
            self.assertTrue(catalog_scope.applies(BOB))
        with patch("openfeed.config.get_settings", return_value=self._settings(False)), \
                patch.object(catalog_scope, "_read_shared", return_value=False):
            catalog_scope.clear_cache()
            self.assertFalse(catalog_scope.applies("local"))            # the one person
            self.assertTrue(catalog_scope.applies("local:p:1234"))      # a household profile
            self.assertTrue(catalog_scope.applies(""))                  # nobody: nothing
        with patch("openfeed.config.get_settings", return_value=self._settings(False)), \
                patch.object(catalog_scope, "_read_shared", return_value=True):
            catalog_scope.clear_cache()
            self.assertTrue(catalog_scope.applies("local"))             # once shared, for good

    def test_an_unreadable_state_counts_as_shared(self) -> None:
        with patch.object(db, "connect", side_effect=OSError("down")):
            self.assertTrue(catalog_scope._read_shared())

    def test_filter_sql(self) -> None:
        with patch.object(catalog_scope, "applies", return_value=False):
            self.assertEqual(catalog_scope.video_filter_sql("local"), ("", ()))
        with patch.object(catalog_scope, "applies", return_value=True):
            sql, params = catalog_scope.video_filter_sql(BOB, "x.id")
        self.assertTrue(sql.startswith(" AND (x.id <= "))
        self.assertIn("x.id IN (SELECT openfeed_video_scope(%s))", sql)
        self.assertEqual(params, (BOB, BOB))


class _Cursor:
    """Answers the few queries the startup and claim code runs."""

    def __init__(self, keys: list[str], shared: bool = False, profiles_exist: bool = False, top: int = 900) -> None:
        self.keys, self.shared, self.profiles_exist, self.top = keys, shared, profiles_exist, top
        self.calls: list[tuple[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        self.rowcount = 1
        if "FROM app_settings" in query:
            self._rows = [{"value": {"shared": True}}] if self.shared else []
        elif "max(id)" in query:
            self._rows = [{"top": self.top}]
        elif "account_profiles WHERE account_key" in query:
            self._rows = [{"?column?": 1}] if self.profiles_exist else []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def statements(self) -> list[str]:
        return [q for q, _ in self.calls]


class ClaimAndUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog_scope.clear_cache()
        self.addCleanup(catalog_scope.clear_cache)

    def test_the_single_person_who_built_the_catalog_claims_it(self) -> None:
        cur = _Cursor(["local"])
        with patch("openfeed.migrations.existing_user_keys", return_value=["local", "local:p:abc"]):
            result = catalog_scope.mark_shared(cur, "profiles")
        self.assertEqual(result["through_video_id"], 900)
        claim = next((q, p) for q, p in cur.calls if "INSERT INTO catalog_claims" in q)
        self.assertEqual(claim[1], ("local", 900))
        self.assertTrue(any("INSERT INTO app_settings" in q for q in cur.statements()))

    def test_nobody_claims_a_catalog_several_people_built(self) -> None:
        cur = _Cursor([])
        with patch("openfeed.migrations.existing_user_keys", return_value=["local", ALICE, BOB]):
            result = catalog_scope.mark_shared(cur, "sign-in")
        self.assertEqual(result["through_video_id"], 0)
        self.assertFalse(any("INSERT INTO catalog_claims" in q for q in cur.statements()))

    def test_marking_twice_changes_nothing(self) -> None:
        cur = _Cursor([], shared=True)
        self.assertEqual(catalog_scope.mark_shared(cur, "profiles"), {"changed": False})
        self.assertFalse(any("INSERT" in q for q in cur.statements()))

    def test_upgrade_backfills_every_persons_own_history(self) -> None:
        cur = _Cursor([])
        with patch("openfeed.config.get_settings", return_value=SimpleNamespace(sign_in_enabled=False)):
            result = catalog_scope.at_startup(cur, created=True)
        sources = [p[0] for q, p in cur.calls if "INSERT INTO video_provenance" in q]
        self.assertEqual(sources, ["watch", "event", "feedback", "playlist", "download", "takeout"])
        self.assertNotIn("shared", result)   # one person, no profiles: stays unscoped
        for query, _ in cur.calls:
            if "INSERT INTO video_provenance" in query:
                self.assertIn("ON CONFLICT (user_key, video_id) DO NOTHING", query)

    def test_startup_marks_shared_with_sign_in_or_profiles(self) -> None:
        for sign_in, profiles_exist, expect in ((True, False, True), (False, True, True), (False, False, False)):
            with self.subTest(sign_in=sign_in, profiles=profiles_exist):
                cur = _Cursor([], profiles_exist=profiles_exist)
                with patch("openfeed.config.get_settings", return_value=SimpleNamespace(sign_in_enabled=sign_in)), \
                        patch.object(catalog_scope, "mark_shared", return_value={"changed": True}) as mark:
                    catalog_scope.at_startup(cur, created=False)
                self.assertEqual(mark.called, expect)
                self.assertFalse(any("INSERT INTO video_provenance" in q for q in cur.statements()))

    def test_adding_a_household_profile_marks_the_install_shared(self) -> None:
        cur = MagicMock()
        cur.fetchone.side_effect = [{"n": 1, "top": 0}, None, {"id": "x", "account_key": "local", "user_key": "local:p:1",
                                                                     "name": "Kid", "color": "graphite", "is_kids": False,
                                                                     "is_main": False, "position": 1, "created_at": None}]
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(db, "connect", return_value=conn), \
                patch.object(profiles, "_lock_account"), patch.object(profiles, "_ensure_main"), \
                patch.object(catalog_scope, "mark_shared") as mark:
            profiles.create_profile("local", "Kid")
        mark.assert_called_once_with(cur, "profiles")

    def test_provenance_and_claims_leave_with_their_person(self) -> None:
        self.assertIn("video_provenance", profiles.USER_KEY_TABLES)
        self.assertIn("catalog_claims", profiles.USER_KEY_TABLES)


class RecordingTests(unittest.TestCase):
    def test_record_by_youtube_id(self) -> None:
        with patch("openfeed.config.get_settings", return_value=SimpleNamespace(sign_in_enabled=True)):
            query, params = _capture_sql(lambda: catalog_scope.record_youtube_ids(BOB, ["abcdefghijk", "abcdefghijk", ""], "search"))
        self.assertIn("INSERT INTO video_provenance", query)
        self.assertIn("ON CONFLICT (user_key, video_id) DO NOTHING", query)
        self.assertEqual(params, (BOB, "search", ["abcdefghijk"]))

    def test_nothing_is_recorded_for_nobody_or_an_unknown_reason(self) -> None:
        with patch("openfeed.config.get_settings", return_value=SimpleNamespace(sign_in_enabled=True)), \
                patch.object(db, "connect", side_effect=AssertionError("no write expected")):
            self.assertEqual(catalog_scope.record_youtube_ids("", ["abcdefghijk"], "search"), 0)
            self.assertEqual(catalog_scope.record_youtube_ids(BOB, ["abcdefghijk"], "made-up"), 0)
            # With sign-in on, `local` is the worker's own background work, not a person.
            self.assertEqual(catalog_scope.record_video_ids("local", [1, 2], "discovery"), 0)

    def test_a_failed_write_is_not_fatal(self) -> None:
        with patch("openfeed.config.get_settings", return_value=SimpleNamespace(sign_in_enabled=True)), \
                patch.object(db, "connect", side_effect=OSError("down")):
            self.assertEqual(catalog_scope.record_rows(BOB, [{"youtube_video_id": "abcdefghijk"}], "channel"), 0)

    def test_discovery_runs_record_for_who_started_them(self) -> None:
        with patch.object(discovery, "fetch_trending", return_value=[{"youtube_video_id": "abcdefghijk"}]), \
                patch.object(db, "upsert_discovered_video", return_value=41), \
                patch.object(catalog_scope, "record_video_ids") as record:
            discovery.seed_trending(categories=("",), user_key=BOB)
        record.assert_called_once_with(BOB, [41], "discovery")


# --- schema ---------------------------------------------------------------------------------


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = db.SCHEMA_PATH.read_text(encoding="utf-8")

    def test_provenance_table_is_lean_deduplicated_and_indexed(self) -> None:
        table = re.search(r"CREATE TABLE IF NOT EXISTS video_provenance \((.*?)\n\);", self.schema, re.S).group(1)
        for column in ("user_key TEXT NOT NULL", "video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE",
                       "source TEXT NOT NULL", "first_seen TIMESTAMPTZ", "PRIMARY KEY (user_key, video_id)"):
            self.assertIn(column, table)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_video_provenance_video ON video_provenance (video_id)", self.schema)

    def test_history_writes_record_provenance_through_triggers(self) -> None:
        for table in ("playback_progress", "user_video_events", "user_video_feedback", "downloads", "playlist_items"):
            with self.subTest(table):
                self.assertRegex(self.schema, rf"CREATE TRIGGER openfeed_provenance_\w+ AFTER INSERT ON {table}\b")
        # Removing a video from history forgets that it was watched.
        self.assertRegex(self.schema, r"CREATE TRIGGER openfeed_provenance_progress_forget AFTER DELETE ON playback_progress")
        forget = re.search(r"FUNCTION openfeed_forget_watch_provenance\(\).*?\$\$(.*?)\$\$;", self.schema, re.S).group(1)
        self.assertIn("source IN ('watch', 'event')", forget)
        # Idempotent on every start.
        self.assertEqual(self.schema.count("CREATE TRIGGER"), self.schema.count("IF NOT EXISTS (SELECT 1 FROM pg_trigger"))

    def test_scope_function_reads_the_person_only(self) -> None:
        body = re.search(r"CREATE OR REPLACE FUNCTION openfeed_video_scope\(p_user TEXT\).*?\$\$(.*?)\$\$;", self.schema, re.S).group(1)
        for source in ("video_provenance vp WHERE vp.user_key = p_user", "us.user_key = p_user", "video_discoveries",
                       "catalog_claims cc WHERE cc.user_key = p_user"):
            self.assertIn(source, body)
        self.assertNotIn("discovery_source", body)   # no install-wide source is everyone's


# --- the routes: two accounts ---------------------------------------------------------------


from openfeed.tests.test_isolation import VIDEO, _TwoAccounts  # noqa: E402


class TwoAccountRouteTests(_TwoAccounts):
    """Alice searched; Bob's search, Shorts, Home chips and video list ask for Bob's scope only."""

    def test_search_reads_and_records_the_callers_scope(self) -> None:
        library = self.own(db, "search_videos", [{"youtube_video_id": VIDEO, "title": "Alice's search"}], [])
        stored = self.own(db, "store_search_results", 1, 0)
        with patch.object(main.discovery, "search_youtube", return_value=[{"youtube_video_id": "Bobsresult1", "title": "From YouTube"}]), \
                patch.object(main, "_search_channels", return_value=([], "", False)), \
                patch.object(main.search_extras, "playlist_matches", return_value=[]):
            page = self.b.get("/search", params={"q": "origami"}).text
            api = self.b.get("/api/search", params={"q": "origami", "source": "library"}).json()
            more = self.b.get("/api/search/html", params={"q": "origami", "page": 1}).json()
        self.assertNotIn("Alice", page)
        self.assertEqual(api["items"], [])
        self.assertNotIn("Alice", more["html"])
        self.assertOnlyBob(library)
        self.assertOnlyBob(stored)

    def test_later_search_pages_label_by_the_callers_library(self) -> None:
        known = self.own(db, "video_ids_in_library", {VIDEO}, set())
        self.own(db, "store_search_results", 1, 0)
        with patch.object(main.discovery, "search_youtube", return_value=[{"youtube_video_id": VIDEO, "title": "Same video"}]):
            html = self.b.get("/api/search/html", params={"q": "origami", "page": 2}).json()["html"]
        self.assertOnlyBob(known)
        # Alice's stored copy does not hide it from Bob as "already shown".
        self.assertIn(VIDEO, html)

    def test_shorts_and_home_chips(self) -> None:
        shorts = self.own(db, "list_short_videos", [{"youtube_video_id": VIDEO, "title": "Alice's short", "url": f"/shorts/{VIDEO}"}], [])
        tags = self.own(db, "list_popular_tags", [{"tag": "aliceorigami"}], [])
        with patch.object(main, "_shorts_hidden", return_value=False), patch.object(main, "_kids_profile", return_value=False):
            self.assertNotIn("Alice", self.b.get("/api/shorts").json()["html"])
            self.assertNotIn("Alice", self.b.get("/shorts").text)
        self.assertOnlyBob(shorts)
        with patch.object(db, "list_subscription_recommendations", return_value=[]):
            self.assertNotIn("aliceorigami", self.b.get("/").text)
        self.assertOnlyBob(tags)

    def test_type_ahead_never_reads_the_library(self) -> None:
        with patch.object(main.search_extras, "suggestions", return_value=["origami crane"]) as suggest, \
                patch.object(db, "search_videos", side_effect=AssertionError("suggestions read the library")):
            self.assertEqual(self.b.get("/api/search/suggestions", params={"q": "orig"}).json()["suggestions"], ["origami crane"])
        self.assertNotEqual(suggest.call_args.kwargs["scope"], "")

    def test_up_next_falls_back_to_the_callers_own_pool(self) -> None:
        # With v2 unavailable, Up next re-sorts the caller's recommendation and subscription
        # pools, both scoped to the caller (classic SQL above, subscriptions by definition).
        pool = self.own(db, "list_recommended_videos", [{"youtube_video_id": "Alicesnext1", "title": "Alice's next"}], [])
        subs = self.own(db, "list_subscription_recommendations", [], [])
        with patch.object(db, "get_video_by_youtube_id", return_value={"youtube_video_id": VIDEO, "title": "x"}), \
                patch.object(ranker, "up_next_rows", return_value=None):
            rows = db.list_related_videos(VIDEO, 14, self.B)
        self.assertEqual(rows, [])
        self.assertOnlyBob(pool)
        self.assertOnlyBob(subs)

if __name__ == "__main__":
    unittest.main()
