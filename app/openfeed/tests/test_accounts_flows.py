"""Sign-in, two-step, sessions, setup, account and admin flows through the real app.

The routes, middleware and templates run for real; accounts.py is replaced by an in-memory
store (accounts_support.FakeStore) and Redis by the limiter's in-process fallback. Most
tests here are failure paths: wrong password, unknown user, replayed code, revoked or
expired session, disabled account, another user's id, missing Origin.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

import pyotp
from fastapi.testclient import TestClient

from openfeed import accounts, db, main, passwords, ratelimit, signin, tokens, twofactor
from openfeed.tests.accounts_support import FakeStore, fake_accounts
from openfeed.tests.support import offline_db

ORIGIN = "http://testserver"
PASSWORD = "tangerine bicycle orbit"


class _Base(unittest.TestCase):
    env = {"OPENFEED_AUTH_MODE": "accounts", "OPENFEED_SECRET_KEY": "flow-test-secret", "OPENFEED_PUBLIC_URL": "http://testserver"}

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        ratelimit.reset_client()
        self.addCleanup(ratelimit.reset_client)
        self.enterContext(patch.object(ratelimit, "_redis", side_effect=ConnectionError("no redis in tests")))
        signin._memory_setup_code.clear()
        self.store = self.enterContext(fake_accounts(FakeStore()))
        self.enterContext(offline_db())
        # Page chrome reads a few more per-user things; empty is the fresh-install answer.
        for name, value in (("list_api_tokens", []), ("find_api_token", None), ("touch_api_token", None)):
            self.enterContext(patch.object(db, name, return_value=value))
        self.enterContext(patch.object(main, "_notification_items", return_value=([], 0)))
        self.enterContext(patch.object(main, "_appearance_choice", return_value=""))
        self.enterContext(patch.object(main, "_rss_poller_status", return_value={"ok": False}))
        self.enterContext(patch.object(main, "_dearrow_body_attrs", return_value=""))
        self.client = self.new_client()

    def new_client(self) -> TestClient:
        client = TestClient(main.app)
        client.headers["origin"] = ORIGIN
        self.addCleanup(client.close)
        return client

    def make(self, username: str, role: str = "member", password: str = PASSWORD, **extra) -> dict:
        account = self.store.create_account(username, username.title(), role=role, password_hash=passwords.hash_password(password))
        self.store.accounts[account["id"]].update(extra)
        return account

    def sign_in(self, client: TestClient, username: str, password: str = PASSWORD, next: str = "/account"):
        return client.post("/auth/login", data={"username": username, "password": password, "next": next}, follow_redirects=False)

    def session_cookie(self, client: TestClient) -> str:
        values = [c.value for c in client.cookies.jar if c.name == main.get_settings().session_cookie]
        fresh = [v for v in values if v.startswith("v2.")]
        return (fresh or values or [""])[-1]


class SignInTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make("root-admin", role="admin")
        self.alice = self.make("alice")

    def test_password_sign_in_starts_a_server_session(self) -> None:
        response = self.sign_in(self.client, "alice")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/account")
        cookie_header = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=lax", cookie_header)
        self.assertNotIn("Secure", cookie_header)  # plain http here
        token = self.session_cookie(self.client)
        self.assertTrue(token.startswith("v2."))
        stored = [s["token_hash"] for s in self.store.sessions.values()]
        self.assertEqual(stored, [signin.token_hash(token)])
        self.assertNotIn(token, repr(self.store.sessions))
        me = self.client.get("/api/").json()
        self.assertEqual(me["user_key"], self.alice["user_key"])

    def test_cookie_is_secure_over_https(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_PUBLIC_URL": "https://openfeed.test"}):
            client = TestClient(main.app, base_url="https://openfeed.test")
            self.addCleanup(client.close)
            client.headers["origin"] = "https://openfeed.test"
            response = self.sign_in(client, "alice")
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_wrong_password_unknown_user_and_disabled_account_look_the_same(self) -> None:
        self.make("dora", disabled=True)
        answers = []
        for username, password in (("alice", "wrong password here"), ("nobody", PASSWORD), ("dora", PASSWORD)):
            response = self.sign_in(self.client, username, password)
            answers.append((response.status_code, "match an active account" in response.text))
            self.assertFalse(self.session_cookie(self.client))
        self.assertEqual(answers, [(401, True)] * 3)

    def test_lockout_per_username_even_with_the_right_password(self) -> None:
        for _ in range(ratelimit.SIGNIN_ACCOUNT.limit):
            self.sign_in(self.client, "alice", "wrong password here")
        locked = self.sign_in(self.client, "alice")
        self.assertEqual(locked.status_code, 429)
        self.assertFalse(self.session_cookie(self.client))
        # An unknown name locks the same way, so the lockout reveals nothing.
        for _ in range(ratelimit.SIGNIN_ACCOUNT.limit):
            self.sign_in(self.client, "ghost", "wrong password here")
        self.assertEqual(self.sign_in(self.client, "ghost", "anything at all").status_code, 429)
        # Another account from elsewhere is unaffected.
        self.assertEqual(self.sign_in(self.new_client(), "root-admin").status_code, 303)

    def test_login_form_needs_a_same_site_origin(self) -> None:
        client = self.new_client()
        client.headers.pop("origin")
        self.assertEqual(self.sign_in(client, "alice").status_code, 403)
        client.headers["origin"] = "https://evil.example"
        self.assertEqual(self.sign_in(client, "alice").status_code, 403)

    def test_rehash_on_sign_in_when_parameters_changed(self) -> None:
        from argon2 import PasswordHasher

        weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
        self.store.set_password(self.alice["id"], weak)
        self.assertEqual(self.sign_in(self.client, "alice").status_code, 303)
        upgraded = self.store.password_hash(self.alice["id"])
        self.assertNotEqual(upgraded, weak)
        self.assertFalse(passwords.needs_rehash(upgraded))

    def test_sign_out_ends_the_server_session(self) -> None:
        self.sign_in(self.client, "alice")
        token = self.session_cookie(self.client)
        self.client.get("/auth/logout", follow_redirects=False)
        self.assertEqual(self.store.sessions, {})
        replay = self.new_client()
        replay.cookies.set(main.get_settings().session_cookie, token)
        self.assertEqual(replay.get("/api/").status_code, 401)

    def test_revoked_expired_and_disabled_sessions_stop_at_once(self) -> None:
        self.sign_in(self.client, "alice")
        self.assertEqual(self.client.get("/api/").status_code, 200)
        session = next(iter(self.store.sessions.values()))
        session["idle_expires_at"] = time.time() - 1
        self.assertEqual(self.client.get("/api/").status_code, 401)
        session["idle_expires_at"] = time.time() + 600
        self.assertEqual(self.client.get("/api/").status_code, 200)
        self.store.accounts[self.alice["id"]]["disabled"] = True
        self.assertEqual(self.client.get("/api/").status_code, 401)
        self.store.accounts[self.alice["id"]]["disabled"] = False
        self.store.delete_sessions(self.alice["id"])
        self.assertEqual(self.client.get("/api/").status_code, 401)

    def test_a_planted_session_cookie_is_replaced_on_sign_in(self) -> None:
        self.sign_in(self.client, "root-admin")
        planted = self.session_cookie(self.client)
        self.sign_in(self.client, "alice")
        self.assertNotEqual(self.session_cookie(self.client), planted)
        self.assertNotIn(signin.token_hash(planted), [s["token_hash"] for s in self.store.sessions.values()])

    def test_old_stateless_cookies_mean_nothing_with_accounts_on(self) -> None:
        self.client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
            "sub": self.alice["user_key"], "groups": [], "iat": int(time.time()), "expires_at": int(time.time()) + 600,
        }))
        self.assertEqual(self.client.get("/api/").status_code, 401)

    def test_open_redirects_are_refused(self) -> None:
        response = self.sign_in(self.client, "alice", next="https://evil.example/")
        self.assertEqual(response.headers["location"], "/")


class TwoStepTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.make("root-admin", role="admin")
        self.secret = twofactor.new_secret()
        self.bob = self.make("bob", totp_secret=self.secret, totp_enabled_at=time.time())
        self.codes = twofactor.new_recovery_codes()
        self.store.replace_recovery_codes(self.bob["id"], [twofactor.hash_recovery_code(c, signin._secret()) for c in self.codes])

    def test_password_alone_is_not_a_session(self) -> None:
        response = self.sign_in(self.client, "bob")
        self.assertEqual(response.headers["location"], "/auth/2fa")
        self.assertFalse(self.session_cookie(self.client))
        self.assertEqual(self.client.get("/api/").status_code, 401)
        self.assertEqual(self.store.sessions, {})

    def test_wrong_then_right_code_then_replay_refused(self) -> None:
        self.sign_in(self.client, "bob")
        bad = self.client.post("/auth/2fa", data={"kind": "totp", "code": "000000"}, follow_redirects=False)
        self.assertEqual(bad.status_code, 401)
        code = pyotp.TOTP(self.secret).now()
        good = self.client.post("/auth/2fa", data={"kind": "totp", "code": code}, follow_redirects=False)
        self.assertEqual(good.status_code, 303)
        self.assertEqual(good.headers["location"], "/account")
        self.assertTrue(self.session_cookie(self.client))
        # The same code from another browser that also knows the password.
        other = self.new_client()
        self.sign_in(other, "bob")
        replay = other.post("/auth/2fa", data={"kind": "totp", "code": code}, follow_redirects=False)
        self.assertEqual(replay.status_code, 401)
        self.assertFalse(self.session_cookie(other))

    def test_recovery_code_works_once(self) -> None:
        self.sign_in(self.client, "bob")
        first = self.client.post("/auth/2fa", data={"kind": "recovery", "recovery_code": self.codes[3].upper()}, follow_redirects=False)
        self.assertEqual(first.status_code, 303)
        other = self.new_client()
        self.sign_in(other, "bob")
        again = other.post("/auth/2fa", data={"kind": "recovery", "recovery_code": self.codes[3]}, follow_redirects=False)
        self.assertEqual(again.status_code, 401)
        self.assertEqual(self.store.recovery_codes_left(self.bob["id"]), 9)

    def test_no_pending_cookie_no_second_step(self) -> None:
        response = self.client.post("/auth/2fa", data={"kind": "totp", "code": pyotp.TOTP(self.secret).now()}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/auth/login")
        self.assertFalse(self.session_cookie(self.client))

    def test_forged_pending_cookie_for_another_account_is_refused(self) -> None:
        carol = self.make("carol", totp_secret=twofactor.new_secret(), totp_enabled_at=time.time())
        forged = signin.sign("pending", {"aid": carol["id"], "m": "password", "exp": int(time.time()) + 60}) + "x"
        self.client.cookies.set(signin.PENDING_COOKIE, forged)
        response = self.client.post("/auth/2fa", data={"kind": "totp", "code": "123456"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/auth/login")

    def test_second_step_attempts_are_limited(self) -> None:
        self.sign_in(self.client, "bob")
        for _ in range(ratelimit.SECOND_STEP_ACCOUNT.limit):
            self.client.post("/auth/2fa", data={"kind": "totp", "code": "000000"})
        response = self.client.post("/auth/2fa", data={"kind": "totp", "code": pyotp.TOTP(self.secret).now()}, follow_redirects=False)
        self.assertEqual(response.status_code, 429)
        self.assertFalse(self.session_cookie(self.client))


class SetupTests(_Base):
    def test_no_admin_sends_every_page_to_setup(self) -> None:
        response = self.client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/setup")
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/auth/login?next=/")

    def test_setup_needs_the_code_and_creates_one_admin(self) -> None:
        page = self.client.get("/setup")
        self.assertEqual(page.status_code, 200)
        code = signin.setup_code(create=False)
        self.assertTrue(code)
        form = {"username": "owner", "password": PASSWORD, "password2": PASSWORD, "display_name": "Owner"}
        wrong = self.client.post("/setup", data={**form, "setup_code": "0000-0000-0000"}, follow_redirects=False)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(self.store.accounts, {})
        mismatch = self.client.post("/setup", data={**form, "setup_code": code, "password2": "different words here"}, follow_redirects=False)
        self.assertEqual(mismatch.status_code, 400)
        ok = self.client.post("/setup", data={**form, "setup_code": code.upper()}, follow_redirects=False)
        self.assertEqual(ok.status_code, 303)
        self.assertEqual([a["role"] for a in self.store.accounts.values()], ["admin"])
        self.assertTrue(self.session_cookie(self.client))
        # Used up: a second admin cannot be made through /setup.
        again = self.new_client().post("/setup", data={**form, "username": "intruder", "setup_code": code}, follow_redirects=False)
        self.assertEqual(again.headers["location"], "/auth/login")
        self.assertEqual(len(self.store.accounts), 1)

    def test_setup_code_guessing_is_limited(self) -> None:
        self.client.get("/setup")
        form = {"username": "owner", "password": PASSWORD, "password2": PASSWORD}
        for _ in range(ratelimit.SETUP_CLIENT.limit):
            self.client.post("/setup", data={**form, "setup_code": "1111-1111-1111"})
        response = self.client.post("/setup", data={**form, "setup_code": signin.setup_code(create=False)}, follow_redirects=False)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.store.accounts, {})

    def test_keep_data_offer_moves_local_data_once(self) -> None:
        self.client.get("/setup")
        with patch.object(self.store, "local_data_counts", return_value={"playback_progress": 3}), \
             patch.object(accounts, "local_data_counts", return_value={"playback_progress": 3}):
            ok = self.client.post("/setup", data={"username": "owner", "password": PASSWORD, "password2": PASSWORD,
                                                  "setup_code": signin.setup_code(create=False)}, follow_redirects=False)
            self.assertEqual(ok.headers["location"], "/setup/data")
            with patch.object(accounts, "adopt_local_data", wraps=self.store.adopt_local_data) as adopt:
                kept = self.client.post("/setup/data", data={"choice": "keep"}, follow_redirects=False)
            owner = next(iter(self.store.accounts.values()))
            adopt.assert_called_once_with(owner["user_key"])
            self.assertEqual(kept.headers["location"], "/?done=kept")
            # Decided: the offer is gone.
            self.assertEqual(self.client.get("/setup/data", follow_redirects=False).headers["location"], "/")

    def test_members_cannot_take_the_local_data(self) -> None:
        self.make("root-admin", role="admin")
        self.make("alice")
        self.sign_in(self.client, "alice")
        with patch.object(accounts, "adopt_local_data") as adopt:
            response = self.client.post("/setup/data", data={"choice": "keep"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/")
        adopt.assert_not_called()


class AccountPageTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.make("root-admin", role="admin")
        self.alice = self.make("alice")
        self.mallory = self.make("mallory")
        self.sign_in(self.client, "alice")

    def stale(self) -> None:
        for session in self.store.sessions.values():
            session["reauth_at"] = time.time() - 3600

    def test_account_page_renders_every_section(self) -> None:
        html = self.client.get("/account").text
        for text in ("Account &amp; security", "Display name", "Change password", "Two-step verification", "Sign-in methods",
                     "Devices", "Delete my account", "This device"):
            self.assertIn(text, html)
        self.assertNotIn("Manage users", html)

    def test_sensitive_changes_ask_for_the_password_again(self) -> None:
        self.stale()
        for path in ("/account/2fa/off", "/account/recovery-codes", "/account/delete"):
            with self.subTest(path=path):
                response = self.client.post(path, data={"confirm": "alice"}, follow_redirects=False)
                self.assertTrue(response.headers["location"].startswith("/auth/confirm"), response.headers["location"])
        self.assertIn(self.alice["id"], self.store.accounts)
        self.assertTrue(self.client.get("/account/2fa", follow_redirects=False).headers["location"].startswith("/auth/confirm"))
        wrong = self.client.post("/auth/confirm", data={"password": "not my password", "next": "/account"}, follow_redirects=False)
        self.assertEqual(wrong.status_code, 401)
        before = self.session_cookie(self.client)
        right = self.client.post("/auth/confirm", data={"password": PASSWORD, "next": "/account"}, follow_redirects=False)
        self.assertEqual(right.headers["location"], "/account")
        self.assertNotEqual(self.session_cookie(self.client), before, "confirming must rotate the session id")
        self.assertEqual(self.client.get("/account/2fa").status_code, 200)

    def test_turning_on_two_step(self) -> None:
        page = self.client.get("/account/2fa").text
        self.assertIn("<svg", page)
        self.assertNotIn("api.qrserver", page)
        secret = self.store.accounts[self.alice["id"]]["totp_pending_secret"]
        self.assertIn(twofactor.format_secret(secret), page)
        self.assertEqual(self.client.post("/account/2fa", data={"code": "000000"}).status_code, 400)
        shown = self.client.post("/account/2fa", data={"code": pyotp.TOTP(secret).now()})
        self.assertEqual(shown.status_code, 200)
        self.assertEqual(shown.headers["cache-control"], "no-store")
        self.assertEqual(shown.text.count('</code></li>'), 10)
        self.assertTrue(self.store.accounts[self.alice["id"]]["totp_enabled_at"])
        self.assertEqual(self.store.recovery_codes_left(self.alice["id"]), 10)

    def test_change_password_needs_the_current_one_and_ends_other_sessions(self) -> None:
        other = self.new_client()
        self.sign_in(other, "alice")
        self.assertEqual(len(self.store.list_sessions(self.alice["id"])), 2)
        new = "a different long passphrase"
        refused = self.client.post("/account/password", data={"current_password": "wrong one", "password": new, "password2": new})
        self.assertEqual(refused.status_code, 401)
        weak = self.client.post("/account/password", data={"current_password": PASSWORD, "password": "1234567890", "password2": "1234567890"})
        self.assertEqual(weak.status_code, 400)
        ok = self.client.post("/account/password", data={"current_password": PASSWORD, "password": new, "password2": new}, follow_redirects=False)
        self.assertEqual(ok.headers["location"], "/account?done=password")
        self.assertEqual(len(self.store.list_sessions(self.alice["id"])), 1)
        self.assertEqual(other.get("/api/").status_code, 401)
        self.assertEqual(self.client.get("/api/").status_code, 200)

    def test_cannot_touch_another_accounts_sessions_or_methods(self) -> None:
        theirs = self.new_client()
        self.sign_in(theirs, "mallory")
        their_session = next(s["id"] for s in self.store.sessions.values() if s["account_id"] == self.mallory["id"])
        their_identity = next(i["id"] for i in self.store.identities.values() if i["account_id"] == self.mallory["id"])
        self.store.add_identity(self.mallory["id"], "oidc:authentik", "mallory-sub", "Authentik")
        response = self.client.post(f"/account/sessions/{their_session}/revoke", follow_redirects=False)
        self.assertIn("error=not_found", response.headers["location"])
        self.assertEqual(theirs.get("/api/").status_code, 200)
        response = self.client.post(f"/account/methods/{their_identity}/remove", follow_redirects=False)
        self.assertIn("error=not_found", response.headers["location"])
        self.assertIn(their_identity, self.store.identities)

    def test_sign_out_everywhere_else(self) -> None:
        others = [self.new_client() for _ in range(2)]
        for client in others:
            self.sign_in(client, "alice")
        self.client.post("/account/sessions/revoke-others")
        self.assertEqual(self.client.get("/api/").status_code, 200)
        for client in others:
            self.assertEqual(client.get("/api/").status_code, 401)

    def test_last_sign_in_method_cannot_be_removed(self) -> None:
        identity = next(i["id"] for i in self.store.identities.values() if i["account_id"] == self.alice["id"])
        response = self.client.post(f"/account/methods/{identity}/remove", follow_redirects=False)
        self.assertIn("error=last_method", response.headers["location"])

    def test_writes_without_origin_are_refused(self) -> None:
        self.client.headers.pop("origin")
        self.assertEqual(self.client.post("/account/name", data={"display_name": "x"}).status_code, 403)
        self.assertEqual(self.client.post("/api/playlists", json={"name": "x"}).status_code, 403)
        self.client.headers["origin"] = "https://evil.example"
        self.assertEqual(self.client.post("/account/sessions/revoke-others").status_code, 403)

    def test_delete_my_account(self) -> None:
        wrong = self.client.post("/account/delete", data={"confirm": "mallory"}, follow_redirects=False)
        self.assertIn("error=confirm_name", wrong.headers["location"])
        self.assertIn(self.alice["id"], self.store.accounts)
        with patch.object(main, "_after_user_keys_deleted") as cleanup:
            gone = self.client.post("/account/delete", data={"confirm": "Alice"}, follow_redirects=False)
        self.assertEqual(gone.headers["location"], "/auth/login")
        self.assertNotIn(self.alice["id"], self.store.accounts)
        cleanup.assert_called_once_with([self.alice["user_key"]], [])
        self.assertEqual(self.client.get("/api/").status_code, 401)


class AdminTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make("root-admin", role="admin")
        self.alice = self.make("alice")
        self.sign_in(self.client, "root-admin")

    def test_members_cannot_reach_admin_pages(self) -> None:
        member = self.new_client()
        self.sign_in(member, "alice")
        self.assertEqual(member.get("/admin/users").status_code, 403)
        self.assertEqual(member.post(f"/admin/users/{self.admin['id']}/disable").status_code, 403)
        self.assertEqual(member.get("/admin/status").status_code, 403)
        self.assertFalse(self.store.accounts[self.admin["id"]]["disabled"])

    def test_users_page_lists_accounts(self) -> None:
        html = self.client.get("/admin/users").text
        self.assertIn("alice", html)
        self.assertIn("2FA off", html)
        self.assertIn("Registration", html)

    def test_admin_actions_ask_for_the_password_again(self) -> None:
        for session in self.store.sessions.values():
            session["reauth_at"] = time.time() - 3600
        response = self.client.post(f"/admin/users/{self.alice['id']}/disable", follow_redirects=False)
        self.assertTrue(response.headers["location"].startswith("/auth/confirm"))
        self.assertFalse(self.store.accounts[self.alice["id"]]["disabled"])

    def test_disable_ends_sessions_and_tokens_at_once(self) -> None:
        member = self.new_client()
        self.sign_in(member, "alice")
        plaintext, prefix, digest = tokens.generate()
        record = {"id": "t1", "user_key": self.alice["user_key"], "name": "ci", "token_prefix": prefix,
                  "token_hash": digest, "read_only": False, "revoked_at": None}
        script = self.new_client()
        script.headers.pop("origin")
        script.headers["authorization"] = f"Bearer {plaintext}"
        with patch.object(db, "find_api_token", return_value=record), patch.object(db, "get_feed_preferences", return_value={}):
            self.assertEqual(script.get("/api/feed/preferences").status_code, 200)
            self.client.post(f"/admin/users/{self.alice['id']}/disable")
            self.assertEqual(member.get("/api/").status_code, 401)
            self.assertEqual(script.get("/api/feed/preferences").status_code, 401)
            # A token whose owner has no account at all is refused in accounts mode.
            record["user_key"] = "u-deleted"
            self.assertEqual(script.get("/api/feed/preferences").status_code, 401)
        self.assertEqual(self.sign_in(self.new_client(), "alice").status_code, 401)

    def test_role_change_ends_the_targets_sessions(self) -> None:
        member = self.new_client()
        self.sign_in(member, "alice")
        self.client.post(f"/admin/users/{self.alice['id']}/role", data={"role": "admin"})
        self.assertEqual(self.store.accounts[self.alice["id"]]["role"], "admin")
        self.assertEqual(member.get("/api/").status_code, 401)

    def test_admin_cannot_act_on_themself_and_last_admin_stays(self) -> None:
        response = self.client.post(f"/admin/users/{self.admin['id']}/role", data={"role": "member"}, follow_redirects=False)
        self.assertIn("error=self", response.headers["location"])
        with self.assertRaises(accounts.AccountError):
            self.store.update_account(self.admin["id"], role="member")
        mine = self.client.post("/account/delete", data={"confirm": "root-admin"}, follow_redirects=False)
        self.assertIn("error=last_admin", mine.headers["location"])

    def test_unknown_or_malformed_target(self) -> None:
        response = self.client.post("/admin/users/not-a-uuid/disable", follow_redirects=False)
        self.assertIn("error=not_found", response.headers["location"])

    def test_reset_link_is_single_use_and_signs_the_account_out(self) -> None:
        member = self.new_client()
        self.sign_in(member, "alice")
        page = self.client.post(f"/admin/users/{self.alice['id']}/reset-link").text
        token = page.split("/auth/reset/")[1].split("<")[0]
        stranger = self.new_client()
        self.assertEqual(stranger.get(f"/auth/reset/{token}").status_code, 200)
        new = "a brand new passphrase"
        done = stranger.post(f"/auth/reset/{token}", data={"password": new, "password2": new}, follow_redirects=False)
        self.assertEqual(done.headers["location"], "/auth/login?done=password_set")
        self.assertEqual(member.get("/api/").status_code, 401)
        self.assertEqual(stranger.post(f"/auth/reset/{token}", data={"password": "another long one", "password2": "another long one"}).status_code, 410)
        self.assertEqual(self.sign_in(self.new_client(), "alice", new).status_code, 303)
        self.assertEqual(self.new_client().get("/auth/reset/made-up-token").status_code, 410)

    def test_create_account_gives_a_set_password_link(self) -> None:
        page = self.client.post("/admin/users/create", data={"username": "carol", "display_name": "Carol", "role": "member"}).text
        self.assertIn("/auth/reset/", page)
        carol = self.store.get_by_username("carol")
        self.assertEqual(carol["role"], "member")
        self.assertIsNone(self.store.password_hash(carol["id"]))
        taken = self.client.post("/admin/users/create", data={"username": "alice"})
        self.assertEqual(taken.status_code, 409)

    def test_invites_follow_the_registration_mode(self) -> None:
        page = self.client.post("/admin/invites", data={"role": "member", "days": "7"}).text
        token = page.split("/auth/invite/")[1].split("<")[0]
        guest = self.new_client()
        form = {"username": "dave", "password": PASSWORD, "password2": PASSWORD}
        self.assertEqual(guest.post(f"/auth/invite/{token}", data=form, follow_redirects=False).status_code, 303)
        self.assertEqual(self.store.get_by_username("dave")["role"], "member")
        # Single use.
        reuse = self.new_client().post(f"/auth/invite/{token}", data={**form, "username": "eve"})
        self.assertEqual(reuse.status_code, 400)
        self.assertIsNone(self.store.get_by_username("eve"))
        # Closed registration turns invites off.
        page = self.client.post("/admin/invites", data={"role": "member", "days": "1"}).text
        second = page.split("/auth/invite/")[1].split("<")[0]
        self.client.post("/admin/users/registration", data={"mode": "closed"})
        self.assertEqual(self.new_client().get(f"/auth/invite/{second}").status_code, 410)

    def test_open_sign_up_is_rate_limited_and_off_by_default(self) -> None:
        guest = self.new_client()
        self.assertEqual(guest.get("/auth/signup", follow_redirects=False).headers["location"], "/auth/login")
        self.client.post("/admin/users/registration", data={"mode": "open"})
        self.assertIn("Create an account", guest.get("/auth/login").text)
        for n in range(ratelimit.SIGNUP_CLIENT.limit):
            response = guest.post("/auth/signup", data={"username": f"new{n}", "password": PASSWORD, "password2": PASSWORD}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            guest.cookies.clear()
        blocked = guest.post("/auth/signup", data={"username": "one-more", "password": PASSWORD, "password2": PASSWORD})
        self.assertEqual(blocked.status_code, 429)
        self.assertIsNone(self.store.get_by_username("one-more"))

    def test_signup_honeypot(self) -> None:
        self.client.post("/admin/users/registration", data={"mode": "open"})
        bot = self.new_client()
        bot.post("/auth/signup", data={"username": "botty", "password": PASSWORD, "password2": PASSWORD, "website": "spam"})
        self.assertIsNone(self.store.get_by_username("botty"))

    def test_delete_user_with_all_data(self) -> None:
        with patch.object(main, "_after_user_keys_deleted") as cleanup:
            wrong = self.client.post(f"/admin/users/{self.alice['id']}/delete", data={"confirm": "bob"}, follow_redirects=False)
            self.assertIn("error=confirm_name", wrong.headers["location"])
            done = self.client.post(f"/admin/users/{self.alice['id']}/delete", data={"confirm": "alice"}, follow_redirects=False)
        self.assertIn("done=deleted", done.headers["location"])
        self.assertNotIn(self.alice["id"], self.store.accounts)
        cleanup.assert_called_once()

    def test_admin_reset_of_two_step(self) -> None:
        self.store.accounts[self.alice["id"]].update(totp_secret="X", totp_enabled_at=time.time())
        self.client.post(f"/admin/users/{self.alice['id']}/reset-2fa")
        self.assertIsNone(self.store.accounts[self.alice["id"]]["totp_enabled_at"])

    def test_tokens_cannot_drive_admin_forms(self) -> None:
        plaintext, prefix, digest = tokens.generate()
        record = {"id": "t2", "user_key": self.admin["user_key"], "name": "ci", "token_prefix": prefix, "token_hash": digest,
                  "read_only": False, "revoked_at": None, "delegated_admin": True}
        script = self.new_client()
        script.headers["authorization"] = f"Bearer {plaintext}"
        with patch.object(db, "find_api_token", return_value=record):
            response = script.post(f"/admin/users/{self.alice['id']}/disable", follow_redirects=False)
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(self.store.accounts[self.alice["id"]]["disabled"])


class AdminRoleTests(unittest.TestCase):
    def test_role_and_idp_groups(self) -> None:
        now = int(time.time())
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "accounts", "OPENFEED_ADMIN_GROUPS": "ops"}, clear=True):
            self.assertTrue(main._user_is_admin({"session_backed": True, "role": "admin"}))
            self.assertFalse(main._user_is_admin({"session_backed": True, "role": "member"}))
            # IdP group grants admin to an OIDC session, within the admin age limit.
            self.assertTrue(main._user_is_admin({"session_backed": True, "role": "member", "idp": True, "groups": ["ops"], "iat": now}))
            self.assertFalse(main._user_is_admin({"session_backed": True, "role": "member", "idp": True, "groups": ["ops"], "iat": now - 2 * 86400}))
            # A password session cannot claim groups.
            self.assertFalse(main._user_is_admin({"session_backed": True, "role": "member", "idp": False, "groups": ["ops"], "iat": now}))
            # Anonymous in accounts mode is never admin.
            self.assertFalse(main._user_is_admin({"authenticated": False}))

    def test_authentik_installs_keep_their_rules(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "authentik_oidc"}, clear=True):
            # No admin groups configured: everyone signed in administers, as before.
            self.assertTrue(main._user_is_admin({"session_backed": True, "role": "member", "idp": True, "groups": []}))
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "authentik_oidc", "OPENFEED_REQUIRE_LOGIN": "true",
                                     "OPENFEED_ADMIN_GROUPS": "ops"}, clear=True):
            self.assertFalse(main._user_is_admin({"session_backed": True, "role": "member", "idp": True, "groups": ["users"], "iat": int(time.time())}))

    def test_required_group_applies_to_idp_sessions_only(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "accounts", "OPENFEED_REQUIRED_GROUP": "family"}, clear=True):
            self.assertTrue(main._user_has_required_access({"authenticated": True, "session_backed": True, "idp": False}))
            self.assertFalse(main._user_has_required_access({"authenticated": True, "session_backed": True, "idp": True, "groups": []}))


class DisabledModeTests(unittest.TestCase):
    """Sign-in off: nothing about accounts is consulted and the local user is everyone."""

    def test_disabled_mode_is_untouched(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(db, "connect", side_effect=AssertionError("db")), \
             patch.object(accounts, "session_by_hash") as lookup:
            client = TestClient(main.app)
            try:
                # Nothing to sign in to: the sign-in steps go where the person was headed.
                self.assertEqual(client.get("/auth/login", follow_redirects=False).headers["location"], "/")
                self.assertEqual(client.get("/setup", follow_redirects=False).headers["location"], "/")
                self.assertEqual(client.get("/account", follow_redirects=False).headers["location"], "/?settings=account")
                self.assertEqual(main._current_user(None)["user_key"], "local")
                client.cookies.set(main.get_settings().session_cookie, "v2.anything")
            finally:
                client.close()
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AuthentikModeTests(_Base):
    """An existing Authentik deployment: same config, same user_keys, now server sessions."""

    env = {
        "OPENFEED_AUTH_MODE": "authentik_oidc", "OPENFEED_SECRET_KEY": "flow-test-secret", "OPENFEED_PUBLIC_URL": "http://testserver",
        "OPENFEED_OIDC_ISSUER": "https://auth.example/application/o/openfeed", "OPENFEED_OIDC_CLIENT_ID": "id",
        "OPENFEED_OIDC_CLIENT_SECRET": "secret", "OPENFEED_ADMIN_GROUPS": "ops",
    }

    def setUp(self) -> None:
        super().setUp()
        from openfeed import authstate
        from openfeed.tests.oidc_support import FakeIdP

        authstate.reset()
        # A provider that signs real ID tokens (oidc.py verifies them) and answers userinfo.
        self.idp = FakeIdP().install(self)
        self.userinfo = self.idp.claims
        self.enterContext(patch.object(db, "upsert_user", return_value={}))
        self.codes = 0

    def oidc_sign_in(self, client: TestClient, next: str = "/account"):
        start = client.get(f"/auth/login?next={next}", follow_redirects=False)
        self.assertTrue(start.headers["location"].startswith("https://auth.example/authorize?"), "still redirects straight to the IdP")
        state, _nonce = self.idp.authorize(start.headers["location"])
        self.codes += 1
        return client.get(f"/auth/callback?code=c{self.codes}&state={state}", follow_redirects=False)

    def test_sign_in_keeps_the_sub_as_user_key_and_makes_a_server_session(self) -> None:
        done = self.oidc_sign_in(self.client)
        self.assertEqual(done.status_code, 303)
        self.assertEqual(done.headers["location"], "/account")
        self.assertTrue(self.session_cookie(self.client).startswith("v2."))
        account = self.store.get_by_user_key("ak-sub-123")
        self.assertEqual(account["username"], "alice")
        self.assertIsNotNone(self.store.find_identity("oidc:authentik", "ak-sub-123"))
        self.assertEqual(self.client.get("/api/").json()["user_key"], "ak-sub-123")
        # Second sign-in reuses the account.
        self.oidc_sign_in(self.new_client())
        self.assertEqual(len(self.store.accounts), 1)

    def test_idp_admin_group_and_disabled_account(self) -> None:
        self.userinfo["groups"] = ["ops"]
        self.oidc_sign_in(self.client)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        account = self.store.get_by_user_key("ak-sub-123")
        self.store.accounts[account["id"]]["disabled"] = True
        refused = self.oidc_sign_in(self.new_client())
        self.assertEqual(refused.status_code, 403)

    def test_a_forged_state_is_refused(self) -> None:
        self.client.get("/auth/login", follow_redirects=False)
        response = self.client.get("/auth/callback?code=c&state=not-the-state", follow_redirects=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.accounts, {})

    def test_an_old_signed_cookie_is_exchanged_once(self) -> None:
        legacy = main._sign_payload({"sub": "ak-sub-123", "name": "Alice", "username": "alice", "groups": [],
                                     "iat": int(time.time()) - 60, "expires_at": int(time.time()) + 600})
        browser = self.new_client()
        browser.cookies.set(main.get_settings().session_cookie, legacy)
        first = browser.get("/api/")
        self.assertTrue(first.json()["authenticated"])
        self.assertEqual(first.json()["user_key"], "ak-sub-123")
        self.assertTrue(self.session_cookie(browser).startswith("v2."), "upgraded to a server session")
        self.assertEqual(len(self.store.sessions), 1)
        # The same old cookie a second time (a copy) gets nothing.
        thief = self.new_client()
        thief.cookies.set(main.get_settings().session_cookie, legacy)
        self.assertFalse(thief.get("/api/").json()["authenticated"])
        self.assertEqual(len(self.store.sessions), 1)

    def test_old_cookie_of_a_disabled_account_is_not_exchanged(self) -> None:
        account = self.store.create_account("alice", user_key="ak-sub-123", identity=("oidc:authentik", "ak-sub-123", "Authentik"))
        self.store.accounts[account["id"]]["disabled"] = True
        browser = self.new_client()
        browser.cookies.set(main.get_settings().session_cookie, main._sign_payload({
            "sub": "ak-sub-123", "iat": int(time.time()), "expires_at": int(time.time()) + 600}))
        self.assertFalse(browser.get("/api/").json()["authenticated"])
        self.assertEqual(self.store.sessions, {})

    def test_password_form_is_opt_in(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_PASSWORD_SIGNIN": "true"}):
            page = self.client.get("/auth/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="password"', page.text)
        self.assertIn("Sign in with Authentik", page.text)
        self.assertIn('data-signin-method="oidc:authentik"', page.text)
