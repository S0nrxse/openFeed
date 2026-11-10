"""`tools.accounts claim-catalog`: the upgrade path for an install that already had several
people when catalog scoping arrived (production had `local` plus one Authentik account), so
nobody claimed the catalog automatically and the owner's Home narrowed."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from openfeed import catalog_scope, db
from openfeed.tools import accounts as cli


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> dict:
        return {"top": 84000}

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.cur = _Cursor()
        self.committed = False

    def cursor(self) -> _Cursor:
        return self.cur

    def commit(self) -> None:
        self.committed = True


class ClaimCatalogCommandTests(unittest.TestCase):
    def test_claims_everything_stored_so_far_for_that_account(self) -> None:
        conn = _Conn()

        @contextmanager
        def fake_connect():
            yield conn

        with patch.object(cli.accounts, "get_by_username", return_value={"username": "peyton", "user_key": "acct-1"}), \
             patch.object(db, "connect", fake_connect), patch.object(catalog_scope, "clear_cache") as cleared:
            cli.cmd_claim_catalog(cli.build_parser().parse_args(["claim-catalog", "peyton"]))
        insert = [c for c in conn.cur.calls if "INSERT INTO catalog_claims" in c[0]]
        self.assertEqual(insert[0][1], ("acct-1", 84000))
        self.assertTrue(conn.committed)
        cleared.assert_called_once()

    def test_an_unknown_username_changes_nothing(self) -> None:
        with patch.object(cli.accounts, "get_by_username", return_value=None), \
             patch.object(db, "connect") as connect:
            with self.assertRaises(SystemExit):
                cli.cmd_claim_catalog(cli.build_parser().parse_args(["claim-catalog", "nobody"]))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
