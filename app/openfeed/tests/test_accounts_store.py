"""The SQL in accounts.py: the rules the database itself has to enforce.

accounts_support.FakeStore mirrors these rules for the flow tests; this file checks the
statements that actually run, with a cursor that records them (no database needed).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from openfeed import accounts, db, profiles


class _Cursor:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 1) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self._last: list[Any] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((" ".join(query.split()), params))
        self._last = [self.rows.pop(0)] if self.rows else []

    def fetchone(self) -> Any:
        return self._last[0] if self._last else None

    def fetchall(self) -> list[Any]:
        value = self._last[0] if self._last else []
        return value if isinstance(value, list) else [value]


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self.cur = cursor
        self.committed = False

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cur

    def commit(self) -> None:
        self.committed = True

    def transaction(self) -> "_Conn":
        return self


def run(callback: Any, rows: list[Any] | None = None, rowcount: int = 1) -> tuple[Any, list[tuple[str, Any]], _Conn]:
    cursor = _Cursor(rows, rowcount)
    conn = _Conn(cursor)
    with patch.object(db, "connect", return_value=conn):
        result = callback()
    return result, cursor.calls, conn


class SessionSqlTests(unittest.TestCase):
    def test_lookup_refuses_expired_idle_and_disabled(self) -> None:
        _, calls, _ = run(lambda: accounts.session_by_hash("h"))
        query, params = calls[-1]
        self.assertIn("s.token_hash = %s", query)
        self.assertIn("s.expires_at > now()", query)
        self.assertIn("s.idle_expires_at > now()", query)
        self.assertIn("NOT a.disabled", query)
        self.assertEqual(params, ("h",))

    def test_create_stores_only_the_hash_and_no_address(self) -> None:
        _, calls, conn = run(lambda: accounts.create_session("acct", "hash", device="Firefox on Android", methods=["password"],
                                                              idp_groups=[], idle_seconds=60, max_seconds=600), rows=[None, {"id": "s"}])
        insert = next(q for q, _ in calls if q.startswith("INSERT INTO auth_sessions"))
        self.assertNotRegex(insert, r"\bip\b|address")
        self.assertTrue(conn.committed)
        schema = Path(accounts.__file__).with_name("accounts.sql").read_text()
        sessions_table = schema.split("CREATE TABLE IF NOT EXISTS auth_sessions")[1].split(");")[0]
        self.assertNotRegex(sessions_table.lower(), r"\bip\b|_ip\b|address|email")

    def test_touch_is_throttled_and_bounded_by_the_absolute_expiry(self) -> None:
        _, calls, _ = run(lambda: accounts.touch_session("s", "a", 600), rowcount=0)
        query = calls[0][0]
        self.assertIn("LEAST(expires_at", query)
        self.assertIn("last_seen_at < now() - interval '60 seconds'", query)

    def test_sign_out_everywhere_else_keeps_only_this_session(self) -> None:
        _, calls, _ = run(lambda: accounts.delete_sessions("acct", except_session_id="mine"))
        query, params = calls[0]
        self.assertIn("account_id = %s", query)
        self.assertIn("id <> %s::uuid", query)
        self.assertEqual(params, ("acct", "mine", "mine"))

    def test_deleting_one_session_is_scoped_to_its_account(self) -> None:
        _, calls, _ = run(lambda: accounts.delete_session("acct", "sid"))
        self.assertIn("WHERE id = %s AND account_id = %s", calls[0][0])


class TwoStepSqlTests(unittest.TestCase):
    def test_a_step_is_claimed_only_if_later_than_the_last(self) -> None:
        claimed, calls, _ = run(lambda: accounts.claim_totp_step("acct", 100), rowcount=1)
        self.assertTrue(claimed)
        self.assertIn("totp_last_step IS NULL OR totp_last_step < %s", calls[0][0])
        replayed, _, _ = run(lambda: accounts.claim_totp_step("acct", 100), rowcount=0)
        self.assertFalse(replayed)

    def test_recovery_codes_are_single_use(self) -> None:
        used, calls, _ = run(lambda: accounts.use_recovery_code("acct", "h"), rowcount=1)
        self.assertTrue(used)
        self.assertIn("used_at IS NULL", calls[0][0])
        again, _, _ = run(lambda: accounts.use_recovery_code("acct", "h"), rowcount=0)
        self.assertFalse(again)

    def test_enabling_requires_the_pending_secret(self) -> None:
        ok, calls, _ = run(lambda: accounts.enable_totp("acct", "SECRET", 5, ["a", "b"]), rowcount=1)
        self.assertTrue(ok)
        self.assertIn("totp_pending_secret = %s", calls[0][0])
        self.assertEqual(sum(1 for q, _ in calls if q.startswith("INSERT INTO account_recovery_codes")), 2)
        refused, calls, _ = run(lambda: accounts.enable_totp("acct", "OTHER", 5, ["a"]), rowcount=0)
        self.assertFalse(refused)
        self.assertFalse(any("account_recovery_codes" in q for q, _ in calls))


class LinkSqlTests(unittest.TestCase):
    def test_links_are_single_use_and_expire(self) -> None:
        _, calls, _ = run(lambda: accounts.find_link("invite", "h"))
        self.assertIn("used_at IS NULL AND l.expires_at > now()", calls[0][0])

    def test_redeeming_an_invite_consumes_it_in_the_same_transaction(self) -> None:
        rows = [{"id": "l", "kind": "invite", "account_id": None, "role": "member"},
                {"id": "a", "user_key": "u-x", "username": "dave", "display_name": "", "role": "member", "disabled": False,
                 "totp_enabled_at": None, "created_at": None, "updated_at": None, "last_active_at": None}]
        _, calls, conn = run(lambda: accounts.redeem_invite("h", "dave", "", "hash"), rows=rows)
        self.assertTrue(calls[0][0].startswith("UPDATE account_links SET used_at = now()"))
        self.assertIn("used_at IS NULL AND expires_at > now()", calls[0][0])
        self.assertTrue(any(q.startswith("INSERT INTO accounts") for q, _ in calls))
        self.assertTrue(conn.committed)

    def test_a_used_invite_creates_nothing(self) -> None:
        with self.assertRaises(accounts.AccountError):
            _, calls, _ = run(lambda: accounts.redeem_invite("h", "dave", "", "hash"), rows=[None])
        cursor = _Cursor([None])
        with patch.object(db, "connect", return_value=_Conn(cursor)), self.assertRaises(accounts.AccountError):
            accounts.redeem_invite("h", "dave", "", "hash")
        self.assertFalse(any(q.startswith("INSERT INTO accounts") for q, _ in cursor.calls))

    def test_reset_ends_every_session(self) -> None:
        rows = [{"id": "l", "kind": "reset", "account_id": "acct", "role": "member"}, None, None, {"id": "acct"}]
        _, calls, _ = run(lambda: accounts.redeem_reset("h", "hash"), rows=rows)
        self.assertTrue(any(q == "DELETE FROM auth_sessions WHERE account_id = %s" for q, _ in calls))


class AccountSqlTests(unittest.TestCase):
    def test_disabling_ends_sessions(self) -> None:
        rows = [None, {"n": 1}, {"role": "member", "disabled": False}, {"id": "acct"}, None,
                {"id": "acct", "user_key": "u", "username": "a", "display_name": "", "role": "member", "disabled": True}]
        _, calls, _ = run(lambda: accounts.update_account("acct", disabled=True), rows=rows)
        self.assertIn(("DELETE FROM auth_sessions WHERE account_id = %s", ("acct",)), calls)

    def test_the_last_admin_cannot_be_demoted(self) -> None:
        rows = [None, {"n": 0}, {"role": "admin", "disabled": False}]
        with self.assertRaises(accounts.AccountError) as raised:
            run(lambda: accounts.update_account("acct", role="member"), rows=rows)
        self.assertEqual(raised.exception.code, "last_admin")

    def test_first_admin_setup_is_locked_and_refused_once_an_admin_exists(self) -> None:
        cursor = _Cursor([None, {"?column?": 1}])
        with patch.object(db, "connect", return_value=_Conn(cursor)), self.assertRaises(accounts.AccountError) as raised:
            accounts.create_account("owner", role="admin", first_admin=True)
        self.assertEqual(raised.exception.code, "setup_done")
        self.assertIn("pg_advisory_xact_lock", cursor.calls[0][0])
        self.assertFalse(any(q.startswith("INSERT INTO accounts") for q, _ in cursor.calls))

    def test_delete_account_removes_every_user_key_it_owns(self) -> None:
        rows = [None, {"id": "acct", "user_key": "u-1", "username": "a", "role": "member", "disabled": False},
                [{"user_key": "u-1"}, {"user_key": "u-1:p:aa"}]]
        result, calls, conn = run(lambda: accounts.delete_account("acct"), rows=rows)
        self.assertEqual(result["user_keys"], ["u-1", "u-1:p:aa"])
        for key in ("u-1", "u-1:p:aa"):
            for table in profiles.USER_KEY_TABLES:
                self.assertIn((f"DELETE FROM {table} WHERE user_key = %s", (key,)), calls, (table, key))
            self.assertIn(("DELETE FROM users WHERE authentik_sub = %s", (key,)), calls)
        self.assertIn(("DELETE FROM account_profiles WHERE account_key = %s", ("u-1",)), calls)
        self.assertIn(("DELETE FROM accounts WHERE id = %s", ("acct",)), calls)
        self.assertTrue(conn.committed)

    def test_token_owner_state_resolves_profiles_to_their_account(self) -> None:
        state, calls, _ = run(lambda: accounts.owner_state("u-1:p:aa"), rows=[{"disabled": True}])
        self.assertEqual(state, "disabled")
        self.assertIn("account_profiles", calls[0][0])
        self.assertEqual(run(lambda: accounts.owner_state("x"), rows=[None])[0], "unknown")
        self.assertEqual(run(lambda: accounts.owner_state("x"), rows=[{"disabled": False}])[0], "active")

    def test_every_statement_is_parameterised(self) -> None:
        hostile = "x'; DROP TABLE accounts; --"
        for call in (lambda: accounts.get_by_username(hostile), lambda: accounts.get_by_user_key(hostile),
                     lambda: accounts.find_identity("password", hostile), lambda: accounts.session_by_hash(hostile),
                     lambda: accounts.find_link("invite", hostile), lambda: accounts.get_setting(hostile)):
            _, calls, _ = run(call)
            for query, _params in calls:
                self.assertNotIn("DROP TABLE", query)


class AdoptionSqlTests(unittest.TestCase):
    def test_tokens_from_the_open_server_are_not_adopted(self) -> None:
        self.assertIn("api_tokens", accounts._ADOPT_SKIP)

    def test_adoption_refuses_a_local_target(self) -> None:
        for target in ("", "local", "local:p:aa"):
            with self.assertRaises(accounts.AccountError):
                accounts.adopt_local_data(target)

    def test_adoption_moves_every_user_key_table_and_records_the_decision(self) -> None:
        cursor = _Cursor([None, []], rowcount=2)
        conn = _Conn(cursor)
        with patch.object(db, "connect", return_value=conn):
            counts = accounts.adopt_local_data("u-admin")
        queries = [q for q, _ in cursor.calls]
        for table in profiles.USER_KEY_TABLES:
            moved = f"UPDATE {table} SET user_key = %s WHERE user_key = %s"
            if table in accounts._ADOPT_SKIP:
                self.assertNotIn(moved, queries)
            else:
                self.assertIn((moved, ("u-admin", "local")), cursor.calls)
                self.assertEqual(counts[table], 2)
        self.assertTrue(any("jsonb_set(metadata, '{user_key}'" in q for q in queries))
        self.assertTrue(any("'local_data_adopted'" in q for q in queries))
        self.assertTrue(conn.committed)


class SchemaTests(unittest.TestCase):
    def test_no_plaintext_secret_columns(self) -> None:
        schema = Path(accounts.__file__).with_name("accounts.sql").read_text().lower()
        for forbidden in ("password text", "email", "ip_address", "raw_ip", "remote_addr"):
            self.assertNotIn(forbidden, schema)
        self.assertIn("token_hash text not null unique", schema)
        self.assertRegex(schema, re.compile(r"code_hash text not null"))

    def test_init_db_applies_the_accounts_schema(self) -> None:
        cursor = _Cursor()
        with patch.object(db, "connect", return_value=_Conn(cursor)):
            db.init_db()
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS auth_sessions" in q for q, _ in cursor.calls))


if __name__ == "__main__":
    unittest.main()
