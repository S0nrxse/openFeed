"""Library search uses the description full-text index once it exists, and ILIKE before.

A single WHERE that ORs every field forces a sequential scan, which made Postgres compute
the description tsvector for every row (about 25 s). The query therefore collects matches
in a UNION, so the index branch can use the GIN index. DB-free: the SQL is captured.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import db
from openfeed.tests.sqlcapture import _capture_sql


def _search_sql(valid: bool) -> tuple[str, tuple]:
    with patch.object(db, "_search_index_valid", return_value=valid):
        query, params = _capture_sql(lambda: db.search_videos("Gardening tips", 50))
    return query, tuple(params)


class SearchIndexTests(unittest.TestCase):
    def test_index_path_uses_phrase_query_inside_a_union(self) -> None:
        query, params = _search_sql(True)
        self.assertIn("UNION", query)
        self.assertIn("phraseto_tsquery('simple', %s)", query)
        self.assertIn(db._SEARCH_TEXT_TSV_V, query)
        self.assertNotIn("v.description ILIKE", query)
        # The phrase is passed raw, not as an ILIKE pattern.
        self.assertIn("Gardening tips", params)
        self.assertEqual(query.count("%s"), len(params))

    def test_index_expression_matches_the_query_expression(self) -> None:
        # The planner only uses the index when the expressions are identical up to the alias.
        self.assertEqual(db._SEARCH_TEXT_TSV_V.replace("v.", ""), db._SEARCH_TEXT_TSV)

    def test_without_index_descriptions_fall_back_to_ilike(self) -> None:
        query, params = _search_sql(False)
        self.assertIn("v.description ILIKE %s", query)
        self.assertNotIn("tsquery", query)
        self.assertEqual(query.count("%s"), len(params))

    def test_filters_apply_to_the_scored_rows(self) -> None:
        with patch.object(db, "_search_index_valid", return_value=True):
            query, _ = _capture_sql(lambda: db.search_videos("x", 50, duration="short"))
        outer = query.split("FROM hits h", 1)[1]
        self.assertIn("v.duration_seconds", outer)


if __name__ == "__main__":
    unittest.main()
