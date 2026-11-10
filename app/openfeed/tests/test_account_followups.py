"""Follow-ups from the accounts QA round: pages drawn as the answer to a POST, the recovery
code list, refused password changes, the 403 page, sign-in steps with sign-in off, the
notices after "Keep it in my account" and a reset link, the Devices list, the Privacy
pane's "Off on this server" and the cross-origin headers.

Same harness as test_accounts_flows: the real app with an in-memory accounts store.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import unittest
from datetime import datetime, timezone
from html import escape
from types import SimpleNamespace
from unittest.mock import patch

import pyotp
from fastapi.testclient import TestClient

from openfeed import account_pages, accounts, db, main, privacy, ratelimit, signin, twofactor
from openfeed.tests.test_accounts_flows import PASSWORD, _Base

REPLACE = re.compile(r'<span hidden data-replace-url="([^"]*)"></span><script src="/assets/auth/replace-([0-9a-f]{16})\.js"></script>')


def replaced(html: str) -> str:
    """The GET address a POST answer moves the address bar to, or ""."""
    found = REPLACE.search(html)
    return found.group(1).replace("&amp;", "&") if found else ""


class ReplaceUrlTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make("root-admin", role="admin")
        self.alice = self.make("alice")

    def test_the_script_is_served_by_digest_and_does_only_local_paths(self) -> None:
        tag = account_pages.replace_url_tag("/account?done=codes")
        digest = REPLACE.search(tag).group(2)
        script = self.client.get(f"/assets/auth/replace-{digest}.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("history.replaceState", script.text)
        self.assertIn("immutable", script.headers["cache-control"])
        self.assertEqual(self.client.get("/assets/auth/replace-0000000000000000.js").status_code, 404)
        for bad in ("", "https://evil.example/", "//evil.example/x", "javascript:alert(1)", "account"):
            with self.subTest(url=bad):
                self.assertEqual(account_pages.replace_url_tag(bad), "")
        # A GET of the same page never carries one.
        self.assertEqual(replaced(self.client.get("/auth/login").text), "")

    def test_refused_sign_in_goes_back_to_the_sign_in_address(self) -> None:
        refused = self.sign_in(self.client, "alice", "wrong password", next="/history")
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(replaced(refused.text), "/auth/login?next=/history")
        self.assertNotIn("wrong password", replaced(refused.text))
        plain = self.client.post("/auth/login", data={"username": "alice", "password": "nope", "next": "/"})
        self.assertEqual(replaced(plain.text), "/auth/login")

    def test_refused_second_step_and_confirm(self) -> None:
        bob = self.make("bob", totp_secret=twofactor.new_secret(), totp_enabled_at=time.time())
        self.assertTrue(bob)
        self.sign_in(self.client, "bob")
        bad = self.client.post("/auth/2fa", data={"kind": "totp", "code": "000000"})
        self.assertEqual((bad.status_code, replaced(bad.text)), (401, "/auth/2fa"))
        bad = self.client.post("/auth/2fa", data={"kind": "recovery", "recovery_code": "aaaa-bbbb-cc"})
        self.assertEqual(replaced(bad.text), "/auth/2fa?recovery=1")
        member = self.new_client()
        self.sign_in(member, "alice")
        wrong = member.post("/auth/confirm", data={"password": "not it", "next": "/account"})
        self.assertEqual((wrong.status_code, replaced(wrong.text)), (401, "/auth/confirm?next=/account"))

    def test_recovery_codes_are_shown_once_and_reload_shows_the_account(self) -> None:
        self.sign_in(self.client, "alice")
        self.client.get("/account/2fa")
        secret = self.store.accounts[self.alice["id"]]["totp_pending_secret"]
        shown = self.client.post("/account/2fa", data={"code": pyotp.TOTP(secret).now()})
        self.assertEqual(shown.status_code, 200)
        self.assertEqual(replaced(shown.text), "/account?done=2fa_on")
        codes = re.findall(r"<code>([a-z0-9-]+)</code></li>", shown.text)
        self.assertEqual(len(codes), twofactor.RECOVERY_CODE_COUNT)
        for code in codes:
            self.assertNotIn(code, replaced(shown.text))
        # What Reload (or Back) now loads: the account page, a notice, no codes.
        later = self.client.get("/account?done=2fa_on").text
        self.assertIn("Two-step verification is on.", later)
        self.assertFalse(any(code in later for code in codes))
        again = self.client.post("/account/recovery-codes")
        self.assertEqual(replaced(again.text), "/account?done=codes")
        self.assertIn("New recovery codes made.", self.client.get("/account?done=codes").text)

    def test_a_wrong_setup_code_keeps_the_key_on_reload(self) -> None:
        self.sign_in(self.client, "alice")
        first = self.client.get("/account/2fa")
        secret = self.store.accounts[self.alice["id"]]["totp_pending_secret"]
        self.assertIn(twofactor.format_secret(secret), first.text)
        wrong = self.client.post("/account/2fa", data={"code": "000000"})
        self.assertEqual((wrong.status_code, replaced(wrong.text)), (400, "/account/2fa"))
        reload = self.client.get("/account/2fa")
        self.assertIn(twofactor.format_secret(secret), reload.text, "the key they scanned still works")
        self.assertEqual(self.store.accounts[self.alice["id"]]["totp_pending_secret"], secret)

    def test_admin_one_time_links_never_reach_the_address_bar(self) -> None:
        self.sign_in(self.client, "root-admin")
        made = self.client.post(f"/admin/users/{self.alice['id']}/reset-link")
        link = re.search(r"<code data-copy-text>([^<]+)</code>", made.text).group(1)
        self.assertIn("/auth/reset/", link)
        self.assertEqual(replaced(made.text), "/admin/users?done=reset_link")
        token = link.rsplit("/", 1)[1]
        self.assertNotIn(token, replaced(made.text))
        self.assertIn("It was shown once", self.client.get("/admin/users?done=reset_link").text)
        self.assertNotIn(token, self.client.get("/admin/users?done=reset_link").text)
        self.client.post("/admin/users/registration", data={"mode": "invite"})
        invite = self.client.post("/admin/invites", data={"role": "member", "days": "7"})
        self.assertEqual(replaced(invite.text), "/admin/users?done=invite")
        created = self.client.post("/admin/users/create", data={"username": "newbie", "role": "member"})
        self.assertEqual(replaced(created.text), "/admin/users?done=created")
        taken = self.client.post("/admin/users/create", data={"username": "alice", "role": "member"})
        self.assertEqual(replaced(taken.text), "/admin/users")

    def test_admin_server_error_goes_back_to_the_page(self) -> None:
        self.sign_in(self.client, "root-admin")
        stale = self.client.post("/admin/server", data={"section": "nonsense"})
        self.assertEqual((stale.status_code, replaced(stale.text)), (400, "/admin/server"))

    def test_signed_out_forms(self) -> None:
        with patch.object(accounts, "registration_mode", return_value="open"):
            short = self.client.post("/auth/signup", data={"username": "newbie", "password": "x", "password2": "x"})
        self.assertEqual((short.status_code, replaced(short.text)), (400, "/auth/signup"))
        token = "a-reset-token"
        self.store.create_link("reset", signin.token_hash(token), 3600, created_by=self.admin["id"], account_id=self.alice["id"])
        mismatch = self.client.post(f"/auth/reset/{token}", data={"password": "one long passphrase", "password2": "another"})
        self.assertEqual((mismatch.status_code, replaced(mismatch.text)), (400, f"/auth/reset/{token}"))


class RecoveryCodeLayoutTests(_Base):
    def test_two_aligned_columns_of_monospace_codes(self) -> None:
        self.make("root-admin", role="admin")
        alice = self.make("alice")
        self.sign_in(self.client, "alice")
        self.client.get("/account/2fa")
        secret = self.store.accounts[alice["id"]]["totp_pending_secret"]
        html = self.client.post("/account/2fa", data={"code": pyotp.TOTP(secret).now()}).text
        self.assertIn('<ol class="acct-codes" role="list" data-recovery-codes style="--code-rows: 5">', html)
        numbers = re.findall(r'<span class="acct-code-num" aria-hidden="true">(\d+)\.</span><code>', html)
        self.assertEqual(numbers, [str(n) for n in range(1, 11)])
        css = (main._ASSET_DIR / "css" / "38-accounts.css").read_text()
        rule = re.search(r"\.acct-codes \{[^}]*\}", css).group(0)
        for part in ("grid-auto-flow: column", "grid-template-rows: repeat(var(--code-rows, 5), auto)",
                     "grid-template-columns: repeat(2, max-content)", "list-style: none"):
            self.assertIn(part, rule)
        self.assertIn("monospace", re.search(r"\.acct-codes code \{[^}]*\}", css).group(0))
        self.assertIn("text-align: right", re.search(r"\.acct-code-num \{[^}]*\}", css).group(0))


class PasswordChangeTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.make("root-admin", role="admin")
        self.alice = self.make("alice")
        self.sign_in(self.client, "alice")

    def test_wrong_current_password_shows_the_account_page_with_the_error_at_the_field(self) -> None:
        new = "a different long passphrase"
        refused = self.client.post("/account/password", data={"current_password": "wrong one", "password": new, "password2": new})
        self.assertEqual(refused.status_code, 401)
        html = refused.text
        self.assertIn("Change password", html)
        self.assertIn("Two-step verification", html, "the whole account page, not a dead end")
        self.assertNotIn("Back to Account", html)
        field = re.search(r'<label class="text-field acct-field has-error" for="f-current_password">.*?</label>', html, re.S).group(0)
        self.assertIn('aria-invalid="true"', field)
        self.assertIn('aria-describedby="f-current_password-error"', field)
        self.assertIn("That isn&#x27;t your current password. Nothing was changed.", field)
        self.assertEqual(replaced(html), "/account")
        self.assertTrue(passwords_ok(self.store, self.alice["id"]))

    def test_weak_new_password_is_shown_at_the_new_password_field(self) -> None:
        refused = self.client.post("/account/password", data={"current_password": PASSWORD, "password": "1234567890", "password2": "1234567890"})
        self.assertEqual(refused.status_code, 400)
        field = re.search(r'<label class="text-field acct-field has-error" for="f-password">.*?</label>', refused.text, re.S).group(0)
        self.assertIn("Nothing was changed.", field)
        self.assertNotIn("has-error\" for=\"f-current_password\"", refused.text)

    def test_too_many_wrong_passwords(self) -> None:
        for _ in range(ratelimit.REAUTH_ACCOUNT.limit):
            self.client.post("/account/password", data={"current_password": "wrong", "password": "x", "password2": "x"})
        locked = self.client.post("/account/password", data={"current_password": PASSWORD, "password": "x", "password2": "x"})
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Too many wrong passwords", locked.text)
        self.assertIn('aria-invalid="true"', locked.text)


def passwords_ok(store, account_id: str) -> bool:
    from openfeed import passwords

    return passwords.verify(store.password_hash(account_id), PASSWORD)


class AccessDeniedTests(_Base):
    def test_members_hear_about_admins_of_this_openfeed(self) -> None:
        self.make("root-admin", role="admin")
        self.make("alice")
        self.sign_in(self.client, "alice")
        for path in ("/admin/users", "/admin/server", "/admin/status"):
            with self.subTest(path=path):
                page = self.client.get(path)
                self.assertEqual(page.status_code, 403)
                self.assertIn("This page is for admins of this OpenFeed.", page.text)
                self.assertNotIn("identity-provider", page.text)
                self.assertNotIn("identity provider", page.text)
                self.assertNotIn("admin group", page.text)

    def test_idp_group_failures_keep_the_idp_wording(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_ADMIN_GROUPS": "ops", "OPENFEED_REQUIRED_GROUP": "openfeed"}):
            # An OIDC session without the admin group.
            idp_session = {"session_backed": True, "idp": True, "authenticated": True, "methods": ["oidc:authentik"], "groups": ["users"]}
            page = main._access_denied_response(idp_session, admin_only=True)
            self.assertIn("identity-provider", page.body.decode())
            # Missing the access group is always an IdP failure.
            page = main._access_denied_response({"authenticated": True, "groups": []})
            self.assertIn("missing openfeed", page.body.decode())
            # A built-in account is an admin by role, whatever groups are configured.
            member = {"session_backed": True, "idp": False, "authenticated": True, "methods": ["password"], "username": "alice"}
            page = main._access_denied_response(member, admin_only=True).body.decode()
            self.assertIn("This page is for admins of this OpenFeed.", page)
            self.assertNotIn("identity-provider", page)
        # A provider with no admin groups: admin is the account's role there too.
        with patch.dict(os.environ, {"OPENFEED_ADMIN_GROUPS": ""}):
            page = main._access_denied_response({"session_backed": True, "idp": True, "methods": ["oidc:authentik"]}, admin_only=True)
            self.assertNotIn("identity-provider", page.body.decode())


class SignInOffTests(unittest.TestCase):
    """Sign-in off: the sign-in steps go to the page asked for, never to OPENFEED_PUBLIC_URL."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "disabled"}, clear=True))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("db")))
        self.client = TestClient(main.app, base_url="http://127.0.0.1:8191")
        self.addCleanup(self.client.close)

    def test_sign_in_pages_go_home_or_to_next(self) -> None:
        cases = {
            "/auth/login": "/", "/auth/login?next=/history": "/history", "/auth/login?next=//evil.example/": "/",
            "/auth/login?next=https://evil.example/": "/", "/auth/login?next=/auth/login": "/",
            "/auth/signup": "/", "/auth/signup?next=/playlists": "/playlists", "/auth/2fa": "/", "/auth/2fa?recovery=1": "/",
            "/auth/invite/sometoken": "/", "/auth/reset/sometoken": "/",
        }
        for path, target in cases.items():
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], target)
        for path in ("/auth/login", "/auth/2fa", "/auth/signup"):
            with self.subTest(post=path):
                response = self.client.post(path, data={}, headers={"origin": "http://127.0.0.1:8191"}, follow_redirects=False)
                self.assertEqual(response.headers["location"], "/")

    def test_canonical_redirect_only_to_a_configured_public_url(self) -> None:
        request = SimpleNamespace(headers={"host": "192.168.1.5:8060"}, url=SimpleNamespace(netloc="192.168.1.5:8060"))
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "accounts"}, clear=True):
            self.assertFalse(main.get_settings().public_url_configured)
            self.assertIsNone(main._canonical_login_redirect(request, "/"))
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "accounts", "OPENFEED_PUBLIC_URL": "http://localhost:8060"}, clear=True):
            self.assertIsNotNone(main._canonical_login_redirect(request, "/"), "set on purpose, even to the default")


class NoticeTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make("root-admin", role="admin")
        self.alice = self.make("alice")

    def test_after_setting_a_password_the_sign_in_page_says_so(self) -> None:
        token = "set-password-token"
        self.store.create_link("reset", signin.token_hash(token), 3600, created_by=self.admin["id"], account_id=self.alice["id"])
        new = "a brand new passphrase"
        done = self.client.post(f"/auth/reset/{token}", data={"password": new, "password2": new}, follow_redirects=False)
        self.assertEqual(done.headers["location"], "/auth/login?done=password_set")
        page = self.client.get("/auth/login?done=password_set").text
        self.assertIn(escape(account_pages.NOTICES["password_set"], quote=True), page)
        self.assertIn('role="status"', page)
        # Only that notice, and no ?error= text, on the sign-in page.
        other = self.client.get("/auth/login?done=deleted&error=last_admin").text
        self.assertNotIn(account_pages.NOTICES["deleted"], other)
        self.assertNotIn(account_pages.ERRORS["last_admin"], other)

    def test_keep_and_fresh_land_home_with_a_toast(self) -> None:
        self.sign_in(self.client, "root-admin")
        with patch.object(account_pages, "_local_data_pending", return_value=True), \
             patch.object(accounts, "adopt_local_data", return_value={"playlists": 1}), \
             patch.object(main, "_forget_account_profiles"):
            kept = self.client.post("/setup/data", data={"choice": "keep"}, follow_redirects=False)
        self.assertEqual(kept.headers["location"], "/?done=kept")
        fresh = self.client.post("/setup/data", data={"choice": "fresh"}, follow_redirects=False)
        self.assertEqual(fresh.headers["location"], "/?done=fresh")
        script = (main._ASSET_DIR / "js" / "accounts.js").read_text()
        block = script.split("const HOME_NOTICES = {", 1)[1].split("};", 1)[0]
        for key in ("kept", "fresh"):
            self.assertIn(f"{key}: '{account_pages.NOTICES[key]}'", block, "the toast says what the server's notice says")
        self.assertIn("window.ofToast(message", script)
        self.assertIn("params.delete('done')", script)


class DevicesTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.make("root-admin", role="admin")
        self.alice = self.make("alice")

    def test_sessions_can_be_told_apart(self) -> None:
        others = [self.new_client() for _ in range(2)]
        for client in others:
            self.sign_in(client, "alice")
        self.sign_in(self.client, "alice")
        # Postgres hands back timestamps; the in-memory store keeps epoch seconds.
        listed = self.store.list_sessions

        def as_postgres(account_id: str) -> list[dict]:
            rows = listed(account_id)
            for row in rows:
                for key in ("created_at", "last_seen_at"):
                    row[key] = datetime.fromtimestamp(row[key], timezone.utc)
            return rows

        with patch.object(accounts, "list_sessions", as_postgres):
            html = self.client.get("/account").text
        devices = html.split(">Devices<", 1)[1]
        rows = re.findall(r'<div class="set-row acct-row( is-current)?" data-session-row>.*?</small>', devices, re.S)
        self.assertEqual(len(rows), 3)
        first = re.search(r'<div class="set-row acct-row is-current" data-session-row>.*?</small>', devices, re.S).group(0)
        self.assertTrue(devices.index(first) < devices.index('<div class="set-row acct-row" data-session-row>'),
                        "this device is listed first")
        self.assertIn("This device", first)
        self.assertRegex(first, r'Signed in just now \(<time datetime="\d{4}-\d\d-\d\dT[^"]+" data-local-time>[A-Z][a-z]{2} \d+, \d\d:\d\d UTC</time>\)')
        tags = re.findall(r'<span class="acct-session-tag"[^>]*>([0-9A-F]{4})</span>', devices)
        self.assertEqual(len(tags), 3)
        self.assertEqual(len(set(tags)), 3, "each session has its own tag")
        mine = [s for s in self.store.sessions.values() if s["account_id"] == self.alice["id"]]
        self.assertEqual({account_pages.session_tag(s["id"]) for s in mine}, set(tags))
        self.assertIn('aria-label="Sign out ', devices)
        self.assertNotIn("Chrome", account_pages.session_tag("abc"))


class PrivacyPaneTests(unittest.TestCase):
    def _pane(self, admin: bool) -> str:
        settings = SimpleNamespace(youtube_fallback_enabled=False, invidious_fallback_list=[], allow_public_instances=False,
                                   sponsorblock_enabled=False, dearrow_enabled=True, ryd_enabled=True,
                                   invidious_base_url="", invidious_public_url="", source_url="https://example.org/src")
        with patch.object(privacy, "get_settings", return_value=settings), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(privacy, "get_choices", lambda user_key="local": {"youtube_player": False, "public_instances": False}), \
             patch.object(db, "get_feed_preferences", return_value={}), \
             patch.object(db, "get_playback_preferences", return_value={"public_invidious_urls": []}):
            return main._privacy_pane("local", admin=admin)

    def _row(self, html: str, key: str) -> str:
        return re.search(rf'<div class="set-row privacy-row" data-privacy-service="{key}".*?</div></div>', html, re.S).group(0)

    def test_members_hear_that_an_admin_decides(self) -> None:
        html = self._pane(admin=False)
        for key in ("youtube_player", "public_instances", "sponsorblock_enabled"):
            with self.subTest(key=key):
                row = self._row(html, key)
                self.assertIn("Off on this server", row)
                self.assertIn("An admin of this OpenFeed can allow it for this server.", row)
                self.assertNotIn("/admin/server", row)
        self.assertNotIn("privacy-admin-note", self._row(html, "dearrow_enabled"), "allowed rows have a switch, no note")

    def test_admins_get_a_link_to_admin_server(self) -> None:
        html = self._pane(admin=True)
        self.assertIn('href="/admin/server#admin-players">Admin, Server</a>', self._row(html, "youtube_player"))
        self.assertIn('href="/admin/server#admin-players"', self._row(html, "public_instances"))
        self.assertIn('href="/admin/server#admin-lookups"', self._row(html, "sponsorblock_enabled"))
        self.assertNotIn("An admin of this OpenFeed", html)


class CrossOriginHeaderTests(unittest.TestCase):
    def test_every_response_gets_coop_and_corp(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "disabled"}, clear=True), \
             patch.object(db, "connect", side_effect=AssertionError("db")):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            digest = hashlib.sha256(account_pages._replace_script_text().encode()).hexdigest()[:16]
            for path in ("/assets/favicon-32.png", f"/assets/auth/replace-{digest}.js", "/api/does-not-exist", "/embed/does-not-exist"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.headers["cross-origin-opener-policy"], "same-origin")
                    self.assertEqual(response.headers["cross-origin-resource-policy"], "same-origin")

    def test_apply_headers_sets_them_for_pages_api_and_embed(self) -> None:
        for path in ("/", "/api/", "/embed/abc", "/watch/abc/player", "/vi/abc/hqdefault.jpg", "/api/download/file/1"):
            with self.subTest(path=path):
                headers: dict[str, str] = {}
                privacy.apply_headers(headers, path)
                self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
                self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")


if __name__ == "__main__":
    unittest.main()
