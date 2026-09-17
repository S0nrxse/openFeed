from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from openfeed import db


class _FakeCursor:
    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    def execute(self, query: str, params: Any = None) -> None:
        self._seen.append(query)

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConnection:
    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    def cursor(self) -> Any:
        cursor = _FakeCursor(self._seen)

        @contextmanager
        def _use():
            yield cursor

        return _use()


class RankingQueryPlanTests(unittest.TestCase):
    """The ranking queries full-scan the library, so their plan shape is load-bearing.

    The estimator predicts ~1 row for the ~20k-row candidate set and nest-loops
    every helper relation once per video row (tens of millions of comparisons,
    multi-second renders). These tests pin the two structural defenses without
    needing a database: single-evaluation helper CTEs and join settings that
    take the nested loop off the table.
    """

    def _statements(self, call) -> list[str]:
        seen: list[str] = []

        @contextmanager
        def _fake_connect():
            yield _FakeConnection(seen)

        with (
            patch.object(db, "connect", _fake_connect),
            patch.object(db, "_apply_recommendation_scores", return_value=[]),
            patch.object(db, "_diversify_recommendations", return_value=[]),
        ):
            call()
        return seen

    def _assert_ranking_shape(self, statements: list[str]) -> None:
        text = "\n".join(statements)
        self.assertIn("event_rollup AS MATERIALIZED", text)
        self.assertIn("fe_latest AS MATERIALIZED", text)
        self.assertIn("SET LOCAL enable_nestloop = off", text)
        self.assertIn("SET LOCAL work_mem", text)
        # The dedup must only exist as a CTE, never again as an inline derived
        # table (which the planner nest-loops once per video row).
        self.assertNotIn("LEFT JOIN (\n                    SELECT DISTINCT ON", text)

    def test_recommended_lane_query_shape(self) -> None:
        statements = self._statements(lambda: db.list_recommended_videos(54, 0, "recommended", "local"))
        self._assert_ranking_shape(statements)

    def test_subscription_ranking_query_shape(self) -> None:
        statements = self._statements(lambda: db.list_subscription_recommendations(160, "local"))
        self._assert_ranking_shape(statements)

    def test_discovery_ranking_query_shape(self) -> None:
        statements = self._statements(lambda: db.list_discovery_videos(50, "local"))
        self._assert_ranking_shape(statements)
