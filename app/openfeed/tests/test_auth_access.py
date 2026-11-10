from __future__ import annotations

import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import main


class _DummyRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _FakeUrl:
    def __init__(self, path: str) -> None:
        self.path = path
        self.query = ""


class _FakeRequest:
    def __init__(self, path: str, method: str = "GET") -> None:
        self.method = method
        self.url = _FakeUrl(path)
        self.cookies: dict[str, str] = {}
        self.headers = {"host": "localhost:8060"}


_ALLOWED = object()


def _call_guard(path: str, user: dict[str, object] | None = None, method: str = "GET"):
    """Run the access middleware, returning _ALLOWED when the request gets through."""
    async def call_next(_request: object) -> object:
        return _ALLOWED

    async def run() -> object:
        return await main.enforce_api_access(_FakeRequest(path, method), call_next)

    if user is None:
        return asyncio.run(run())
    with patch.object(main, "_current_user", return_value=user):
        return asyncio.run(run())


class AuthAccessTests(unittest.TestCase):
    def test_required_group_allows_member(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "openfeed_access"}
        user = {"authenticated": True, "groups": ["user", "openfeed_access"]}
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(main._user_has_required_access(user))

    def test_required_group_allows_admin(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "openfeed_access", "OPENFEED_ADMIN_GROUPS": "administrators"}
        user = {"authenticated": True, "groups": ["administrators"]}
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(main._user_has_required_access(user))

    def test_required_group_blocks_signed_in_user_without_group(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "openfeed_access"}
        user = {"authenticated": True, "groups": ["user"]}
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(main._user_has_required_access(user))

    def test_required_login_blocks_local_fallback(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "openfeed_access"}
        user = {"authenticated": False, "groups": []}
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(main._user_has_required_access(user))

    def test_login_redirects_to_public_host_before_oidc_state(self) -> None:
        env = {"OPENFEED_PUBLIC_URL": "https://openfeed.example.com"}
        with patch.dict(os.environ, env, clear=False):
            response = main._canonical_login_redirect(_DummyRequest({"host": "localhost:8060"}), "/watch/abc?from=home")
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "https://openfeed.example.com/auth/login?next=/watch/abc%3Ffrom%3Dhome")

    def test_login_does_not_redirect_when_already_on_public_host(self) -> None:
        env = {"OPENFEED_PUBLIC_URL": "https://openfeed.example.com"}
        with patch.dict(os.environ, env, clear=False):
            response = main._canonical_login_redirect(_DummyRequest({"host": "openfeed.example.com"}), "/")
        self.assertIsNone(response)

    def test_bare_callback_restarts_login(self) -> None:
        env = {"OPENFEED_AUTH_MODE": "authentik_oidc"}
        with patch.dict(os.environ, env, clear=False):
            response = main.auth_callback(_DummyRequest({"host": "openfeed.example.com"}))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next=/")


class ApiAccessGuardTests(unittest.TestCase):
    """The JSON API used to be reachable even when the pages required a login."""

    def test_default_install_is_untouched(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(_call_guard("/api/subscriptions"), _ALLOWED)
            self.assertIs(_call_guard("/admin/status"), _ALLOWED)

    def test_required_login_blocks_anonymous_api_calls(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "true"}, clear=True):
            response = _call_guard("/api/subscriptions")
        self.assertEqual(response.status_code, 401)

    def test_required_login_blocks_anonymous_unsubscribe(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "true"}, clear=True):
            response = _call_guard("/api/subscriptions", method="DELETE")
        self.assertEqual(response.status_code, 401)

    def test_signed_in_user_without_the_group_is_refused(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "openfeed_access"}
        user = {"authenticated": True, "groups": ["someone-else"]}
        with patch.dict(os.environ, env, clear=True):
            response = _call_guard("/api/videos", user)
        self.assertEqual(response.status_code, 403)

    def test_member_of_the_group_gets_through(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "openfeed_access"}
        user = {"authenticated": True, "groups": ["openfeed_access"]}
        with patch.dict(os.environ, env, clear=True):
            self.assertIs(_call_guard("/api/videos", user), _ALLOWED)

    def test_status_is_admin_only_once_admin_groups_are_configured(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_ADMIN_GROUPS": "operators"}
        # Real sessions now carry the iat they were issued at; admin rights reach their
        # expiry window from that snapshot.
        now = int(time.time())
        member = {"authenticated": True, "groups": ["operators"], "iat": now}
        outsider = {"authenticated": True, "groups": ["viewers"], "iat": now}
        stale = {"authenticated": True, "groups": ["operators"], "iat": now - 100000}
        with patch.dict(os.environ, env, clear=True):
            self.assertIs(_call_guard("/api/status/full", member), _ALLOWED)
            self.assertEqual(_call_guard("/api/status/full", outsider).status_code, 403)
            self.assertEqual(_call_guard("/admin/status", outsider).status_code, 403)

    def test_admin_status_panel_is_admin_only_too(self) -> None:
        # /api/status/panel used to slip through the admin gate even while
        # /api/status/full was covered.
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_ADMIN_GROUPS": "operators"}
        outsider = {"authenticated": True, "groups": ["viewers"], "iat": int(time.time())}
        member = {"authenticated": True, "groups": ["operators"], "iat": int(time.time())}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_call_guard("/api/status/panel", outsider).status_code, 403)
            self.assertIs(_call_guard("/api/status/panel", member), _ALLOWED)

    def test_admin_rights_expire_without_a_fresh_login(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_ADMIN_GROUPS": "operators"}
        stale = {"authenticated": True, "groups": ["operators"], "iat": int(time.time()) - 100000}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_call_guard("/api/status/full", stale).status_code, 403)

    def test_admin_gate_can_be_disabled_with_zero_retention(self) -> None:
        env = {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_ADMIN_GROUPS": "operators", "OPENFEED_ADMIN_SESSION_SECONDS": "0"}
        stale = {"authenticated": True, "groups": ["operators"], "iat": int(time.time()) - 100000}
        with patch.dict(os.environ, env, clear=True):
            self.assertIs(_call_guard("/api/status/full", stale), _ALLOWED)

    def test_admin_groups_without_a_login_do_not_lock_the_owner_out(self) -> None:
        # No login means no identity to test against, so the gate has to stay open.
        with patch.dict(os.environ, {"OPENFEED_ADMIN_GROUPS": "operators"}, clear=True):
            self.assertIs(_call_guard("/admin/status"), _ALLOWED)
            self.assertIs(_call_guard("/api/status/full"), _ALLOWED)

    def test_invidious_proxies_are_covered_too(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "true"}, clear=True):
            self.assertEqual(_call_guard("/api/v1/storyboards/abc").status_code, 401)

    def test_pages_are_left_to_their_own_guard(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "true"}, clear=True):
            self.assertIs(_call_guard("/watch/abc"), _ALLOWED)
            self.assertIs(_call_guard("/healthz"), _ALLOWED)


if __name__ == "__main__":
    unittest.main()


class AuthErrorPageTests(unittest.TestCase):
    """These answer when the shell may be what is broken, so they stand on their own."""

    def _html(self, *args, **kwargs) -> str:
        return main._auth_error_page(*args, **kwargs).body.decode()

    def test_the_page_needs_nothing_from_the_shell(self) -> None:
        html = self._html("Title", "Cause")
        self.assertNotIn('rel="stylesheet"', html)
        self.assertNotIn("/assets/shell/", html)
        self.assertIn("<style>", html)

    def test_inlined_tokens_match_the_stylesheet(self) -> None:
        import re
        from pathlib import Path

        tokens = (Path(main.__file__).parent / "assets" / "css" / "00-tokens.css").read_text(encoding="utf-8")
        inlined = main._AUTH_PAGE_CSS[main._AUTH_PAGE_CSS.index("--bg:"):main._AUTH_PAGE_CSS.index("}")]
        pairs = re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", inlined)
        self.assertGreater(len(pairs), 10)
        for name, value in pairs:
            self.assertIn(f"{name}: {value};", tokens)

    def test_order_is_cause_then_missing_then_fix_then_actions(self) -> None:
        html = self._html("Title", "The cause", ["OPENFEED_A or OPENFEED_B"], fix="The fix")
        body = html[html.index("<main>"):]
        positions = [body.index(text) for text in ("The cause", "Missing settings", "The fix", 'class="actions"')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('data-copy="OPENFEED_A"', html)

    def test_copy_stays_hidden_without_script(self) -> None:
        html = self._html("Title", "Cause", ["OPENFEED_A"])
        self.assertIn(" hidden>Copy</button>", html)
        self.assertIn("[hidden] { display: none !important; }", html)

    def test_no_link_is_red_chrome(self) -> None:
        self.assertNotIn("a { color", main._AUTH_PAGE_CSS)
        self.assertNotIn("a{color", main._AUTH_PAGE_CSS)

    def test_interpolated_values_are_escaped(self) -> None:
        html = self._html("<t>", "<d>", ["<m>"], fix="<f>", account="<a>")
        for raw in ("<t>", "<d>", "<m>", "<f>", "<a>"):
            self.assertNotIn(raw, html.split("</style>")[1])

    def test_a_denied_account_is_named(self) -> None:
        response = main._access_denied_response({"authenticated": True, "email": "someone@example.test"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("someone@example.test", response.body.decode())
        self.assertIn("/auth/logout", response.body.decode())

    def test_an_admin_page_denial_does_not_blame_the_access_group(self) -> None:
        with patch.object(main, "get_settings", return_value=SimpleNamespace(required_group="members")):
            html = main._access_denied_response({"authenticated": True}, admin_only=True).body.decode()
        self.assertNotIn("members", html)
        self.assertIn("administrators", html)

    def test_discovery_codes_read_as_sentences(self) -> None:
        html = self._html("Title", "oidc_discovery_unavailable")
        self.assertNotIn("oidc_discovery_unavailable", html)
