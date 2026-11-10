"""Tests for the direct-RSS polling helpers in ``openfeed.db``.

The poller fills the feeds and feed_entries tables;
these tests cover only the polling-state helpers (due selection, fetch outcome
recording, channel->subscription lookup, newest sync run).

DB-free: ``db.connect`` is patched with a fake whose cursor emulates just enough
Postgres semantics for these queries against an in-memory store, in the style of
test_db_queries.py / sqlcapture.py. The real database is never
touched. For the due query the fake asserts the SQL carries the contract clauses
(identity filter, backoff filter, NULLS FIRST ordering) and then applies that
same contract in Python; the backoff math lives in Python in ``record_rss_fetch``
so the fake applies the UPDATE verbatim and the tests assert exact numbers.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Make `from openfeed import db` work however the suite is launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# psycopg is a runtime dependency that may not be installed where tests run;
# db.py only needs it inside connect(), which these tests stub out.
if "psycopg" not in sys.modules:
    _psycopg = types.ModuleType("psycopg")
    _rows = types.ModuleType("psycopg.rows")
    _rows.dict_row = object  # type: ignore[attr-defined]
    _psycopg.rows = _rows  # type: ignore[attr-defined]
    sys.modules["psycopg"] = _psycopg
    sys.modules["psycopg.rows"] = _rows

from openfeed import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


class _FakeCursor:
    """Emulates the polling queries against an in-memory store.

    store["subs"]: id -> dict with keys id, youtube_channel_id, feed_url, etag,
    last_modified, last_checked_at, fetch_failures, next_fetch_after.
    store["sync_runs"]: list of dicts with id, source, status, message,
    started_at, finished_at, subscriptions_count, entries_count.
    """

    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.calls: list[tuple[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self._one: dict[str, Any] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        params = params or ()
        if "FROM feeds" in query and "next_fetch_after <=" in query:
            self._run_due_query(query, params)
        elif "SELECT fetch_failures" in query:
            sub = self.store["subs"].get(params[0], {})
            self._one = {"fetch_failures": sub.get("fetch_failures", 0)}
            self._rows = []
        elif "UPDATE feeds" in query and "fetch_failures = 0" in query:
            etag, last_modified, ttl, sub_id = params
            sub = self.store["subs"][params[3]]
            now = _utcnow()
            if etag is not None:
                sub["etag"] = etag
            if last_modified is not None:
                sub["last_modified"] = last_modified
            sub["fetch_failures"] = 0
            sub["last_checked_at"] = now
            sub["next_fetch_after"] = now + timedelta(seconds=int(ttl))
            self._rows = []
        elif "UPDATE feeds" in query and "fetch_failures = %s" in query:
            failures, backoff, sub_id = params
            sub = self.store["subs"][sub_id]
            now = _utcnow()
            sub["fetch_failures"] = int(failures)
            sub["last_checked_at"] = now
            sub["next_fetch_after"] = now + timedelta(seconds=int(backoff))
            self._rows = []
        elif "FROM feeds" in query and "youtube_channel_id = %s" in query:
            ids = sorted(
                sub["id"]
                for sub in self.store["subs"].values()
                if sub.get("youtube_channel_id") == params[0]
            )
            self._rows = [{"id": item} for item in ids]
            self._one = self._rows[0] if self._rows else None
        elif "FROM sync_runs" in query:
            runs = [r for r in self.store["sync_runs"] if r.get("source") == params[0]]
            runs.sort(key=lambda r: (r["started_at"], r["id"]), reverse=True)
            self._rows = [dict(runs[0])] if runs else []
            self._one = dict(runs[0]) if runs else None
        else:  # pragma: no cover - every polling query above must be handled
            raise AssertionError(f"unexpected query in rss-db fake: {query[:120]!r}")

    def _run_due_query(self, query: str, params: Any) -> None:
        # The contract lives in the SQL; refuse to emulate a query that lost it.
        for marker in (
            "youtube_channel_id IS NOT NULL",
            "feed_url IS NOT NULL",
            "next_fetch_after IS NULL",
            "last_checked_at",
            "NULLS FIRST",
        ):
            assert marker in query, f"due query lost contract clause: {marker}"
        now = _utcnow()
        due = [
            sub
            for sub in self.store["subs"].values()
            if (sub.get("youtube_channel_id") is not None or sub.get("feed_url") is not None)
            and (sub.get("next_fetch_after") is None or sub["next_fetch_after"] <= now)
        ]
        due.sort(
            key=lambda s: (
                s.get("last_checked_at") is not None,
                s.get("last_checked_at") or _EPOCH,
                s.get("next_fetch_after") is not None,
                s.get("next_fetch_after") or _EPOCH,
                s["id"],
            )
        )
        limited = due[: int(params[0])]
        self._rows = [
            {
                "id": s["id"],
                "youtube_channel_id": s.get("youtube_channel_id"),
                "feed_url": s.get("feed_url"),
                "etag": s.get("etag"),
                "last_modified": s.get("last_modified"),
            }
            for s in limited
        ]
        self._one = self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return dict(self._one) if self._one is not None else None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        return None


def _sub(
    sub_id: str,
    channel: str | None = "chan",
    feed_url: str | None = None,
    *,
    last_checked_at: datetime | None = None,
    next_fetch_after: datetime | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    fetch_failures: int = 0,
) -> dict[str, Any]:
    return {
        "id": sub_id,
        "youtube_channel_id": channel,
        "feed_url": feed_url,
        "etag": etag,
        "last_modified": last_modified,
        "last_checked_at": last_checked_at,
        "fetch_failures": fetch_failures,
        "next_fetch_after": next_fetch_after,
    }


class RssDbTests(unittest.TestCase):
    def _run(self, store: dict[str, Any], callback: Any) -> tuple[Any, _FakeCursor]:
        cursor = _FakeCursor(store)
        with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
            return callback(), cursor

    def _store(self, subs: list[dict[str, Any]], runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"subs": {s["id"]: s for s in subs}, "sync_runs": list(runs or [])}

    # -- due selection --------------------------------------------------------

    def test_due_never_checked_first_then_stalest(self) -> None:
        now = _utcnow()
        store = self._store(
            [
                _sub("feed/recent", last_checked_at=now - timedelta(minutes=5)),
                _sub("feed/never"),
                _sub("feed/old", last_checked_at=now - timedelta(days=2)),
            ]
        )
        rows, _ = self._run(store, lambda: db.list_rss_due_channels(10))
        self.assertEqual([r["id"] for r in rows], ["feed/never", "feed/old", "feed/recent"])
        self.assertEqual(
            set(rows[0]),
            {"id", "youtube_channel_id", "feed_url", "etag", "last_modified"},
        )

    def test_due_excludes_backoff_held_and_identity_less_rows(self) -> None:
        now = _utcnow()
        store = self._store(
            [
                _sub("feed/held", next_fetch_after=now + timedelta(hours=1)),
                _sub("feed/due", next_fetch_after=now - timedelta(seconds=1)),
                _sub("feed/open"),
                _sub("feed/bare", channel=None, feed_url=None),
                _sub("feed/url-only", channel=None, feed_url="https://example.com/feed.xml"),
            ]
        )
        rows, _ = self._run(store, lambda: db.list_rss_due_channels(10))
        ids = [r["id"] for r in rows]
        self.assertNotIn("feed/held", ids)
        self.assertNotIn("feed/bare", ids)
        self.assertIn("feed/due", ids)
        self.assertIn("feed/open", ids)
        self.assertIn("feed/url-only", ids)

    def test_due_limit_capped_at_500(self) -> None:
        store = self._store([_sub(f"feed/{index:04d}") for index in range(600)])
        rows, cursor = self._run(store, lambda: db.list_rss_due_channels(10_000))
        self.assertEqual(len(rows), 500)
        self.assertEqual(cursor.calls[-1][1], (500,))

    def test_due_limit_floored_at_1(self) -> None:
        store = self._store([_sub("feed/a"), _sub("feed/b")])
        rows, cursor = self._run(store, lambda: db.list_rss_due_channels(0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(cursor.calls[-1][1], (1,))

    # -- fetch outcome recording ----------------------------------------------

    def test_record_ok_resets_failures_and_schedules_next(self) -> None:
        store = self._store(
            [_sub("feed/1", etag='"old"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT", fetch_failures=3)]
        )
        before = _utcnow()
        _, cursor = self._run(
            store,
            lambda: db.record_rss_fetch("feed/1", etag='"new"', last_modified="Tue, 02 Jan 2024 00:00:00 GMT", ttl_seconds=3600),
        )
        sub = store["subs"]["feed/1"]
        self.assertEqual(sub["fetch_failures"], 0)
        self.assertEqual(sub["etag"], '"new"')
        self.assertEqual(sub["last_modified"], "Tue, 02 Jan 2024 00:00:00 GMT")
        self.assertGreaterEqual(sub["last_checked_at"], before)
        self.assertLess(sub["next_fetch_after"] - sub["last_checked_at"], timedelta(seconds=3601))
        self.assertGreaterEqual(sub["next_fetch_after"] - sub["last_checked_at"], timedelta(seconds=3600))
        update_sql = cursor.calls[-1][0]
        self.assertIn("last_checked_at = now()", update_sql)
        self.assertIn("fetch_failures = 0", update_sql)

    def test_record_ok_keeps_old_validators_when_none(self) -> None:
        store = self._store([_sub("feed/1", etag='"keep"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")])
        self._run(store, lambda: db.record_rss_fetch("feed/1"))
        sub = store["subs"]["feed/1"]
        self.assertEqual(sub["etag"], '"keep"')
        self.assertEqual(sub["last_modified"], "Mon, 01 Jan 2024 00:00:00 GMT")
        self.assertEqual(sub["fetch_failures"], 0)
        self.assertIsNotNone(sub["next_fetch_after"])

    def test_record_failure_backoff_doubles(self) -> None:
        cases = [
            (0, 1, 3600),  # first failure waits an hour
            (1, 2, 7200),
            (2, 3, 14400),
            (4, 5, 57600),
        ]
        for starting, expected_failures, expected_backoff in cases:
            with self.subTest(starting=starting):
                store = self._store([_sub("feed/1", fetch_failures=starting)])
                _, cursor = self._run(store, lambda: db.record_rss_fetch("feed/1", ok=False))
                sub = store["subs"]["feed/1"]
                self.assertEqual(sub["fetch_failures"], expected_failures)
                _failures, backoff, _sub_id = cursor.calls[-1][1]
                self.assertEqual(backoff, expected_backoff)
                self.assertEqual(
                    sub["next_fetch_after"] - sub["last_checked_at"],
                    timedelta(seconds=expected_backoff),
                )
                self.assertIsNotNone(sub["last_checked_at"])

    def test_record_failure_backoff_caps_at_one_day(self) -> None:
        store = self._store([_sub("feed/1", fetch_failures=10)])
        _, cursor = self._run(store, lambda: db.record_rss_fetch("feed/1", ok=False))
        sub = store["subs"]["feed/1"]
        self.assertEqual(sub["fetch_failures"], 11)
        _failures, backoff, _sub_id = cursor.calls[-1][1]
        self.assertEqual(backoff, 86400)

    # -- channel lookup --------------------------------------------------------

    def test_subscription_id_for_channel_returns_lowest_id(self) -> None:
        store = self._store(
            [
                _sub("feed/9", channel="UCabc"),
                _sub("feed/3", channel="UCabc"),
                _sub("feed/5", channel="UCother"),
            ]
        )
        row_id, _ = self._run(store, lambda: db.subscription_id_for_channel("UCabc"))
        self.assertEqual(row_id, "feed/3")

    def test_subscription_id_for_channel_none_when_missing(self) -> None:
        store = self._store([_sub("feed/3", channel="UCother")])
        row_id, _ = self._run(store, lambda: db.subscription_id_for_channel("UCnope"))
        self.assertIsNone(row_id)
        self.assertIsNone(db.subscription_id_for_channel(""))

    # -- latest sync run ---------------------------------------------------------

    def test_latest_sync_run_none_when_no_rows(self) -> None:
        store = self._store([], runs=[])
        row, _ = self._run(store, lambda: db.latest_sync_run("rss"))
        self.assertIsNone(row)

    def test_latest_sync_run_returns_newest_row_for_source(self) -> None:
        now = _utcnow()
        store = self._store(
            [],
            runs=[
                {
                    "id": 1,
                    "source": "rss",
                    "status": "ok",
                    "message": "first",
                    "started_at": now - timedelta(hours=2),
                    "finished_at": now - timedelta(hours=2) + timedelta(seconds=5),
                    "subscriptions_count": 10,
                    "entries_count": 40,
                },
                {
                    "id": 2,
                    "source": "other",
                    "status": "ok",
                    "message": "other source",
                    "started_at": now - timedelta(minutes=1),
                    "finished_at": now,
                    "subscriptions_count": 99,
                    "entries_count": 99,
                },
                {
                    "id": 3,
                    "source": "rss",
                    "status": "error",
                    "message": "boom",
                    "started_at": now - timedelta(hours=1),
                    "finished_at": now - timedelta(hours=1) + timedelta(seconds=2),
                    "subscriptions_count": 3,
                    "entries_count": 7,
                },
            ],
        )
        row, _ = self._run(store, lambda: db.latest_sync_run("rss"))
        assert row is not None
        self.assertEqual(row["id"], 3)
        self.assertEqual(row["source"], "rss")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["message"], "boom")
        self.assertEqual(row["subscriptions_count"], 3)
        self.assertEqual(row["entries_count"], 7)
        self.assertIn("started_at", row)
        self.assertIn("finished_at", row)


if __name__ == "__main__":
    unittest.main()
