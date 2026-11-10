"""Passwords, two-step codes, signed payloads, device labels, request safety and rate limits.

Pure functions; no database, no Redis (the limiter's Redis client is made to fail, which is
also the fallback path under test).
"""

from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pyotp

from openfeed import accounts, passwords, ratelimit, signin, twofactor
from openfeed.config import get_settings


class PasswordTests(unittest.TestCase):
    def test_hash_is_argon2id_and_verifies(self) -> None:
        stored = passwords.hash_password("correct horse battery")
        self.assertTrue(stored.startswith("$argon2id$"))
        self.assertTrue(passwords.verify(stored, "correct horse battery"))

    def test_wrong_password_and_missing_hash_fail(self) -> None:
        stored = passwords.hash_password("correct horse battery")
        self.assertFalse(passwords.verify(stored, "correct horse batterY"))
        self.assertFalse(passwords.verify(stored, ""))
        self.assertFalse(passwords.verify(None, "correct horse battery"))
        self.assertFalse(passwords.verify("not-a-hash", "anything at all"))

    def test_unknown_account_still_spends_a_hash(self) -> None:
        # A missing account must cost the same as a wrong password, or timing says who exists.
        with patch.object(passwords, "_hasher", wraps=passwords._hasher) as hasher:
            passwords.verify(None, "whatever you like")
        hasher.verify.assert_called_once()

    def test_unicode_forms_are_the_same_password(self) -> None:
        stored = passwords.hash_password("café au lait please")
        self.assertTrue(passwords.verify(stored, "café au lait please"))

    def test_old_parameters_ask_for_a_rehash(self) -> None:
        from argon2 import PasswordHasher

        weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("correct horse battery")
        self.assertTrue(passwords.needs_rehash(weak))
        self.assertFalse(passwords.needs_rehash(passwords.hash_password("correct horse battery")))
        self.assertTrue(passwords.needs_rehash("garbage"))

    def test_policy(self) -> None:
        cases = {
            "short": "password_too_short",
            "x" * (passwords.MAX_LENGTH + 1): "password_too_long",
            "password123": "password_common",
            "QwertyUiop": "password_common",
            "aaaaaaaaaaaa": "password_common",
            "peyton-smith": "password_like_name",
        }
        for value, code in cases.items():
            with self.subTest(value=value[:20]), self.assertRaises(passwords.PasswordPolicyError) as raised:
                passwords.check_policy(value, username="peyton-smith")
            self.assertEqual(raised.exception.code, code)
        # No composition rules: long lowercase words are fine.
        self.assertEqual(passwords.check_policy("tangerine bicycle orbit"), "tangerine bicycle orbit")


class TwoStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = twofactor.new_secret()
        self.totp = pyotp.TOTP(self.secret)
        self.now = 1_800_000_015.0

    def test_current_and_one_step_of_skew_match(self) -> None:
        step = int(self.now // 30)
        for offset in (-1, 0, 1):
            code = self.totp.at(self.now + offset * 30)
            self.assertEqual(twofactor.matching_step(self.secret, code, now=self.now), step + offset)

    def test_codes_outside_the_skew_or_malformed_fail(self) -> None:
        for offset in (-3, -2, 2, 3):
            self.assertIsNone(twofactor.matching_step(self.secret, self.totp.at(self.now + offset * 30), now=self.now))
        for bad in ("", "12345", "1234567", "abcdef"):
            self.assertIsNone(twofactor.matching_step(self.secret, bad, now=self.now))
        self.assertIsNone(twofactor.matching_step("", self.totp.at(self.now), now=self.now))

    def test_spaces_in_a_typed_code_are_fine(self) -> None:
        code = self.totp.at(self.now)
        self.assertIsNotNone(twofactor.matching_step(self.secret, f"{code[:3]} {code[3:]}", now=self.now))

    def test_qr_is_local_svg_with_nothing_external(self) -> None:
        svg = twofactor.qr_svg(twofactor.provisioning_uri(self.secret, "alice"))
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("<image", svg)
        self.assertNotIn("href", svg)
        # Only the SVG namespace may mention a URL.
        self.assertEqual(svg.count("http"), 1)
        self.assertIn("http://www.w3.org/2000/svg", svg)

    def test_provisioning_uri_carries_issuer_and_secret(self) -> None:
        uri = twofactor.provisioning_uri(self.secret, "alice", issuer="OpenFeed")
        self.assertTrue(uri.startswith("otpauth://totp/OpenFeed:alice?"))
        self.assertIn(f"secret={self.secret}", uri)

    def test_recovery_codes(self) -> None:
        codes = twofactor.new_recovery_codes()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.assertRegex(code, r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{2}$")
        key = b"k1"
        first = codes[0]
        self.assertEqual(twofactor.hash_recovery_code(first, key), twofactor.hash_recovery_code(first.upper().replace("-", " "), key))
        self.assertNotEqual(twofactor.hash_recovery_code(first, key), twofactor.hash_recovery_code(first, b"k2"))
        self.assertNotIn(twofactor.normalize_recovery_code(first), twofactor.hash_recovery_code(first, key))


class SignedPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_SECRET_KEY": "unit-test-secret"}, clear=True))

    def test_round_trip(self) -> None:
        value = signin.sign("pending", {"aid": "a", "exp": int(time.time()) + 60})
        self.assertEqual(signin.unsign("pending", value)["aid"], "a")

    def test_tampered_expired_or_repurposed_payloads_fail(self) -> None:
        good = signin.sign("pending", {"aid": "a", "exp": int(time.time()) + 60})
        body, mac = good.rsplit(".", 1)
        forged = signin.sign("pending", {"aid": "b", "exp": int(time.time()) + 60}).rsplit(".", 1)[0] + "." + mac
        self.assertIsNone(signin.unsign("pending", forged))
        self.assertIsNone(signin.unsign("other", good))
        self.assertIsNone(signin.unsign("pending", signin.sign("pending", {"aid": "a", "exp": int(time.time()) - 1})))
        self.assertIsNone(signin.unsign("pending", "garbage"))
        self.assertIsNone(signin.unsign("pending", None))

    def test_without_a_secret_nothing_verifies(self) -> None:
        value = signin.sign("pending", {"aid": "a", "exp": int(time.time()) + 60})
        with patch.dict(os.environ, {"OPENFEED_SECRET_KEY": ""}, clear=True):
            self.assertIsNone(signin.unsign("pending", value))


class HelperTests(unittest.TestCase):
    def test_device_labels_are_coarse(self) -> None:
        cases = {
            "Mozilla/5.0 (Android 14; Mobile; rv:128.0) Gecko/128.0 Firefox/128.0": "Firefox on Android",
            "Mozilla/5.0 (Linux; Android 14; Pixel 8; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/129.0 Mobile Safari/537.36": "OpenFeed app on Android",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15": "Safari on macOS",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0": "Edge on Windows",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/130.0 Mobile/15E148 Safari/604.1": "Chrome on iPhone",
            "curl/8.5.0": "Script",
            "": "Unknown device",
        }
        for ua, label in cases.items():
            with self.subTest(ua=ua[:30]):
                self.assertEqual(signin.device_label(ua), label)

    def test_next_paths_stay_on_site_and_out_of_sign_in(self) -> None:
        self.assertEqual(signin.safe_next("/watch/abc?t=1"), "/watch/abc?t=1")
        self.assertEqual(signin.safe_next("/setup/data"), "/setup/data")
        for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)", "/auth/login", "/auth/2fa", "/setup"):
            self.assertEqual(signin.safe_next(bad), "/", bad)

    def test_usernames(self) -> None:
        self.assertEqual(accounts.clean_username("  Alice.B  "), "alice.b")
        for bad in ("a", "-alice", "al ice", "x" * 40, "local", "<script>", ""):
            with self.subTest(bad=bad), self.assertRaises(accounts.AccountError):
                accounts.clean_username(bad)
        self.assertEqual(accounts.clean_display_name("  Alice ​  Smith ", "x"), "Alice Smith")
        self.assertEqual(accounts.clean_display_name("", "fallback"), "fallback")


def _request(method: str = "POST", path: str = "/api/playlists", headers: dict[str, str] | None = None, cookie: str = ""):
    base = {"host": "openfeed.test"}
    base.update(headers or {})
    return SimpleNamespace(
        method=method, url=SimpleNamespace(path=path), headers=base,
        cookies={get_settings().session_cookie: cookie} if cookie else {},
    )


class CsrfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_PUBLIC_URL": "https://openfeed.test"}, clear=True))

    def test_session_writes_need_a_same_site_origin_or_referer(self) -> None:
        cookie = "v2.sometoken"
        self.assertTrue(signin.csrf_failure(_request(cookie=cookie)))
        self.assertTrue(signin.csrf_failure(_request(cookie=cookie, headers={"origin": "https://evil.example"})))
        self.assertTrue(signin.csrf_failure(_request(cookie=cookie, headers={"origin": "null"})))
        self.assertTrue(signin.csrf_failure(_request(cookie=cookie, headers={"referer": "https://evil.example/openfeed.test"})))
        self.assertFalse(signin.csrf_failure(_request(cookie=cookie, headers={"origin": "https://openfeed.test"})))
        self.assertFalse(signin.csrf_failure(_request(cookie=cookie, headers={"referer": "https://openfeed.test/account"})))

    def test_sign_in_forms_are_checked_without_a_cookie(self) -> None:
        for path in ("/auth/login", "/setup", "/auth/2fa", "/auth/invite/x", "/account/delete", "/admin/users/x/role"):
            with self.subTest(path=path):
                self.assertTrue(signin.csrf_failure(_request(path=path)))
                self.assertFalse(signin.csrf_failure(_request(path=path, headers={"origin": "https://openfeed.test"})))

    def test_reads_scripts_and_old_cookies_are_untouched(self) -> None:
        self.assertFalse(signin.csrf_failure(_request(method="GET", cookie="v2.x")))
        self.assertFalse(signin.csrf_failure(_request()))  # a token script: no cookie
        self.assertFalse(signin.csrf_failure(_request(cookie="eyJsegacy.sig")))


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_SECRET_KEY": "k"}, clear=True))
        ratelimit.reset_client()
        self.addCleanup(ratelimit.reset_client)
        # No Redis: every call falls back to this process's counters.
        self.enterContext(patch.object(ratelimit, "_redis", side_effect=ConnectionError("no redis")))

    def test_blocks_after_the_limit_and_clears(self) -> None:
        rule = ratelimit.Rule("t", 3, 60)
        key = ratelimit.account_key(rule, "acct")
        for _ in range(3):
            self.assertFalse(ratelimit.blocked(key, rule))
            ratelimit.hit(key, rule.window)
        self.assertTrue(ratelimit.blocked(key, rule))
        ratelimit.clear(key)
        self.assertFalse(ratelimit.blocked(key, rule))

    def test_window_expires(self) -> None:
        rule = ratelimit.Rule("t", 1, 60)
        key = ratelimit.account_key(rule, "acct")
        ratelimit.hit(key, rule.window)
        self.assertTrue(ratelimit.blocked(key, rule))
        with patch.object(ratelimit.time, "time", return_value=time.time() + 61):
            self.assertFalse(ratelimit.blocked(key, rule))

    def test_client_keys_never_contain_the_address(self) -> None:
        request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"), headers={})
        key = ratelimit.client_key(ratelimit.SIGNIN_CLIENT, request)
        self.assertNotIn("203.0.113.7", key)
        self.assertNotIn("203", key.split(":")[-1][:3] + "x")
        with patch.dict(os.environ, {"OPENFEED_SECRET_KEY": "other"}):
            self.assertNotEqual(ratelimit.client_key(ratelimit.SIGNIN_CLIENT, request), key)

    def test_forwarded_for_is_believed_only_from_trusted_proxies(self) -> None:
        spoofed = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"), headers={"x-forwarded-for": "198.51.100.1"})
        self.assertEqual(ratelimit.client_ip(spoofed), "203.0.113.7")
        with patch.dict(os.environ, {"OPENFEED_TRUSTED_PROXIES": "10.0.0.0/8"}):
            proxied = SimpleNamespace(client=SimpleNamespace(host="10.0.0.2"), headers={"x-forwarded-for": "198.51.100.9, 198.51.100.1, 10.0.0.3"})
            self.assertEqual(ratelimit.client_ip(proxied), "198.51.100.1")
            self.assertEqual(ratelimit.client_ip(spoofed), "203.0.113.7")

    def test_remember_once(self) -> None:
        self.assertTrue(ratelimit.remember_once("of:test:once", 60))
        self.assertFalse(ratelimit.remember_once("of:test:once", 60))


class ModeSettingsTests(unittest.TestCase):
    def test_accounts_mode_always_requires_sign_in(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "accounts", "OPENFEED_REQUIRE_LOGIN": "false"}, clear=True):
            settings = get_settings()
            self.assertTrue(settings.require_login)
            self.assertTrue(settings.password_signin_enabled)
            self.assertTrue(settings.sign_in_enabled)

    def test_authentik_mode_keeps_its_old_defaults(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "authentik_oidc"}, clear=True):
            settings = get_settings()
            self.assertFalse(settings.require_login)
            self.assertFalse(settings.password_signin_enabled)
        with patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "authentik_oidc", "OPENFEED_PASSWORD_SIGNIN": "true"}, clear=True):
            self.assertTrue(get_settings().password_signin_enabled)

    def test_disabled_mode_has_no_sign_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = get_settings()
            self.assertFalse(settings.sign_in_enabled)
            self.assertFalse(settings.password_signin_enabled)
            self.assertFalse(settings.require_login)


class RegistryTests(unittest.TestCase):
    def test_built_in_methods_are_registered(self) -> None:
        self.assertIs(signin.method("password"), signin.PASSWORD)
        self.assertIs(signin.method("oidc:authentik"), signin.OIDC)
        self.assertIsNone(signin.method("nothing"))

    def test_a_new_method_plugs_in(self) -> None:
        class Fake(signin.SignInMethod):
            def available(self) -> bool:
                return True

            def entry(self, next_path: str):
                return signin.MethodEntry(self.key, "Use a passkey", f"/auth/passkey?next={next_path}", "passkey")

        real = signin._REGISTRY.get("passkey")
        item = signin.register(Fake(key="passkey", kind="passkey", label="Passkey", both_factors=True))
        self.addCleanup(lambda: signin.register(real) if real else signin._REGISTRY.pop("passkey", None))
        self.assertIn(item, signin.available_methods())
        self.assertEqual(signin.labels_for(["password", "passkey"]), ["Password", "Passkey"])

    def test_a_broken_method_does_not_break_the_list(self) -> None:
        class Broken(signin.SignInMethod):
            def available(self) -> bool:
                raise RuntimeError("boom")

        signin.register(Broken(key="broken", kind="oidc", label="Broken"))
        self.addCleanup(signin._REGISTRY.pop, "broken", None)
        with self.assertLogs("openfeed.auth", level="ERROR"):
            self.assertNotIn("broken", [m.key for m in signin.available_methods()])


if __name__ == "__main__":
    unittest.main()
