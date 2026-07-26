from __future__ import annotations

import asyncio
import os
import unittest
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
    def __init__(self, path: str) -> None:
        self.url = _FakeUrl(path)
        self.cookies: dict[str, str] = {}
        self.headers = {"host": "localhost:8060"}


_ALLOWED = object()


def _call_guard(path: str, user: dict[str, object] | None = None):
    """Run the access middleware, returning _ALLOWED when the request gets through."""
    async def call_next(_request: object) -> object:
        return _ALLOWED

    async def run() -> object:
        return await main.enforce_api_access(_FakeRequest(path), call_next)

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
            response = _call_guard("/api/subscriptions/freshrss")
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
        member = {"authenticated": True, "groups": ["operators"]}
        outsider = {"authenticated": True, "groups": ["viewers"]}
        with patch.dict(os.environ, env, clear=True):
            self.assertIs(_call_guard("/api/status/full", member), _ALLOWED)
            self.assertEqual(_call_guard("/api/status/full", outsider).status_code, 403)
            self.assertEqual(_call_guard("/admin/status", outsider).status_code, 403)

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
