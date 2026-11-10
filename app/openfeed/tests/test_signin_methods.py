"""Phase 2 sign-in methods: several OIDC providers, passkeys and the trusted header.

The app runs for real (routes, middleware, pages); accounts.py is the in-memory store
(signin_support.SigninStore), Redis is the in-process fallback, the identity provider is
oidc_support.FakeIdP (real RS256 ID tokens) and passkeys come from a software
authenticator (signin_support.SoftAuthenticator) whose signatures py_webauthn checks.
Most tests are failure paths.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from openfeed import authstate, db, main, oidc, passkeys, privacy, signin, trusted_header
from openfeed.tests.oidc_support import FakeIdP
from openfeed.tests.signin_support import SigninStore, SoftAuthenticator, signin_accounts
from openfeed.tests.test_accounts_flows import PASSWORD, _Base

POCKET = {
    "OPENFEED_OIDC_PROVIDERS": "authentik,pocketid",
    "OPENFEED_OIDC_POCKETID_ISSUER": "https://id.example", "OPENFEED_OIDC_POCKETID_CLIENT_ID": "pid-client",
    "OPENFEED_OIDC_POCKETID_CLIENT_SECRET": "pid-secret", "OPENFEED_OIDC_POCKETID_ADMIN_GROUPS": "pid-admins",
}
LEGACY = {
    "OPENFEED_OIDC_ISSUER": "https://auth.example/application/o/openfeed", "OPENFEED_OIDC_CLIENT_ID": "id",
    "OPENFEED_OIDC_CLIENT_SECRET": "secret", "OPENFEED_ADMIN_GROUPS": "ops",
}


class _SigninBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.enterContext(signin_accounts(SigninStore()))
        authstate.reset()
        self.addCleanup(authstate.reset)
        trusted_header.forget()
        self.enterContext(patch.object(db, "upsert_user", return_value={}))

    def fresh(self, client: TestClient, username: str) -> None:
        """Signed in with a password just now (so "Confirm it's you" is fresh)."""
        self.assertEqual(self.sign_in(client, username).status_code, 303)


# --- OIDC ----------------------------------------------------------------------------------


class ProviderConfigTests(_SigninBase):
    env = {**_Base.env, **LEGACY, **POCKET}

    def test_single_provider_settings_stay_one_provider(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_OIDC_PROVIDERS": ""}):
            found = oidc.providers()
        self.assertEqual([p.name for p in found], ["authentik"])
        legacy = found[0]
        self.assertTrue(legacy.legacy)
        self.assertEqual(legacy.key, "oidc:authentik")
        self.assertEqual(legacy.callback_path, "/auth/callback")
        self.assertEqual(legacy.admin_groups, frozenset({"ops"}))
        self.assertEqual(oidc.callback_url(legacy), "http://testserver/auth/callback")

    def test_named_providers_with_their_own_settings(self) -> None:
        authentik, pocket = oidc.providers()
        self.assertTrue(authentik.legacy, "listing authentik keeps the single-provider settings as its defaults")
        self.assertEqual(authentik.client_id, "id")
        self.assertFalse(pocket.legacy)
        self.assertEqual((pocket.key, pocket.display_name, pocket.callback_path), ("oidc:pocketid", "Pocket ID", "/auth/oidc/pocketid/callback"))
        self.assertEqual(pocket.admin_groups, frozenset({"pid-admins"}))
        self.assertEqual(pocket.create_accounts, "registration")

    def test_bad_names_and_half_configured_providers_are_skipped(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_OIDC_PROVIDERS": "pocketid,Bad Name!,keycloak,pocketid"}):
            names = [p.name for p in oidc.providers()]
        self.assertEqual(names, ["authentik", "pocketid"])

    def test_sign_in_page_lists_each_provider_as_a_plain_link(self) -> None:
        self.make("root-admin", role="admin")
        page = self.client.get("/auth/login?next=/history")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/auth/oidc/authentik/start?next=%2Fhistory"', page.text)
        self.assertIn('href="/auth/oidc/pocketid/start?next=%2Fhistory"', page.text)
        self.assertIn("Sign in with Pocket ID", page.text)

    def test_registry_follows_the_configuration(self) -> None:
        self.assertIsInstance(signin.method("oidc:pocketid"), oidc.ProviderMethod)
        with patch.dict(os.environ, {"OPENFEED_OIDC_PROVIDERS": ""}):
            self.assertIsNone(signin.method("oidc:pocketid"))
            self.assertIsInstance(signin.method("oidc:authentik"), oidc.ProviderMethod)


class OidcFlowTests(_SigninBase):
    env = {**_Base.env, **LEGACY, **POCKET}

    def setUp(self) -> None:
        super().setUp()
        self.idp = FakeIdP(issuer="https://id.example", client_id="pid-client").install(self)
        self.idp.claims = {"sub": "pid-77", "preferred_username": "alice", "name": "Alice P", "email": "alice@example.com", "groups": []}
        self.codes = 0

    def begin(self, client: TestClient, query: str = "next=/history") -> tuple[str, str]:
        start = client.get(f"/auth/oidc/pocketid/start?{query}", follow_redirects=False)
        self.assertEqual(start.status_code, 303, start.text[:300])
        return self.idp.authorize(start.headers["location"])

    def back(self, client: TestClient, state: str, path: str = "/auth/oidc/pocketid/callback", code: str = ""):
        self.codes += 1
        return client.get(f"{path}?code={code or f'c{self.codes}'}&state={state}", follow_redirects=False)

    def test_start_uses_pkce_state_and_nonce(self) -> None:
        start = self.client.get("/auth/oidc/pocketid/start?next=/history", follow_redirects=False)
        location = urllib.parse.urlsplit(start.headers["location"])
        query = dict(urllib.parse.parse_qsl(location.query))
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}", "https://id.example/authorize")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["redirect_uri"], "http://testserver/auth/oidc/pocketid/callback")
        self.assertGreaterEqual(len(query["state"]), 32)
        self.assertGreaterEqual(len(query["nonce"]), 24)
        self.assertIn(main.get_settings().oidc_state_cookie, start.headers["set-cookie"])
        self.assertNotIn(query["state"], start.headers["set-cookie"], "the cookie names a hash of the state, not the state")
        flow = authstate.take("oidc", query["state"])
        self.assertEqual(oidc.pkce_challenge(flow["verifier"]), query["code_challenge"])
        self.assertEqual(flow["nonce"], query["nonce"])

    def test_token_request_sends_the_verifier(self) -> None:
        self.store.set_setting("registration_mode", "open")
        state, _ = self.begin(self.client)
        self.assertEqual(self.back(self.client, state).status_code, 303)
        request = self.idp.token_requests[-1]
        self.assertGreaterEqual(len(request["code_verifier"]), 43)
        self.assertEqual(request["redirect_uri"], "http://testserver/auth/oidc/pocketid/callback")

    def test_unknown_identity_needs_registration(self) -> None:
        state, _ = self.begin(self.client)
        refused = self.back(self.client, state)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Sign-in isn", refused.text)
        self.assertEqual(len(self.store.accounts), 0)
        self.assertFalse(self.session_cookie(self.client))

    def test_open_registration_makes_a_new_account_with_its_own_user_key(self) -> None:
        self.store.set_setting("registration_mode", "open")
        state, _ = self.begin(self.client)
        done = self.back(self.client, state)
        self.assertEqual((done.status_code, done.headers["location"]), (303, "/history"))
        account = self.store.find_identity("oidc:pocketid", "pid-77")
        self.assertIsNotNone(account)
        self.assertTrue(account["user_key"].startswith("u-"), "a named provider's sub is never used as the user_key")
        self.assertEqual(self.client.get("/api/").json()["user_key"], account["user_key"])

    def test_never_matched_to_an_existing_account_by_username_or_email(self) -> None:
        alice = self.make("alice")
        self.store.set_setting("registration_mode", "open")
        state, _ = self.begin(self.client)
        self.back(self.client, state)
        made = self.store.find_identity("oidc:pocketid", "pid-77")
        self.assertNotEqual(made["id"], alice["id"])
        self.assertEqual(made["username"], "alice2")
        self.assertEqual([i["method"] for i in self.store.list_identities(alice["id"])], ["password"])

    def test_invite_link_lets_a_provider_create_the_account(self) -> None:
        token = "invite-token-xyz"
        self.store.create_link("invite", signin.token_hash(token), 3600, role="admin")
        state, _ = self.begin(self.client, f"invite={token}")
        done = self.back(self.client, state)
        self.assertEqual(done.status_code, 303)
        made = self.store.find_identity("oidc:pocketid", "pid-77")
        self.assertEqual(made["role"], "admin")
        self.assertIsNone(self.store.find_link("invite", signin.token_hash(token)), "the invite is used up")
        # The same invite cannot start another flow.
        again = self.new_client().get(f"/auth/oidc/pocketid/start?invite={token}", follow_redirects=False)
        self.assertEqual(again.status_code, 410)

    def test_create_accounts_always(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_OIDC_POCKETID_CREATE_ACCOUNTS": "always"}):
            state, _ = self.begin(self.client)
            self.assertEqual(self.back(self.client, state).status_code, 303)
        self.assertIsNotNone(self.store.find_identity("oidc:pocketid", "pid-77"))

    def _rejected(self, response, status_code: int, phrase: str) -> None:
        self.assertEqual(response.status_code, status_code, response.text[:400])
        self.assertIn(phrase, response.text)
        self.assertFalse(self.session_cookie(self.client))
        self.assertEqual(len(self.store.sessions), 0)

    def test_bad_state_is_refused_before_the_code_is_spent(self) -> None:
        self.begin(self.client)
        self._rejected(self.back(self.client, "not-the-state"), 400, "expired or was already used")
        self.assertEqual(self.idp.token_requests, [])

    def test_state_from_another_browser_is_refused(self) -> None:
        state, _ = self.begin(self.client)
        other = self.new_client()
        response = self.back(other, state)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.idp.token_requests, [])

    def test_replayed_callback_is_refused(self) -> None:
        self.store.set_setting("registration_mode", "open")
        state, _ = self.begin(self.client)
        self.assertEqual(self.back(self.client, state, code="code-1").status_code, 303)
        thief = self.new_client()
        thief.cookies.set(main.get_settings().oidc_state_cookie, signin.sign("oidc-state", {"s": oidc._state_hash(state), "exp": int(time.time()) + 60}))
        replay = self.back(thief, state, code="code-1")
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(len(self.idp.token_requests), 1, "the second use never reached the token endpoint")

    def test_replayed_code_refused_by_the_provider(self) -> None:
        self.store.set_setting("registration_mode", "open")
        self.idp.codes_used.append("stolen")
        state, _ = self.begin(self.client)
        self._rejected(self.back(self.client, state, code="stolen"), 403, "refused the sign-in code")

    def test_wrong_nonce(self) -> None:
        state, _ = self.begin(self.client)
        self.idp.override_token = self.idp.id_token(nonce="someone-elses-nonce")
        self._rejected(self.back(self.client, state), 403, "nonce mismatch")

    def test_missing_nonce(self) -> None:
        state, _ = self.begin(self.client)
        self.idp.override_token = self.idp.id_token(nonce=None)
        self._rejected(self.back(self.client, state), 403, "nonce mismatch")

    def test_wrong_issuer(self) -> None:
        state, _ = self.begin(self.client)
        self.idp.override_token = self.idp.id_token(iss="https://evil.example")
        self._rejected(self.back(self.client, state), 403, "different issuer")

    def test_wrong_audience(self) -> None:
        state, _ = self.begin(self.client)
        self.idp.override_token = self.idp.id_token(aud="another-app")
        self._rejected(self.back(self.client, state), 403, "different application")

    def test_wrong_authorized_party(self) -> None:
        state, _ = self.begin(self.client)
        self.idp.override_token = self.idp.id_token(aud=["pid-client", "another-app"], azp="another-app")
        self._rejected(self.back(self.client, state), 403, "different application")

    def test_expired_token(self) -> None:
        state, _ = self.begin(self.client)
        now = int(time.time())
        self.idp.override_token = self.idp.id_token(iat=now - 3600, exp=now - 600)
        self._rejected(self.back(self.client, state), 403, "took too long")

    def test_token_signed_by_another_key(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        state, _ = self.begin(self.client)
        self.idp.override_token = self.idp.id_token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
        self._rejected(self.back(self.client, state), 403, "signature")

    def test_unsigned_token(self) -> None:
        import jwt

        state, _ = self.begin(self.client)
        self.idp.override_token = jwt.encode({"sub": "pid-77", "iss": "https://id.example/", "aud": "pid-client", "nonce": self.idp.nonce,
                                              "iat": int(time.time()), "exp": int(time.time()) + 60}, None, algorithm="none")
        self._rejected(self.back(self.client, state), 403, "unsigned")

    def test_shared_secret_token_needs_the_client_secret(self) -> None:
        self.store.set_setting("registration_mode", "open")
        state, _ = self.begin(self.client)
        # Authentik signs with HS256 and the client secret when no signing key is chosen.
        self.idp.override_token = self.idp.id_token(key=b"pid-secret", alg="HS256")
        self.assertEqual(self.back(self.client, state).status_code, 303)
        state, _ = self.begin(self.new_client())
        self.idp.override_token = self.idp.id_token(key=b"a-guess", alg="HS256")
        self.assertEqual(self.back(self.client, state).status_code, 400)

    def test_userinfo_for_someone_else_is_refused(self) -> None:
        self.store.set_setting("registration_mode", "open")
        state, _ = self.begin(self.client)
        self.idp.userinfo = {"sub": "someone-else", "groups": ["pid-admins"]}
        self._rejected(self.back(self.client, state), 403, "different person")

    def test_callback_for_another_provider_is_refused(self) -> None:
        state, _ = self.begin(self.client)
        mixed = self.back(self.client, state, path="/auth/callback")  # the legacy provider's callback
        # /auth/callback serves whichever provider the flow belongs to, so this one works...
        self.assertIn(mixed.status_code, {303, 403})
        state, _ = self.begin(self.client)
        wrong = self.back(self.client, state, path="/auth/oidc/authentik/callback")
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("wrong place", wrong.text)

    def test_provider_error_is_shown_and_spends_the_state(self) -> None:
        state, _ = self.begin(self.client)
        response = self.client.get(f"/auth/oidc/pocketid/callback?error=access_denied&error_description=<b>nope</b>&state={state}")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("<b>", response.text)
        self.assertIsNone(authstate.take("oidc", state))

    def test_disabled_account_is_refused(self) -> None:
        account = self.store.create_account("pat", identity=("oidc:pocketid", "pid-77", "Pocket ID"))
        self.store.accounts[account["id"]]["disabled"] = True
        state, _ = self.begin(self.client)
        self._rejected(self.back(self.client, state), 403, "disabled")

    def test_provider_sign_in_still_asks_for_two_step(self) -> None:
        account = self.store.create_account("pat", identity=("oidc:pocketid", "pid-77", "Pocket ID"))
        self.store.accounts[account["id"]]["totp_enabled_at"] = time.time()
        state, _ = self.begin(self.client)
        done = self.back(self.client, state)
        self.assertEqual(done.headers["location"], "/auth/2fa")

    def test_admin_groups_are_per_provider(self) -> None:
        account = self.store.create_account("pat", identity=("oidc:pocketid", "pid-77", "Pocket ID"))
        self.idp.claims["groups"] = ["ops"]  # the *Authentik* admin group, at Pocket ID
        state, _ = self.begin(self.client)
        self.back(self.client, state)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.idp.claims["groups"] = ["pid-admins"]
        browser = self.new_client()
        state, _ = self.begin(browser)
        self.back(browser, state)
        self.assertEqual(browser.get("/admin/users").status_code, 200)
        self.assertEqual(self.store.get_account(account["id"])["role"], "member", "groups grant admin per session, not in the database")

    def test_required_group_per_provider(self) -> None:
        self.store.create_account("pat", identity=("oidc:pocketid", "pid-77", "Pocket ID"))
        with patch.dict(os.environ, {"OPENFEED_OIDC_POCKETID_REQUIRED_GROUP": "family"}):
            state, _ = self.begin(self.client)
            self.assertEqual(self.back(self.client, state).status_code, 403)
            self.idp.claims["groups"] = ["family"]
            state, _ = self.begin(self.client)
            self.assertEqual(self.back(self.client, state).status_code, 303)

    def test_linking_needs_confirm_and_never_steals(self) -> None:
        bob = self.make("bob")
        self.sign_in(self.client, "bob")
        # A session that is not fresh must confirm first.
        session = next(iter(self.store.sessions.values()))
        session["reauth_at"] = time.time() - 3600
        first = self.client.get("/auth/oidc/pocketid/start?intent=link", follow_redirects=False)
        self.assertTrue(first.headers["location"].startswith("/auth/confirm"))
        session["reauth_at"] = time.time()
        state, _ = self.begin(self.client, "intent=link")
        linked = self.back(self.client, state)
        self.assertEqual(linked.headers["location"], "/account?done=linked")
        self.assertEqual(self.store.find_identity("oidc:pocketid", "pid-77")["id"], bob["id"])
        # Someone else's provider identity cannot be linked to a second account.
        carol = self.make("carol")
        browser = self.new_client()
        self.sign_in(browser, "carol")
        state, _ = self.begin(browser, "intent=link")
        taken = self.back(browser, state)
        self.assertEqual(taken.headers["location"], "/account?error=identity_taken")
        self.assertEqual([i["method"] for i in self.store.list_identities(carol["id"])], ["password"])

    def test_link_flow_is_bound_to_the_account_that_started_it(self) -> None:
        self.make("bob")
        self.sign_in(self.client, "bob")
        state, _ = self.begin(self.client, "intent=link")
        # The browser signs in as someone else before the provider answers.
        self.make("carol")
        self.sign_in(self.client, "carol")
        done = self.back(self.client, state)
        self.assertEqual(done.headers["location"], "/auth/login")
        self.assertIsNone(self.store.find_identity("oidc:pocketid", "pid-77"))

    def test_reauth_with_a_provider(self) -> None:
        bob = self.make("bob")
        self.store.add_identity(bob["id"], "oidc:pocketid", "pid-77", "Pocket ID")
        self.sign_in(self.client, "bob")
        session = next(iter(self.store.sessions.values()))
        session["reauth_at"] = time.time() - 3600
        confirm = self.client.get("/auth/confirm?next=/account")
        self.assertIn("Confirm with Pocket ID", confirm.text)
        start = self.client.get("/auth/oidc/pocketid/start?intent=reauth&next=/account", follow_redirects=False)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.headers["location"]).query))
        self.assertEqual((query["prompt"], query["max_age"]), ("login", "0"))
        self.idp.authorize(start.headers["location"])
        self.idp.claims["auth_time"] = int(time.time())
        done = self.back(self.client, query["state"])
        self.assertEqual(done.headers["location"], "/account")
        self.assertGreater(next(iter(self.store.sessions.values()))["reauth_at"], time.time() - 5)

    def test_reauth_with_someone_elses_provider_account(self) -> None:
        bob = self.make("bob")
        self.store.add_identity(bob["id"], "oidc:pocketid", "pid-77", "Pocket ID")
        self.sign_in(self.client, "bob")
        start = self.client.get("/auth/oidc/pocketid/start?intent=reauth&next=/account", follow_redirects=False)
        state, _ = self.idp.authorize(start.headers["location"])
        self.idp.claims["sub"] = "pid-someone-else"
        done = self.back(self.client, state)
        self.assertEqual(done.headers["location"], "/account?error=oidc_mismatch")

    def test_backchannel_issuer_spelling_is_accepted_only_for_its_provider(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_OIDC_POCKETID_BACKCHANNEL_BASE_URL": "http://pocket-id:1411"}):
            config = oidc.provider("pocketid")
        self.assertIn("http://pocket-id:1411", oidc.expected_issuers(config))
        self.assertNotIn("http://pocket-id:1411", oidc.expected_issuers(oidc.provider("pocketid")))


class LegacyProviderTests(_SigninBase):
    """authentik_oidc mode with only the single-provider settings: unchanged behaviour."""

    env = {"OPENFEED_AUTH_MODE": "authentik_oidc", "OPENFEED_SECRET_KEY": "flow-test-secret", "OPENFEED_PUBLIC_URL": "http://testserver", **LEGACY}

    def test_login_still_goes_straight_to_the_provider_and_creates_accounts(self) -> None:
        idp = FakeIdP().install(self)
        start = self.client.get("/auth/login?next=/history", follow_redirects=False)
        self.assertTrue(start.headers["location"].startswith("https://auth.example/authorize?"))
        state, _ = idp.authorize(start.headers["location"])
        done = self.client.get(f"/auth/callback?code=x&state={state}", follow_redirects=False)
        self.assertEqual(done.headers["location"], "/history")
        self.assertEqual(self.store.get_by_user_key("ak-sub-123")["username"], "alice")

    def test_a_second_provider_shows_the_sign_in_page(self) -> None:
        with patch.dict(os.environ, POCKET):
            page = self.client.get("/auth/login", follow_redirects=False)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sign in with Authentik", page.text)
        self.assertIn("Sign in with Pocket ID", page.text)

    def test_existing_user_key_from_before_accounts_is_kept(self) -> None:
        idp = FakeIdP().install(self)
        old = self.store.create_account("alice", user_key="ak-sub-123")
        start = self.client.get("/auth/login", follow_redirects=False)
        state, _ = idp.authorize(start.headers["location"])
        self.client.get(f"/auth/callback?code=x&state={state}", follow_redirects=False)
        self.assertEqual(self.store.find_identity("oidc:authentik", "ak-sub-123")["id"], old["id"])


# --- passkeys ----------------------------------------------------------------------------------


class PasskeyStatusTests(_SigninBase):
    def test_secure_context_rules(self) -> None:
        cases = {
            "https://openfeed.example": (True, ""),
            "http://localhost:8060": (True, ""),
            "http://openfeed.example": (False, "secure connection"),
            "http://192.168.1.20:8060": (False, "IP address"),
            "https://10.0.0.5": (False, "IP address"),
        }
        for url, (ok, phrase) in cases.items():
            with self.subTest(url=url), patch.dict(os.environ, {"OPENFEED_PUBLIC_URL": url}):
                got, reason = passkeys.status()
                self.assertEqual(got, ok)
                self.assertIn(phrase, reason)
        with patch.dict(os.environ, {"OPENFEED_PASSKEYS": "off", "OPENFEED_PUBLIC_URL": "https://openfeed.example"}):
            self.assertEqual(passkeys.status()[0], False)

    def test_off_by_default_in_authentik_mode(self) -> None:
        env = {"OPENFEED_AUTH_MODE": "authentik_oidc", "OPENFEED_PUBLIC_URL": "https://openfeed.example", **LEGACY}
        with patch.dict(os.environ, env):
            self.assertFalse(passkeys.status()[0])
            with patch.dict(os.environ, {"OPENFEED_PASSKEYS": "on"}):
                self.assertTrue(passkeys.status()[0])

    def test_rp_id_and_origin_come_from_the_public_url(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_PUBLIC_URL": "https://Feed.Example.org:8443"}):
            self.assertEqual(passkeys.relying_party(), ("feed.example.org", "https://feed.example.org:8443"))

    def test_permissions_policy_allows_passkeys_for_this_origin_only(self) -> None:
        policy = privacy.permissions_policy(["https://www.youtube-nocookie.com"])
        self.assertIn("publickey-credentials-get=(self)", policy)
        self.assertIn("publickey-credentials-create=(self)", policy)
        headers = self.client.get("/auth/login").headers
        self.assertIn("publickey-credentials-get=(self)", headers["Permissions-Policy"])

    def test_insecure_server_explains_and_offers_nothing(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_PUBLIC_URL": "http://192.168.1.20:8060"}):
            self.make("root-admin", role="admin")
            page = self.client.get("/auth/login")
            self.assertNotIn("data-passkey-signin", page.text)
            self.assertNotIn("webauthn", page.text)
            client = TestClient(main.app, base_url="http://192.168.1.20:8060")
            self.addCleanup(client.close)
            client.headers["origin"] = "http://192.168.1.20:8060"
            self.sign_in(client, "root-admin")
            account = client.get("/account")
            self.assertIn("IP address", account.text)
            self.assertNotIn("data-passkey-add", account.text)
            options = client.post("/account/passkeys/options", json={})
            self.assertEqual(options.status_code, 404)


class PasskeyTests(_SigninBase):
    env = {**_Base.env, "OPENFEED_PUBLIC_URL": "https://openfeed.test"}
    ORIGIN = "https://openfeed.test"

    def setUp(self) -> None:
        super().setUp()
        self.client = self.secure_client()
        self.admin = self.make("root-admin", role="admin")
        self.alice = self.make("alice")
        self.key = SoftAuthenticator("openfeed.test", self.ORIGIN)

    def secure_client(self) -> TestClient:
        client = TestClient(main.app, base_url=self.ORIGIN)
        client.headers["origin"] = self.ORIGIN
        self.addCleanup(client.close)
        return client

    def add_passkey(self, client: TestClient, key: SoftAuthenticator, name: str = "Phone", uv: bool = True):
        options = client.post("/account/passkeys/options", json={"name": name})
        self.assertEqual(options.status_code, 200, options.text)
        body = options.json()
        return client.post("/account/passkeys/register", json={"flow": body["flow"], "name": name, "credential": key.create(body["publicKey"], uv=uv)})

    def passkey_sign_in(self, client: TestClient, key: SoftAuthenticator, *, username: str = "", uv: bool = True, **get_args):
        options = client.post("/auth/passkey/options", json={"username": username, "next": "/history"}).json()
        credential = key.get(options["publicKey"], uv=uv, **get_args)
        return client.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": credential}), options

    def test_add_list_rename_and_sign_in_usernameless(self) -> None:
        self.sign_in(self.client, "alice")
        added = self.add_passkey(self.client, self.key, "Pixel 9")
        self.assertEqual(added.json(), {"redirect": "/account?done=passkey_added"})
        stored = self.store.list_method_identities(self.alice["id"], "passkey")
        self.assertEqual(len(stored), 1)
        data = stored[0]["data"]
        self.assertEqual(stored[0]["subject"], self.key.id)
        self.assertEqual(set(data), {"public_key", "sign_count", "user_handle", "transports", "aaguid", "device_type", "backed_up", "user_verified"})
        self.assertNotEqual(data["user_handle"], self.alice["user_key"])
        page = self.client.get("/account")
        self.assertIn("Pixel 9", page.text)
        self.assertIn("data-passkey-add", page.text)
        self.assertIn("Not used yet", page.text)
        renamed = self.client.post(f"/account/passkeys/{stored[0]['id']}/rename", data={"name": "Work phone"}, follow_redirects=False)
        self.assertEqual(renamed.headers["location"], "/account?done=passkey_renamed")
        browser = self.secure_client()
        done, options = self.passkey_sign_in(browser, self.key)
        self.assertEqual(options["publicKey"]["allowCredentials"], [], "usernameless: the browser offers its own list")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["redirect"], "/history")
        self.assertIn("HttpOnly", done.headers["set-cookie"])
        self.assertEqual(browser.get("/api/").json()["user_key"], self.alice["user_key"])
        session = [s for s in self.store.sessions.values() if s["account_id"] == self.alice["id"]][-1]
        self.assertEqual(session["methods"], ["passkey", "passkey-uv"])
        self.assertEqual(self.store.list_method_identities(self.alice["id"], "passkey")[0]["data"]["sign_count"], self.key.sign_count)

    def test_adding_needs_a_fresh_confirmation(self) -> None:
        self.sign_in(self.client, "alice")
        next(iter(s for s in self.store.sessions.values() if s["account_id"] == self.alice["id"]))["reauth_at"] = time.time() - 3600
        options = self.client.post("/account/passkeys/options", json={})
        self.assertEqual(options.status_code, 403)
        self.assertEqual(options.json()["redirect"], "/auth/confirm?next=/account")
        anonymous = self.secure_client().post("/account/passkeys/options", json={})
        self.assertEqual(anonymous.status_code, 401)

    def test_username_first_narrows_to_that_account(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        options = self.secure_client().post("/auth/passkey/options", json={"username": "alice"}).json()
        self.assertEqual([c["id"] for c in options["publicKey"]["allowCredentials"]], [self.key.id])
        unknown = self.secure_client().post("/auth/passkey/options", json={"username": "nobody"}).json()
        self.assertEqual(unknown["publicKey"]["allowCredentials"], [])
        # Alice's passkey, typed username "root-admin": refused.
        browser = self.secure_client()
        options = browser.post("/auth/passkey/options", json={"username": "root-admin"}).json()
        options["publicKey"]["allowCredentials"] = []
        refused = browser.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": self.key.get(options["publicKey"])})
        self.assertEqual(refused.status_code, 401)
        self.assertFalse(self.session_cookie(browser))

    def test_challenge_is_single_use(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        browser = self.secure_client()
        options = browser.post("/auth/passkey/options", json={}).json()
        credential = self.key.get(options["publicKey"])
        self.assertEqual(browser.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": credential}).status_code, 200)
        replay = self.secure_client().post("/auth/passkey/verify", json={"flow": options["flow"], "credential": credential})
        self.assertEqual(replay.status_code, 400)
        self.assertIn("expired or was already used", replay.json()["error"])

    def test_challenge_must_be_the_servers(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        browser = self.secure_client()
        options = browser.post("/auth/passkey/options", json={}).json()
        forged = self.key.get(options["publicKey"], challenge="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(browser.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": forged}).status_code, 401)

    def test_expired_challenge(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        browser = self.secure_client()
        options = browser.post("/auth/passkey/options", json={}).json()
        later = time.time() + passkeys.CHALLENGE_SECONDS + 5
        with patch.object(authstate.time, "time", return_value=later):
            response = browser.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": self.key.get(options["publicKey"])})
        self.assertEqual(response.status_code, 400)

    def test_wrong_origin_is_refused(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        refused, _ = self.passkey_sign_in(self.secure_client(), self.key, origin="https://openfeed.test.evil.example")
        self.assertEqual(refused.status_code, 401)

    def test_counter_going_backwards_is_refused(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        ok, _ = self.passkey_sign_in(self.secure_client(), self.key, counter_step=5)
        self.assertEqual(ok.status_code, 200)
        self.key.sign_count -= 3  # a clone with an older counter
        with self.assertLogs("openfeed.auth", "WARNING") as logs:
            cloned, _ = self.passkey_sign_in(self.secure_client(), self.key, counter_step=0)
        self.assertEqual(cloned.status_code, 401)
        self.assertTrue(any("counter went backwards" in line for line in logs.output))

    def test_synced_passkeys_with_zero_counters_work(self) -> None:
        synced = SoftAuthenticator("openfeed.test", self.ORIGIN, synced=True)
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, synced)
        for _ in range(2):
            done, _ = self.passkey_sign_in(self.secure_client(), synced)
            self.assertEqual(done.status_code, 200)

    def test_user_handle_must_match(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        refused, _ = self.passkey_sign_in(self.secure_client(), self.key, user_handle=b"someone-else-handle")
        self.assertEqual(refused.status_code, 401)

    def test_unknown_or_other_accounts_credential(self) -> None:
        stranger = SoftAuthenticator("openfeed.test", self.ORIGIN)
        refused, _ = self.passkey_sign_in(self.secure_client(), stranger)
        self.assertEqual(refused.status_code, 401)

    def test_disabled_account(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        self.store.accounts[self.alice["id"]]["disabled"] = True
        refused, _ = self.passkey_sign_in(self.secure_client(), self.key)
        self.assertEqual(refused.status_code, 401)

    def test_verified_passkey_satisfies_two_step_and_unverified_does_not(self) -> None:
        self.store.accounts[self.alice["id"]]["totp_enabled_at"] = time.time()
        self.store.accounts[self.alice["id"]]["totp_secret"] = "JBSWY3DPEHPK3PXP"
        # Alice has 2FA on: give her a signed-in session directly to add the passkey.
        session_browser = self.secure_client()
        token = signin.new_token()
        self.store.create_session(self.alice["id"], signin.token_hash(token), device="t", methods=["password", "totp"], idp_groups=[],
                                  idle_seconds=600, max_seconds=600)
        session_browser.cookies.set(main.get_settings().session_cookie, token)
        self.assertEqual(self.add_passkey(session_browser, self.key).status_code, 200)
        verified, _ = self.passkey_sign_in(self.secure_client(), self.key, uv=True)
        self.assertEqual(verified.json()["redirect"], "/history")
        unverified, _ = self.passkey_sign_in(self.secure_client(), self.key, uv=False)
        self.assertEqual(unverified.json()["redirect"], "/auth/2fa", "presence alone is one factor")

    def test_passkey_only_account_and_last_way_in(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        password = next(i for i in self.store.list_identities(self.alice["id"]) if i["method"] == "password")
        removed = self.client.post(f"/account/methods/{password['id']}/remove", follow_redirects=False)
        self.assertEqual(removed.headers["location"], "/account?done=method_removed")
        page = self.client.get("/account")
        self.assertIn("Only way in", page.text)
        passkey = self.store.list_method_identities(self.alice["id"], "passkey")[0]
        refused = self.client.post(f"/account/methods/{passkey['id']}/remove", follow_redirects=False)
        self.assertEqual(refused.headers["location"], "/account?error=last_method")
        self.assertEqual(len(self.store.list_identities(self.alice["id"])), 1)
        # A passkey-only account confirms it's you with the passkey.
        session = [s for s in self.store.sessions.values() if s["account_id"] == self.alice["id"]][-1]
        session["reauth_at"] = time.time() - 3600
        confirm = self.client.get("/auth/confirm?next=/account")
        self.assertIn("data-passkey-confirm", confirm.text)
        self.assertNotIn('name="password"', confirm.text)
        options = self.client.post("/auth/passkey/options", json={"intent": "reauth", "next": "/account"}).json()
        self.assertEqual(options["publicKey"]["userVerification"], "required")
        done = self.client.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": self.key.get(options["publicKey"])})
        self.assertEqual(done.json()["redirect"], "/account")
        self.assertGreater(session["reauth_at"], time.time() - 5)

    def test_reauth_with_another_accounts_passkey_is_refused(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        admin = self.secure_client()
        self.sign_in(admin, "root-admin")
        options = admin.post("/auth/passkey/options", json={"intent": "reauth"})
        self.assertEqual(options.status_code, 400, "the admin has no passkey")
        self.add_passkey(admin, SoftAuthenticator("openfeed.test", self.ORIGIN))
        options = admin.post("/auth/passkey/options", json={"intent": "reauth"}).json()
        options["publicKey"]["allowCredentials"] = []
        refused = admin.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": self.key.get(options["publicKey"])})
        self.assertEqual(refused.status_code, 401)

    def test_reauth_needs_user_verification(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        options = self.client.post("/auth/passkey/options", json={"intent": "reauth"}).json()
        refused = self.client.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": self.key.get(options["publicKey"], uv=False)})
        self.assertEqual(refused.status_code, 401)

    def test_cannot_rename_or_remove_someone_elses_passkey(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        passkey = self.store.list_method_identities(self.alice["id"], "passkey")[0]
        admin = self.secure_client()
        self.sign_in(admin, "root-admin")
        renamed = admin.post(f"/account/passkeys/{passkey['id']}/rename", data={"name": "mine now"}, follow_redirects=False)
        self.assertEqual(renamed.headers["location"], "/account?error=not_found")
        removed = admin.post(f"/account/methods/{passkey['id']}/remove", follow_redirects=False)
        self.assertEqual(removed.headers["location"], "/account?error=not_found")
        self.assertEqual(self.store.identities[passkey["id"]]["label"], "Phone")

    def test_same_authenticator_twice_is_refused(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        options = self.client.post("/account/passkeys/options", json={}).json()
        self.assertEqual([c["id"] for c in options["publicKey"]["excludeCredentials"]], [self.key.id])
        self.assertEqual(options["publicKey"]["attestation"], "none")
        self.assertEqual(options["publicKey"]["authenticatorSelection"]["residentKey"], "preferred")
        second = SoftAuthenticator("openfeed.test", self.ORIGIN)
        self.add_passkey(self.client, second)
        handles = {i["data"]["user_handle"] for i in self.store.list_method_identities(self.alice["id"], "passkey")}
        self.assertEqual(len(handles), 1, "one user handle per account")

    def test_registration_flow_belongs_to_its_account(self) -> None:
        self.sign_in(self.client, "alice")
        options = self.client.post("/account/passkeys/options", json={}).json()
        admin = self.secure_client()
        self.sign_in(admin, "root-admin")
        stolen = admin.post("/account/passkeys/register", json={"flow": options["flow"], "credential": self.key.create(options["publicKey"])})
        self.assertEqual(stolen.status_code, 400)
        self.assertEqual(self.store.list_method_identities(self.admin["id"], "passkey"), [])

    def test_sign_in_page_offers_passkeys_with_autofill(self) -> None:
        page = self.secure_client().get("/auth/login")
        self.assertIn('autocomplete="username webauthn"', page.text)
        self.assertIn("data-passkey-signin", page.text)
        self.assertRegex(page.text, r'<button class="btn auth-method" type="button" data-passkey-signin[^>]* hidden>')
        self.assertIn('data-passkey-origin="https://openfeed.test"', page.text)
        script = page.text.split('<script src="/assets/auth/passkeys-')[1].split('"')[0]
        asset = self.client.get(f"/assets/auth/passkeys-{script}")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("PublicKeyCredential", asset.text)
        self.assertEqual(self.client.get("/assets/auth/passkeys-0000000000000000.js").status_code, 404)

    def test_options_are_rate_limited(self) -> None:
        browser = self.secure_client()
        for _ in range(passkeys.OPTIONS_CLIENT.limit):
            browser.post("/auth/passkey/options", json={})
        refused = browser.post("/auth/passkey/options", json={})
        self.assertEqual(refused.status_code, 429)
        self.assertNotIn("publicKey", refused.json())

    def test_page_loads_from_one_address_do_not_lock_a_household_out(self) -> None:
        # Every sign-in page load asks for options (autofill). Several people behind one
        # NAT reloading the page all day must still get a challenge, and a passkey.
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        self.assertGreaterEqual(passkeys.OPTIONS_CLIENT.limit, 300)
        for _ in range(120):
            self.assertEqual(self.secure_client().post("/auth/passkey/options", json={"conditional": True}).status_code, 200)
        done, _ = self.passkey_sign_in(self.secure_client(), self.key)
        self.assertEqual(done.status_code, 200, done.text)

    def test_limited_autofill_request_is_quiet(self) -> None:
        browser = self.secure_client()
        with patch.object(passkeys, "OPTIONS_CLIENT", passkeys.ratelimit.Rule("passkey-options", 2, 60)):
            for _ in range(2):
                self.assertIn("publicKey", browser.post("/auth/passkey/options", json={"conditional": True}).json())
            quiet = browser.post("/auth/passkey/options", json={"conditional": True})
            self.assertEqual(quiet.status_code, 200, "no 4xx, so the browser console stays clean")
            self.assertEqual(quiet.json(), {"quiet": True})
            self.assertEqual(quiet.headers["cache-control"], "no-store")
            pressed = browser.post("/auth/passkey/options", json={})
            self.assertEqual(pressed.status_code, 429, "a pressed button still hears why")
        script = passkeys._script_text()
        self.assertIn("conditional: quietly", script)
        self.assertIn("options.data.quiet", script)

    def test_stored_challenges_are_capped_for_the_install(self) -> None:
        with patch.object(passkeys, "OPTIONS_GLOBAL", passkeys.ratelimit.Rule("passkey-options-all", 3, 60)):
            for _ in range(3):
                self.assertEqual(self.secure_client().post("/auth/passkey/options", json={}).status_code, 200)
            self.assertEqual(self.secure_client().post("/auth/passkey/options", json={}).status_code, 429)
            self.assertEqual(self.secure_client().post("/auth/passkey/options", json={"conditional": True}).json(), {"quiet": True})

    def test_verify_is_limited_per_account(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        for _ in range(passkeys.VERIFY_ACCOUNT.limit):
            browser = self.secure_client()
            options = browser.post("/auth/passkey/options", json={}).json()
            forged = self.key.get(options["publicKey"], challenge="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            self.assertEqual(browser.post("/auth/passkey/verify", json={"flow": options["flow"], "credential": forged}).status_code, 401)
        # The real passkey, from the same client: the account is locked for the window.
        locked, _ = self.passkey_sign_in(self.secure_client(), self.key)
        self.assertEqual(locked.status_code, 429)
        self.assertNotIn("set-cookie", locked.headers)
        # Another account's passkey still works from here (the client limit is higher).
        self.sign_in(self.client, "root-admin")
        other = SoftAuthenticator("openfeed.test", self.ORIGIN)
        self.add_passkey(self.client, other)
        ok, _ = self.passkey_sign_in(self.secure_client(), other)
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_verify_is_limited_per_client(self) -> None:
        self.sign_in(self.client, "alice")
        self.add_passkey(self.client, self.key)
        stranger = SoftAuthenticator("openfeed.test", self.ORIGIN)
        for _ in range(passkeys.ratelimit.SIGNIN_CLIENT.limit):
            refused, _ = self.passkey_sign_in(self.secure_client(), stranger)
            self.assertEqual(refused.status_code, 401)
        locked, _ = self.passkey_sign_in(self.secure_client(), self.key)
        self.assertEqual(locked.status_code, 429)

    def test_passkey_posts_need_a_same_site_origin(self) -> None:
        browser = TestClient(main.app, base_url=self.ORIGIN)
        self.addCleanup(browser.close)
        browser.headers["origin"] = "https://evil.example"
        self.assertEqual(browser.post("/auth/passkey/options", json={}).status_code, 403)


# --- trusted header ---------------------------------------------------------------------------


class TrustedHeaderTests(_SigninBase):
    env = {**_Base.env, "OPENFEED_TRUSTED_HEADER": "Remote-User", "OPENFEED_TRUSTED_HEADER_GROUPS": "Remote-Groups",
           "OPENFEED_TRUSTED_HEADER_NAME": "Remote-Name", "OPENFEED_TRUSTED_PROXIES": "10.0.0.0/24",
           "OPENFEED_TRUSTED_HEADER_ADMIN_GROUPS": "ops"}

    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make("root-admin", role="admin")

    def proxy(self, peer: str = "10.0.0.2") -> TestClient:
        client = TestClient(main.app, client=(peer, 40000))
        client.headers["origin"] = "http://testserver"
        self.addCleanup(client.close)
        return client

    def link(self, username: str, subject: str) -> dict:
        account = self.make(username)
        self.store.add_identity(account["id"], trusted_header.method_key(), subject, "Proxy")
        return account

    def test_trusted_proxy_signs_a_linked_name_in_with_a_server_session(self) -> None:
        alice = self.link("alice", "alice@corp")
        browser = self.proxy()
        me = browser.get("/api/", headers={"Remote-User": "alice@corp"}).json()
        self.assertEqual(me["user_key"], alice["user_key"])
        token = self.session_cookie(browser)
        self.assertTrue(token.startswith("v2."))
        self.assertEqual([s["methods"] for s in self.store.sessions.values()], [["header:remote-user"]])
        # The session cookie carries the visit; a second request makes no second session.
        browser.get("/api/", headers={"Remote-User": "alice@corp"})
        self.assertEqual(len(self.store.sessions), 1)

    def test_header_from_an_untrusted_peer_is_ignored(self) -> None:
        self.link("alice", "alice@corp")
        for peer in ("203.0.113.7", "10.0.1.2", "testclient"):
            with self.subTest(peer=peer):
                browser = self.proxy(peer)
                me = browser.get("/api/", headers={"Remote-User": "alice@corp", "X-Forwarded-For": "10.0.0.2"}).json()
                self.assertFalse(me.get("authenticated"))
                self.assertFalse(self.session_cookie(browser))
        self.assertEqual(self.store.sessions, {})

    def test_header_from_an_untrusted_peer_is_stripped(self) -> None:
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"remote-user", b"root-admin"), (b"remote-groups", b"ops"),
                                                                         (b"accept", b"text/html")], "client": ("203.0.113.7", 1)}
        request = Request(scope)
        with patch.dict(os.environ, self.env):
            trusted_header.apply(request)
        self.assertEqual([k for k, _ in request.scope["headers"]], [b"accept"])
        self.assertIsNone(request.headers.get("remote-user"))

    def test_signed_in_user_cannot_become_someone_else_from_outside(self) -> None:
        bob = self.make("bob")
        self.link("eve-target", "root-admin")
        browser = self.proxy("203.0.113.7")
        self.sign_in(browser, "bob")
        me = browser.get("/api/", headers={"Remote-User": "root-admin"}).json()
        self.assertEqual(me["user_key"], bob["user_key"])

    def test_unknown_name_needs_registration(self) -> None:
        browser = self.proxy()
        me = browser.get("/api/", headers={"Remote-User": "newbie"}).json()
        self.assertFalse(me.get("authenticated"))
        page = browser.get("/auth/login", headers={"Remote-User": "newbie"})
        self.assertIn("no OpenFeed account for that name", page.text)
        self.assertEqual(len(self.store.accounts), 1)
        self.store.set_setting("registration_mode", "open")
        me = browser.get("/api/", headers={"Remote-User": "newbie", "Remote-Name": "New Person"}).json()
        self.assertTrue(me.get("authenticated"))
        made = self.store.find_identity("header:remote-user", "newbie")
        self.assertEqual((made["username"], made["display_name"]), ("newbie", "New Person"))

    def test_admin_can_let_the_proxy_create_every_account(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_TRUSTED_HEADER_CREATE_ACCOUNTS": "always"}):
            me = self.proxy().get("/api/", headers={"Remote-User": "dana@corp"}).json()
        self.assertTrue(me.get("authenticated"))
        self.assertEqual(self.store.find_identity("header:remote-user", "dana@corp")["username"], "dana")

    def test_another_name_from_the_proxy_replaces_the_session(self) -> None:
        alice = self.link("alice", "alice")
        bob = self.link("bob", "bob")
        browser = self.proxy()
        self.assertEqual(browser.get("/api/", headers={"Remote-User": "alice"}).json()["user_key"], alice["user_key"])
        self.assertEqual(browser.get("/api/", headers={"Remote-User": "bob"}).json()["user_key"], bob["user_key"])
        self.assertEqual([s["account_id"] for s in self.store.sessions.values()], [bob["id"]])

    def test_groups_header_grants_admin_by_the_methods_groups(self) -> None:
        self.link("alice", "alice")
        browser = self.proxy()
        self.assertEqual(browser.get("/admin/users", headers={"Remote-User": "alice", "Remote-Groups": "users"}).status_code, 403)
        browser = self.proxy()
        self.assertEqual(browser.get("/admin/users", headers={"Remote-User": "alice", "Remote-Groups": "users, ops"}).status_code, 200)

    def test_disabled_account(self) -> None:
        alice = self.link("alice", "alice")
        self.store.accounts[alice["id"]]["disabled"] = True
        browser = self.proxy()
        self.assertFalse(browser.get("/api/", headers={"Remote-User": "alice"}).json().get("authenticated", False))

    def test_link_and_confirm_through_the_proxy(self) -> None:
        bob = self.make("bob")
        browser = self.proxy()
        self.sign_in(browser, "bob")
        page = browser.get("/account", headers={"Remote-User": "bob@corp"})
        self.assertIn("/auth/header/link", page.text)
        done = browser.post("/auth/header/link", headers={"Remote-User": "bob@corp"}, follow_redirects=False)
        self.assertEqual(done.headers["location"], "/account?done=linked")
        self.assertEqual(self.store.find_identity("header:remote-user", "bob@corp")["id"], bob["id"])
        session = next(s for s in self.store.sessions.values() if s["account_id"] == bob["id"])
        session["reauth_at"] = time.time() - 3600
        confirm = browser.get("/auth/confirm?next=/account", headers={"Remote-User": "bob@corp"})
        self.assertIn("/auth/header/confirm", confirm.text)
        ok = browser.post("/auth/header/confirm", data={"next": "/account"}, headers={"Remote-User": "bob@corp"}, follow_redirects=False)
        self.assertEqual(ok.headers["location"], "/account")
        # From outside the proxy the same request proves nothing.
        session["reauth_at"] = time.time() - 3600
        outside = self.proxy("203.0.113.7")
        outside.cookies.set(main.get_settings().session_cookie, self.session_cookie(browser))
        refused = outside.post("/auth/header/confirm", data={"next": "/account"}, headers={"Remote-User": "bob@corp"}, follow_redirects=False)
        self.assertEqual(refused.headers["location"], "/account?error=oidc_mismatch")

    def test_sign_out_goes_to_the_proxys_sign_out(self) -> None:
        self.link("alice", "alice")
        browser = self.proxy()
        browser.get("/api/", headers={"Remote-User": "alice"})
        with patch.dict(os.environ, {"OPENFEED_TRUSTED_HEADER_LOGOUT_URL": "https://auth.corp/logout"}):
            out = browser.get("/auth/logout", headers={"Remote-User": "alice"}, follow_redirects=False)
        self.assertEqual(out.headers["location"], "https://auth.corp/logout")
        self.assertEqual(self.store.sessions, {})

    def test_without_trusted_proxies_the_header_is_never_believed(self) -> None:
        self.link("alice", "alice")
        with patch.dict(os.environ, {"OPENFEED_TRUSTED_PROXIES": ""}):
            self.assertFalse(self.proxy().get("/api/", headers={"Remote-User": "alice"}).json().get("authenticated", False))


# --- removing methods, pages ------------------------------------------------------------------


class MethodRemovalTests(_SigninBase):
    env = {**_Base.env, **LEGACY}

    def test_a_method_that_no_longer_works_does_not_count_as_a_way_in(self) -> None:
        alice = self.make("alice")
        self.store.add_identity(alice["id"], "oidc:gone", "x-1", "Gone provider")
        self.sign_in(self.client, "alice")
        password = next(i for i in self.store.list_identities(alice["id"]) if i["method"] == "password")
        refused = self.client.post(f"/account/methods/{password['id']}/remove", follow_redirects=False)
        self.assertEqual(refused.headers["location"], "/account?error=last_method")
        page = self.client.get("/account")
        self.assertIn("Not available on this server now", page.text)
        # The dead one can go.
        gone = next(i for i in self.store.list_identities(alice["id"]) if i["method"] == "oidc:gone")
        self.assertEqual(self.client.post(f"/account/methods/{gone['id']}/remove", follow_redirects=False).headers["location"],
                         "/account?done=method_removed")

    def test_link_buttons_for_each_provider(self) -> None:
        self.make("alice")
        self.sign_in(self.client, "alice")
        with patch.dict(os.environ, POCKET):
            page = self.client.get("/account")
        self.assertIn('href="/auth/oidc/authentik/start?next=%2Faccount&amp;intent=link"', page.text)
        self.assertIn("Link Pocket ID", page.text)

    def test_invite_page_offers_providers(self) -> None:
        self.make("root-admin", role="admin")
        self.store.create_link("invite", signin.token_hash("tok-1"), 3600)
        page = self.client.get("/auth/invite/tok-1")
        self.assertIn('href="/auth/oidc/authentik/start?invite=tok-1"', page.text)
        self.assertIn('name="password"', page.text)
