from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import db


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params=None) -> None:
        self.calls.append((query, params))

    def fetchall(self) -> list[dict[str, object]]:
        return []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


class DbQueryParameterTests(unittest.TestCase):
    def _last_execute_params(self, callback):
        cursor = _FakeCursor()
        with patch.object(db, "connect", return_value=_FakeConnection(cursor)):
            callback()
        return cursor.calls[-1][1]

    def test_recent_retrieval_attempts_passes_limit_only(self) -> None:
        params = self._last_execute_params(lambda: db.list_recent_retrieval_attempts(12))
        self.assertEqual(params, (12,))

    def test_history_videos_passes_user_key_and_limit(self) -> None:
        params = self._last_execute_params(lambda: db.list_history_videos(25, "user-1"))
        self.assertEqual(params, ("user-1", 25))


if __name__ == "__main__":
    unittest.main()
