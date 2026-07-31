"""Shared helpers for tests that render pages without a database.

Most of the suite is pure functions over fixtures and needs nothing running. A few tests
render whole pages, and a page reaches the database for the panels it draws — which made
`python -m unittest discover` fail on a fresh clone with three errors that look like a
broken project rather than a missing Postgres.

`offline_db()` stands in for the handful of reads those renders perform, so the tests
assert on the markup they are actually about. It deliberately does not fake `connect`
itself: a test that reaches an unlisted query should fail loudly and be added here,
rather than silently receiving an empty result and asserting on a page that never
rendered.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator
from unittest.mock import patch

# What each read returns when there is nothing behind it: the shape a fresh install has.
OFFLINE_RESULTS: dict[str, Any] = {
    "get_feed_preferences": {},
    "latest_takeout_import": None,
    "list_recommended_videos": [],
    "system_counts": {},
    "db_status": {"ok": False, "detail": "database not configured"},
    # The status pane summarises the worker's recent activity.
    "enrichment_status": {},
    "list_recent_retrieval_attempts": [],
    "list_sync_runs": [],
    "retrieval_attempt_rollup": [],
}


@contextlib.contextmanager
def offline_db(**overrides: Any) -> Iterator[None]:
    """Render pages with the database reads stubbed out.

    Pass a keyword per function name to override any individual result, e.g.
    ``offline_db(latest_takeout_import={"status": "complete"})``.
    """
    from openfeed import db

    results = {**OFFLINE_RESULTS, **overrides}
    with contextlib.ExitStack() as stack:
        for name, value in results.items():
            stack.enter_context(patch.object(db, name, return_value=value))
        yield
