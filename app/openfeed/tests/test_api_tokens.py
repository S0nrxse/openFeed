from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from openfeed import db, downloads, main, tokens


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
    """Exercise real middleware and routes, with no startup or external services."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {
            "OPENFEED_REQUIRE_LOGIN": "true",
            "OPENFEED_REQUIRED_GROUP": "noah_perm_myyt_access",
            "OPENFEED_ADMIN_GROUPS": "operators",
            "OPENFEED_SESSION_SECRET": "test-only-session-signing-key",
        }, clear=True))
        # Any unmocked database access is a test failure, never a live connection.
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        self.plaintext, prefix, digest = tokens.generate()
        self.record = {
            "id": "00000000-0000-0000-0000-000000000001", "user_key": "actor-1",
            "name": "test", "token_prefix": prefix, "token_hash": digest,
            "read_only": False, "revoked_at": None,
        }
        self.lookup = self.enterContext(patch.object(db, "find_api_token", return_value=self.record))
        self.touch = self.enterContext(patch.object(db, "touch_api_token"))
        # Not a context manager: TestClient must not run application lifespan.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.client.headers["authorization"] = f"Bearer {self.plaintext}"

    def browser(self, groups: list[str]) -> None:
        self.client.headers.pop("authorization", None)
        self.client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
            "sub": "browser-actor", "groups": groups,
            "expires_at": main._utc_timestamp() + 600,
        }))

    def test_existing_token_delegates_standard_access_and_preserves_owner(self) -> None:
        preferences = self.enterContext(patch.object(db, "get_feed_preferences", return_value={}))
        playlists = self.enterContext(patch.object(db, "list_playlists", return_value=[]))
        progress = self.enterContext(patch.object(db, "get_playback_progress", return_value=None))
        for path in ("/api/", "/api/feed/preferences", "/api/playlists", "/api/playback/progress/test-video"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        index = self.client.get("/api/").json()
        self.assertTrue(index["authenticated"])
        self.assertTrue(index["via_token"])
        self.assertFalse(index["is_admin"])
        self.assertEqual(index["user_key"], "actor-1")
        preferences.assert_called_once_with("actor-1")
        playlists.assert_called_once_with("actor-1")
        progress.assert_called_once_with("test-video", "actor-1")

    def test_writable_token_can_mutate_as_its_owner(self) -> None:
        with patch.object(db, "create_playlist", return_value={"id": "test-list"}) as create:
            response = self.client.post("/api/playlists", json={"name": "Test", "user_key": "other-actor"})
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with("Test", None, "actor-1")

    def test_read_only_is_enforced_before_any_write_handler(self) -> None:
        self.record["read_only"] = True
        with patch.object(db, "create_playlist") as create:
            for admin in (False, True):
                self.record["delegated_admin"] = admin
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    for path in ("/api/playlists", "/api/future-write-route", "/admin/future-write-route"):
                        with self.subTest(admin=admin, method=method, path=path):
                            response = self.client.request(method, path, json={"name": "Test"})
                            self.assertEqual(response.status_code, 403)
                            self.assertEqual(response.json()["detail"], "read_only_token")
            create.assert_not_called()
        self.assertTrue(self.client.get("/api/").json()["read_only"])
        # Unsupported safe methods reach routing (405), rather than the write guard.
        for method in ("HEAD", "OPTIONS"):
            self.assertEqual(self.client.request(method, "/api/").status_code, 405)

    def test_admin_gets_require_a_true_persisted_claim(self) -> None:
        full = self.enterContext(patch.object(main, "full_status_payload", return_value={"ok": True}))
        runs = self.enterContext(patch.object(db, "list_sync_runs", return_value=[]))
        for claim in (None, False, "true", "false", 1, [], True):
            self.record["delegated_admin"] = claim
            with self.subTest(claim=claim):
                self.assertIs(self.client.get("/api/").json()["is_admin"], claim is True)
                for path in ("/api/status/full", "/api/sync/runs"):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200 if claim is True else 403)
                    if claim is not True:
                        self.assertEqual(response.json()["detail"], "admin_required")
        full.assert_called_once()
        runs.assert_called_once_with(20)
        self.record["read_only"] = True
        for path in ("/api/status/full", "/api/sync/runs"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_legacy_token_never_inherits_single_user_admin_fallback(self) -> None:
        for env in ({"OPENFEED_REQUIRE_LOGIN": "false"}, {"OPENFEED_ADMIN_GROUPS": ""}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.assertFalse(self.client.get("/api/").json()["is_admin"])
                self.assertEqual(self.client.get("/api/status/full").status_code, 403)

    def test_header_claims_and_browser_cookie_cannot_elevate_a_token(self) -> None:
        self.browser(["operators"])
        self.client.headers.update({
            "authorization": f"Bearer {self.plaintext}",
            "x-authentik-groups": "operators,noah_perm_myyt_access",
            "x-forwarded-groups": "operators", "x-auth-request-groups": "operators",
            "x-user-key": "other-actor", "x-delegated-admin": "true",
        })
        # Even a groups-shaped legacy record is not an admin delegation.
        self.record["groups"] = ["operators"]
        index = self.client.get("/api/").json()
        self.assertEqual(index["user_key"], "actor-1")
        self.assertFalse(index["is_admin"])
        for path in ("/api/status/full", "/api/sync/runs"):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_tokens_cannot_manage_tokens_even_with_admin_and_browser_cookie(self) -> None:
        self.browser(["operators"])
        self.client.headers["authorization"] = f"Bearer {self.plaintext}"
        create = self.enterContext(patch.object(db, "create_api_token"))
        revoke = self.enterContext(patch.object(db, "revoke_api_token"))
        listing = self.enterContext(patch.object(db, "list_api_tokens"))
        for admin in (False, True):
            for read_only in (False, True):
                self.record.update(delegated_admin=admin, read_only=read_only)
                for method, path in (("GET", "/api/tokens"), ("POST", "/api/tokens"),
                                     ("DELETE", f"/api/tokens/{self.record['id']}")):
                    with self.subTest(admin=admin, read_only=read_only, method=method):
                        response = self.client.request(method, path, json={"name": "replacement"})
                        self.assertEqual(response.status_code, 403)
        create.assert_not_called()
        revoke.assert_not_called()
        listing.assert_not_called()

    def test_mint_snapshots_browser_capability_and_owner_not_payload(self) -> None:
        for groups, admin in ((["noah_perm_myyt_access"], False), (["operators"], True)):
            with self.subTest(admin=admin):
                self.browser(groups)
                self.client.headers["x-authentik-groups"] = "operators"
                with patch.object(db, "create_api_token", return_value=self.record) as create:
                    response = self.client.post("/api/tokens", json={
                        "name": "Actor integration", "read_only": True,
                        "user_key": "other-actor", "groups": [] if admin else ["operators"],
                        "delegated_admin": not admin, "is_admin": not admin,
                    })
                self.assertEqual(response.status_code, 201)
                args = create.call_args.args
                self.assertEqual((args[0], args[3], args[4]), ("Actor integration", True, "browser-actor"))
                self.assertEqual(create.call_args.kwargs, {"delegated_admin": admin})
                self.assertTrue(tokens.matches(response.json()["token"], args[2]))

    def test_unauthorized_browser_cannot_mint_by_injecting_claims(self) -> None:
        self.browser(["viewers"])
        self.client.headers["x-authentik-groups"] = "operators,noah_perm_myyt_access"
        with patch.object(db, "create_api_token") as create, patch.object(tokens, "generate") as generate:
            response = self.client.post("/api/tokens", json={"groups": ["operators"], "delegated_admin": True})
        self.assertEqual(response.status_code, 403)
        create.assert_not_called()
        generate.assert_not_called()

    def test_anonymous_cannot_manage_tokens_even_when_login_is_optional(self) -> None:
        self.client.headers.pop("authorization")
        with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "false"}):
            for method, path in (("GET", "/api/tokens"), ("POST", "/api/tokens"),
                                 ("DELETE", f"/api/tokens/{self.record['id']}")):
                self.assertEqual(self.client.request(method, path, json={}).status_code, 401)

    def test_browser_lists_and_revokes_only_its_own_credentials(self) -> None:
        self.browser(["noah_perm_myyt_access"])
        with patch.object(db, "list_api_tokens", return_value=[]) as listing, \
             patch.object(db, "revoke_api_token", return_value=self.record) as revoke:
            self.assertEqual(self.client.get("/api/tokens").status_code, 200)
            self.assertEqual(self.client.delete(f"/api/tokens/{self.record['id']}").status_code, 200)
        listing.assert_called_once_with("browser-actor", False)
        revoke.assert_called_once_with(self.record["id"], "browser-actor")

    def test_token_responses_are_not_cacheable(self) -> None:
        self.browser(["noah_perm_myyt_access"])
        with patch.object(db, "list_api_tokens", return_value=[self.record]), \
             patch.object(db, "create_api_token", return_value=self.record), \
             patch.object(db, "revoke_api_token", return_value=self.record):
            for method, path, body in (
                ("GET", "/api/tokens", None),
                ("POST", "/api/tokens", {"name": "NOAH", "read_only": True}),
                ("DELETE", f"/api/tokens/{self.record['id']}", None),
            ):
                response = self.client.request(method, path, json=body)
                self.assertIn(response.status_code, (200, 201))
                self.assertEqual(response.headers["cache-control"], "private, no-store")
                self.assertEqual(response.headers["pragma"], "no-cache")

    def test_invalid_and_revoked_tokens_never_use_public_or_cookie_fallback(self) -> None:
        self.browser(["operators"])
        health = self.enterContext(patch.object(db, "db_status", return_value={"ok": True}))
        self.enterContext(patch.object(main, "_redis_status", return_value={"ok": True}))
        invalid_records = (None, {**self.record, "token_hash": "wrong"},
                           {**self.record, "revoked_at": "revoked", "delegated_admin": True})
        for require_login in ("true", "false"):
            with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": require_login}):
                for record in invalid_records:
                    self.lookup.return_value = record
                    self.client.headers["authorization"] = f"Bearer {self.plaintext}"
                    for path in ("/api/", "/api/playlists", "/api/status/full", "/healthz"):
                        with self.subTest(login=require_login, path=path):
                            response = self.client.get(path)
                            self.assertEqual(response.status_code, 401)
                            self.assertEqual(response.json()["detail"], "invalid_token")
        self.client.headers["authorization"] = "Bearer malformed"
        self.assertEqual(self.client.get("/healthz").status_code, 401)
        self.touch.assert_not_called()
        health.assert_not_called()
        self.client.headers.pop("authorization")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_token_database_failure_fails_closed_on_public_health(self) -> None:
        self.lookup.side_effect = RuntimeError("test database unavailable")
        self.assertEqual(self.client.get("/healthz").status_code, 401)

    def test_download_job_owner_can_poll_cancel_and_fetch_file_with_browser_or_token(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "job.mp4"
        path.write_bytes(b"test-only-video-content")
        self.enterContext(patch.dict(os.environ, {"OPENFEED_DOWNLOAD_DIR": directory}))
        for identity, owner in (("token", "actor-1"), ("browser", "browser-actor"), ("local", "local")):
            with self.subTest(identity=identity), patch.dict(os.environ, {
                "OPENFEED_REQUIRE_LOGIN": "false" if identity == "local" else "true",
            }):
                self.client.cookies.clear()
                if identity == "browser":
                    self.browser(["noah_perm_myyt_access"])
                elif identity == "local":
                    self.client.headers.pop("authorization", None)
                else:
                    self.client.headers["authorization"] = f"Bearer {self.plaintext}"
                job = downloads.DownloadJob(
                    id="owned-job", youtube_video_id="test-video", height=720,
                    container="mp4", user_key=owner, status="done", path=str(path), filename="job.mp4",
                )
                with patch.object(downloads.manager, "get", return_value=job), \
                     patch.object(downloads.manager, "cancel", return_value=True) as cancel:
                    response = self.client.get("/api/download/jobs/owned-job")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True, "job": job.public()})
                    response = self.client.get("/api/download/jobs/owned-job/file")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"test-only-video-content")
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertIn("job.mp4", response.headers["content-disposition"])
                    response = self.client.delete("/api/download/jobs/owned-job")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True, "cancelled": True})
                    cancel.assert_called_once_with("owned-job")

    def test_download_job_listing_filters_internal_objects_before_public_projection(self) -> None:
        manager = downloads.DownloadManager()
        for index, owner in enumerate(("actor-1", "actor-2", "browser-actor", "local", "actor-1")):
            job = downloads.DownloadJob(
                id=f"job-{index}", youtube_video_id=f"video-{index}", height=720,
                container="mp4", user_key=owner, title=f"private-title-{index}", created_at=index,
            )
            manager._jobs[job.id] = job
        # These have no public() method: projecting any of them must fail the test.
        manager._jobs["legacy-missing"] = SimpleNamespace(created_at=10)
        manager._jobs["legacy-null"] = SimpleNamespace(created_at=11, user_key=None)
        manager._jobs["legacy-empty"] = SimpleNamespace(created_at=12, user_key="")
        with patch.object(downloads, "manager", manager):
            for identity, owner, expected_ids in (
                ("token", "actor-1", ["job-4", "job-0"]),
                ("other-token", "actor-2", ["job-1"]),
                ("admin-token", "actor-1", ["job-4", "job-0"]),
                ("readonly-token", "actor-1", ["job-4", "job-0"]),
                ("browser", "browser-actor", ["job-2"]),
                ("local", "local", ["job-3"]),
            ):
                with self.subTest(identity=identity), patch.dict(os.environ, {
                    "OPENFEED_REQUIRE_LOGIN": "false" if identity == "local" else "true",
                }):
                    self.client.cookies.clear()
                    self.record.update(user_key=owner, delegated_admin=identity == "admin-token",
                                       read_only=identity == "readonly-token")
                    if identity == "browser":
                        self.browser(["operators"])
                    elif identity == "local":
                        self.client.headers.pop("authorization", None)
                    else:
                        self.client.headers["authorization"] = f"Bearer {self.plaintext}"
                    self.client.headers["x-user-key"] = "actor-2"
                    response = self.client.get("/api/download/jobs")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {
                        "ok": True, "items": [manager.get(job_id).public() for job_id in expected_ids],
                    })
                    for item in response.json()["items"]:
                        self.assertNotIn("user_key", item)
                        self.assertNotIn("path", item)

    def test_all_four_download_job_routes_exclude_foreign_missing_and_ownerless_jobs(self) -> None:
        for identity in ("token", "admin-token", "browser", "local"):
            self.client.cookies.clear()
            self.record["delegated_admin"] = identity == "admin-token"
            if identity == "browser":
                self.browser(["operators"])
            elif identity == "local":
                self.client.headers.pop("authorization", None)
            else:
                self.client.headers["authorization"] = f"Bearer {self.plaintext}"
            self.client.headers["x-user-key"] = "foreign-owner"
            for job in (None, SimpleNamespace(user_key="foreign-owner"), SimpleNamespace(),
                        SimpleNamespace(user_key=None), SimpleNamespace(user_key="")):
                with self.subTest(identity=identity, job=job), patch.dict(os.environ, {
                    "OPENFEED_REQUIRE_LOGIN": "false" if identity == "local" else "true",
                }), patch.object(downloads.manager, "get", return_value=job), \
                     patch.object(downloads.manager, "list_jobs", return_value=[] if job is None else [job]), \
                     patch.object(downloads.manager, "cancel") as cancel, \
                     patch.object(downloads, "resolve_completed_path") as resolve:
                    response = self.client.get("/api/download/jobs")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True, "items": []})
                    for method, path in (("GET", "/api/download/jobs/foreign-job"),
                                         ("DELETE", "/api/download/jobs/foreign-job"),
                                         ("GET", "/api/download/jobs/foreign-job/file")):
                        response = self.client.request(method, path)
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(response.json(), {"detail": "No such download job"})
                    cancel.assert_not_called()
                    resolve.assert_not_called()

    def test_download_job_owner_still_gets_existing_file_state_errors(self) -> None:
        job = downloads.DownloadJob(id="owned-job", youtube_video_id="test", height=720,
                                    container="mp4", user_key="actor-1", status="downloading")
        with patch.object(downloads.manager, "get", return_value=job), \
             patch.object(downloads, "resolve_completed_path", return_value=None) as resolve:
            response = self.client.get("/api/download/jobs/owned-job/file")
            self.assertEqual(response.status_code, 409)
            resolve.assert_not_called()
            job.status = "done"
            self.assertEqual(self.client.get("/api/download/jobs/owned-job/file").status_code, 410)
            resolve.assert_called_once_with(job)

    def test_read_only_download_owner_cannot_cancel(self) -> None:
        self.record["read_only"] = True
        job = downloads.DownloadJob(id="owned-job", youtube_video_id="test", height=720,
                                    container="mp4", user_key="actor-1")
        with patch.object(downloads.manager, "get", return_value=job), \
             patch.object(downloads.manager, "cancel") as cancel:
            self.assertEqual(self.client.get("/api/download/jobs/owned-job").status_code, 200)
            response = self.client.delete("/api/download/jobs/owned-job")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "read_only_token")
        cancel.assert_not_called()

    def test_collection_member_body_passes_identifiers_unchanged_and_uses_token_owner(self) -> None:
        collection_id = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        for subscription_id in ("feed/4", "feed%2F4", " feed/4 ", "feed/4?part=1#literal"):
            with self.subTest(subscription_id=subscription_id), \
                 patch.object(db, "remove_subscription_from_collection", return_value=1) as remove:
                response = self.client.request("DELETE", f"/api/subscription-collections/{collection_id}/members", json={
                    "subscription_id": subscription_id, "user_key": "foreign-owner",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"ok": True})
                remove.assert_called_once_with(collection_id, subscription_id, "actor-1")

    def test_collection_member_body_denies_missing_or_foreign_members(self) -> None:
        collection_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with patch.object(db, "remove_subscription_from_collection", return_value=0) as remove:
            response = self.client.request("DELETE", f"/api/subscription-collections/{collection_id}/members",
                                           json={"subscription_id": "feed/4"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Collection member not found"})
        remove.assert_called_once_with(collection_id, "feed/4", "actor-1")

    def test_collection_member_old_path_alias_keeps_browser_and_slash_support(self) -> None:
        self.browser(["noah_perm_myyt_access"])
        collection_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for subscription_path in ("feed/4", "feed%2F4"):
            with self.subTest(path=subscription_path), \
                 patch.object(db, "remove_subscription_from_collection", return_value=1) as remove:
                response = self.client.delete(f"/api/subscription-collections/{collection_id}/members/{subscription_path}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"ok": True})
                remove.assert_called_once_with(collection_id, "feed/4", "browser-actor")

    def test_collection_member_body_requires_subscription_id(self) -> None:
        with patch.object(db, "remove_subscription_from_collection") as remove:
            for payload in (None, {}, {"subscription_id": None}):
                response = self.client.request("DELETE", "/api/subscription-collections/test/members", json=payload)
                self.assertEqual(response.status_code, 422)
            remove.assert_not_called()

    def test_read_only_token_cannot_remove_collection_member_via_body(self) -> None:
        self.record["read_only"] = True
        with patch.object(db, "remove_subscription_from_collection") as remove:
            response = self.client.request("DELETE", "/api/subscription-collections/test/members",
                                           json={"subscription_id": "feed/4"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "read_only_token")
        remove.assert_not_called()

    def test_library_delete_keeps_file_when_reference_check_is_unavailable(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "library.mp4"
        path.write_bytes(b"keep-on-uncertain-cleanup")
        with patch.dict(os.environ, {"OPENFEED_DOWNLOAD_DIR": directory}), \
             patch.object(db, "delete_download", return_value={"path": str(path)}) as delete, \
             patch.object(db, "locked_download_paths", side_effect=RuntimeError("test database unavailable")):
            response = self.client.delete("/api/downloads/test-video")
        self.assertEqual(response.status_code, 200)
        delete.assert_called_once_with("test-video", "actor-1")
        self.assertEqual(path.read_bytes(), b"keep-on-uncertain-cleanup")


@unittest.skipUnless(os.environ.get("OPENFEED_TEST_DATABASE_URL"), "requires an isolated PostgreSQL test database")
class TokenSchemaTests(unittest.TestCase):
    """Opt-in PostgreSQL tests; never use the application's DATABASE_URL.

    Each test owns a temporary schema on the explicitly supplied test database.
    """

    def setUp(self) -> None:
        self.dsn = os.environ["OPENFEED_TEST_DATABASE_URL"]
        self.schema = "token_test_" + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.enterContext(patch.object(db, "connect", self.connect))

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            yield conn

    def drop_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def test_fresh_startup_is_idempotent_and_helpers_preserve_claims(self) -> None:
        db.init_db()
        # Existing positional callers retain read_only and user_key semantics.
        ordinary = db.create_api_token("ordinary", "111111111111", "test-hash", True, "actor-1")
        admin = db.create_api_token("admin", "222222222222", "test-hash", False, "actor-2", delegated_admin=True)
        db.init_db()
        self.assertFalse(db.find_api_token(ordinary["token_prefix"])["delegated_admin"])
        stored = db.find_api_token(admin["token_prefix"])
        self.assertTrue(stored["delegated_admin"])
        self.assertEqual(stored["user_key"], "actor-2")
        self.assertEqual(len(db.list_api_tokens("actor-1")), 1)
        self.assertIsNone(db.revoke_api_token(str(admin["id"]), "actor-1"))
        self.assertIsNotNone(db.find_api_token(admin["token_prefix"]))
        db.revoke_api_token(str(admin["id"]), "actor-2")
        self.assertIsNone(db.find_api_token(admin["token_prefix"]))
        self.assertEqual(db.list_api_tokens("actor-2"), [])

    def test_existing_rows_migrate_without_admin_or_credential_changes(self) -> None:
        with self.connect() as conn:
            conn.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.execute("""
                INSERT INTO api_tokens (user_key, name, token_prefix, token_hash, read_only, revoked_at)
                VALUES ('actor-1', 'legacy', '111111111111', 'test-hash', true, NULL),
                       ('actor-2', 'revoked', '222222222222', 'revoked-hash', false, now())
            """)
            before = list(conn.execute("SELECT * FROM api_tokens ORDER BY token_prefix").fetchall())
        db.init_db()
        db.init_db()
        with self.connect() as conn:
            after = list(conn.execute("SELECT * FROM api_tokens ORDER BY token_prefix").fetchall())
        for old, migrated in zip(before, after):
            self.assertIs(migrated.pop("delegated_admin"), False)
            self.assertEqual(migrated, old)
        self.assertIsNone(db.find_api_token("222222222222"))

    def test_collection_body_removal_enforces_database_owner_and_exact_identifier(self) -> None:
        db.init_db()
        collection = db.create_subscription_collection("Owner collection", None, "owner-1")
        collection_id = str(collection["id"])
        subscription_ids = ("feed/4", "feed%2F4", " feed/4 ")
        with self.connect() as conn:
            for subscription_id in subscription_ids:
                conn.execute("INSERT INTO freshrss_subscriptions (id, title) VALUES (%s, 'Test')", (subscription_id,))
        for subscription_id in subscription_ids:
            self.assertIsNotNone(db.add_subscription_to_collection(collection_id, subscription_id, "owner-1"))
        with patch.dict(os.environ, {
            "OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "noah_perm_myyt_access",
            "OPENFEED_ADMIN_GROUPS": "operators", "OPENFEED_SESSION_SECRET": "test-only-session-signing-key",
        }, clear=True):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            # Even a browser administrator cannot remove another actor's collection members.
            for owner, expected in (("owner-2", 404), ("owner-1", 200)):
                client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
                    "sub": owner, "groups": ["operators"], "expires_at": main._utc_timestamp() + 600,
                }))
                for subscription_id in subscription_ids:
                    with self.subTest(owner=owner, subscription_id=subscription_id):
                        response = client.request("DELETE", f"/api/subscription-collections/{collection_id}/members",
                                                  json={"subscription_id": subscription_id})
                        self.assertEqual(response.status_code, expected)
                        with self.connect() as conn:
                            remaining = conn.execute(
                                "SELECT subscription_id FROM subscription_collection_members WHERE collection_id = %s",
                                (collection_id,),
                            ).fetchall()
                        remaining_ids = {row["subscription_id"] for row in remaining}
                        if owner == "owner-2":
                            self.assertEqual(remaining_ids, set(subscription_ids))
                        else:
                            self.assertNotIn(subscription_id, remaining_ids)
                if owner == "owner-1":
                    self.assertEqual(remaining_ids, set())

    def test_library_delete_preserves_legacy_shared_file_until_final_owner_removes_it(self) -> None:
        db.init_db()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "legacy-shared.mp4"
        path.write_bytes(b"shared-video")
        for owner in ("owner-1", "owner-2"):
            db.register_download("same-video", "Same title", "Friendly name.mp4", str(path), user_key=owner)
        with patch.dict(os.environ, {
            "OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "noah_perm_myyt_access",
            "OPENFEED_SESSION_SECRET": "test-only-session-signing-key", "OPENFEED_DOWNLOAD_DIR": directory,
        }, clear=True):
            client = TestClient(main.app)
            self.addCleanup(client.close)

            def as_owner(owner):
                client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
                    "sub": owner, "groups": ["noah_perm_myyt_access"], "expires_at": main._utc_timestamp() + 600,
                }))

            as_owner("outsider")
            self.assertEqual(client.delete("/api/downloads/same-video").status_code, 404)
            self.assertEqual(path.read_bytes(), b"shared-video")
            as_owner("owner-1")
            self.assertEqual(client.delete("/api/downloads/same-video").status_code, 200)
            self.assertIsNone(db.get_download("same-video", "owner-1"))
            self.assertIsNotNone(db.get_download("same-video", "owner-2"))
            self.assertEqual(client.get("/api/downloads/same-video/file").status_code, 404)
            as_owner("owner-2")
            response = client.get("/api/downloads/same-video/file")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"shared-video")
            self.assertIn("Friendly%20name.mp4", response.headers["content-disposition"])
            self.assertEqual(client.delete("/api/downloads/same-video").status_code, 200)
            self.assertFalse(path.exists())
            self.assertIsNone(db.get_download("same-video", "owner-2"))

    def test_library_replacement_leaves_old_copy_and_other_owner_reference_intact(self) -> None:
        db.init_db()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        old_path, new_path = directory / "old.mp4", directory / "new-job.mp4"
        old_path.write_bytes(b"old-shared-copy")
        new_path.write_bytes(b"new-owner-copy")
        for owner in ("owner-1", "owner-2"):
            db.register_download("same-video", "Title", "Friendly.mp4", str(old_path), user_key=owner)
        db.register_download("same-video", "Title", "Friendly.mp4", str(new_path), user_key="owner-1")
        self.assertEqual(db.get_download("same-video", "owner-1")["path"], str(new_path))
        self.assertEqual(db.get_download("same-video", "owner-2")["path"], str(old_path))
        self.assertEqual(old_path.read_bytes(), b"old-shared-copy")
        self.assertFalse(downloads.unlink_unreferenced_download(old_path))
        self.assertEqual(new_path.read_bytes(), b"new-owner-copy")

    def test_library_reference_lock_blocks_concurrent_registration(self) -> None:
        db.init_db()
        with db.locked_download_paths() as paths:
            self.assertEqual(paths, [])
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                with self.connect() as conn:
                    conn.execute("SET LOCAL lock_timeout = '100ms'")
                    conn.execute("""
                        INSERT INTO downloads (user_key, youtube_video_id, title, filename, path)
                        VALUES ('owner-2', 'video', 'Title', 'video.mp4', '/test/video.mp4')
                    """)
        # The write lock is released after cleanup, so normal registrations still work.
        self.assertIsNotNone(db.register_download("video", "Title", "video.mp4", "/test/video.mp4", user_key="owner-2"))


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


if __name__ == "__main__":
    unittest.main()
