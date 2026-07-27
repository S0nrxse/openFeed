from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import db, main, tokens


class TokenFormatTests(unittest.TestCase):
    def test_a_generated_token_is_recognisable_and_unique(self) -> None:
        first, prefix, digest = tokens.generate()
        second, _, _ = tokens.generate()
        self.assertTrue(first.startswith("ofd_"))
        self.assertNotEqual(first, second)
        self.assertEqual(tokens.split(first), prefix)
        self.assertEqual(digest, tokens.hash_token(first))

    def test_the_plaintext_is_never_derivable_from_the_hash(self) -> None:
        plaintext, _, digest = tokens.generate()
        self.assertNotIn(plaintext.split("_")[-1], digest)
        self.assertEqual(len(digest), 64)

    def test_a_foreign_or_malformed_token_has_no_prefix(self) -> None:
        for bad in ("", "abc", "ofd_short_x", "other_aabbccddeeff_secret", "ofd_zzzzzzzzzzzz_secret",
                    "ofd_aabb_secret", "ofd_aabbccddeeff_"):
            self.assertEqual(tokens.split(bad), "", bad)

    def test_a_secret_containing_underscores_still_parses(self) -> None:
        # token_urlsafe draws from an alphabet including "_", so an unbounded split
        # rejected about half of all valid tokens.
        self.assertEqual(tokens.split("ofd_aabbccddeeff_abc_def_ghi"), "aabbccddeeff")

    def test_every_generated_token_round_trips(self) -> None:
        for _ in range(200):
            plaintext, prefix, digest = tokens.generate()
            self.assertEqual(tokens.split(plaintext), prefix)
            self.assertTrue(tokens.matches(plaintext, digest))

    def test_matching_is_exact(self) -> None:
        plaintext, _, digest = tokens.generate()
        self.assertTrue(tokens.matches(plaintext, digest))
        self.assertFalse(tokens.matches(plaintext + "x", digest))
        self.assertFalse(tokens.matches(plaintext, ""))

    def test_the_header_is_parsed_with_or_without_the_scheme(self) -> None:
        self.assertEqual(tokens.bearer_from_header("Bearer ofd_x"), "ofd_x")
        self.assertEqual(tokens.bearer_from_header("bearer  ofd_x "), "ofd_x")
        self.assertEqual(tokens.bearer_from_header("ofd_x"), "ofd_x")
        self.assertEqual(tokens.bearer_from_header(None), "")

    def test_the_public_view_never_leaks_anything_replayable(self) -> None:
        plaintext, prefix, digest = tokens.generate()
        view = tokens.public_view({"id": "abc", "name": "n", "token_prefix": prefix, "token_hash": digest})
        rendered = repr(view)
        self.assertNotIn(digest, rendered)
        self.assertNotIn(plaintext, rendered)
        self.assertEqual(view["prefix"], f"ofd_{prefix}")


class _FakeRequest:
    def __init__(self, header: str | None = None) -> None:
        self.headers = {"authorization": header} if header else {}
        self.cookies: dict[str, str] = {}


class TokenAuthTests(unittest.TestCase):
    def _record(self, plaintext: str, prefix: str, digest: str, **extra: object) -> dict:
        base = {"id": "tid", "user_key": "someone", "name": "ci", "token_prefix": prefix,
                "token_hash": digest, "read_only": False, "revoked_at": None}
        base.update(extra)
        return base

    def test_a_valid_token_authenticates_without_a_session(self) -> None:
        plaintext, prefix, digest = tokens.generate()
        with patch.object(db, "find_api_token", return_value=self._record(plaintext, prefix, digest)), \
             patch.object(db, "touch_api_token", lambda _id: None):
            user = main._current_user(_FakeRequest(f"Bearer {plaintext}"))
        self.assertTrue(user["authenticated"])
        self.assertTrue(user["via_token"])
        self.assertEqual(user["user_key"], "someone")

    def test_a_wrong_secret_with_a_real_prefix_is_refused(self) -> None:
        plaintext, prefix, digest = tokens.generate()
        forged = f"ofd_{prefix}_notthesecret"
        with patch.object(db, "find_api_token", return_value=self._record(plaintext, prefix, digest)):
            self.assertIsNone(main._user_from_token(_FakeRequest(f"Bearer {forged}")))

    def test_an_unknown_token_falls_through_rather_than_erroring(self) -> None:
        plaintext, _, _ = tokens.generate()
        with patch.object(db, "find_api_token", return_value=None):
            user = main._current_user(_FakeRequest(f"Bearer {plaintext}"))
        # Falls back to the anonymous local user, not an exception.
        self.assertFalse(user["authenticated"])

    def test_a_database_failure_does_not_break_authentication(self) -> None:
        plaintext, _, _ = tokens.generate()
        with patch.object(db, "find_api_token", side_effect=RuntimeError("db down")):
            self.assertIsNone(main._user_from_token(_FakeRequest(f"Bearer {plaintext}")))

    def test_no_header_means_no_token_user(self) -> None:
        self.assertIsNone(main._user_from_token(_FakeRequest()))

    def test_use_is_recorded(self) -> None:
        plaintext, prefix, digest = tokens.generate()
        touched: list[object] = []
        with patch.object(db, "find_api_token", return_value=self._record(plaintext, prefix, digest)), \
             patch.object(db, "touch_api_token", lambda tid: touched.append(tid)):
            main._user_from_token(_FakeRequest(f"Bearer {plaintext}"))
        self.assertEqual(touched, ["tid"])

    def test_read_only_is_carried_onto_the_user(self) -> None:
        plaintext, prefix, digest = tokens.generate()
        with patch.object(db, "find_api_token", return_value=self._record(plaintext, prefix, digest, read_only=True)), \
             patch.object(db, "touch_api_token", lambda _id: None):
            user = main._user_from_token(_FakeRequest(f"Bearer {plaintext}"))
        self.assertTrue(user["token_read_only"])


class TokenGuardTests(unittest.TestCase):
    """The middleware, not each route, is what holds these rules."""

    def test_read_only_tokens_are_blocked_from_writing(self) -> None:
        import inspect

        source = inspect.getsource(main.enforce_api_access)
        self.assertIn('user.get("token_read_only")', source)
        self.assertIn('request.method not in {"GET", "HEAD", "OPTIONS"}', source)

    def test_a_token_cannot_manage_tokens(self) -> None:
        # Otherwise a leaked token could mint replacements and outlive its revocation.
        import inspect

        source = inspect.getsource(main.enforce_api_access)
        self.assertIn('user.get("via_token") and path.startswith("/api/tokens")', source)


if __name__ == "__main__":
    unittest.main()


class RouteOrderTests(unittest.TestCase):
    """Literal segments must be declared before the parameterised route they share."""

    def _index(self, path: str) -> int:
        for position, route in enumerate(main.app.routes):
            if getattr(route, "path", None) == path:
                return position
        raise AssertionError(f"route not registered: {path}")

    def test_download_jobs_is_matched_before_the_video_id_route(self) -> None:
        # Otherwise "jobs" is captured as a youtube_video_id and the listing 502s.
        self.assertLess(self._index("/api/download/jobs"), self._index("/api/download/{youtube_video_id}"))

    def test_every_api_route_is_registered_once(self) -> None:
        seen: dict[tuple[str, str], int] = {}
        for route in main.app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/"):
                continue
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
                key = (method, path)
                seen[key] = seen.get(key, 0) + 1
        duplicates = {key: count for key, count in seen.items() if count > 1}
        self.assertEqual(duplicates, {}, f"duplicate routes shadow each other: {duplicates}")
