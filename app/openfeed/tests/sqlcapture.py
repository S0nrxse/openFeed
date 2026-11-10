"""Shared helpers for DB-free tests: capture the SQL a db.py function would run.

`_capture_sql(callback)` runs `callback` with `db.connect` replaced by a fake connection
whose cursor records every `execute` and returns no rows, then gives back the last
(query, params) pair; `_capture_all_sql` gives back all of them. Nothing touches a real
database. The underscored names are what the existing tests import; `capture_sql` and
`capture_all_sql` are the same functions under public names.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Make `from openfeed import db` work however the suite is launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# psycopg is a runtime dependency that may not be installed where tests run;
# db.py only needs it inside connect(), which these helpers stub out.
if "psycopg" not in sys.modules:
    _psycopg = types.ModuleType("psycopg")
    _rows = types.ModuleType("psycopg.rows")
    _rows.dict_row = object  # type: ignore[attr-defined]
    _psycopg.rows = _rows  # type: ignore[attr-defined]
    sys.modules["psycopg"] = _psycopg
    sys.modules["psycopg.rows"] = _rows

from openfeed import db


class _EmptyCursor:
    """Captures SQL only; returns no rows (safe for post-processing helpers)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> "_EmptyCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        self._rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def cursor(self) -> Any:
        return self._cursor

    def execute(self, query: str, params: Any = None) -> Any:
        self._cursor.execute(query, params)
        return self._cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _capture_sql(callback: Any) -> tuple[str, Any]:
    cursor = _EmptyCursor()
    with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
        callback()
    return cursor.calls[-1]


def _capture_all_sql(callback: Any) -> list[tuple[str, Any]]:
    cursor = _EmptyCursor()
    with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
        callback()
    return list(cursor.calls)


def placeholder_count(query: str) -> int:
    """Positional %s placeholders in a query, ignoring the %% escape."""
    return query.replace("%%", "").count("%s")


capture_sql = _capture_sql
capture_all_sql = _capture_all_sql
