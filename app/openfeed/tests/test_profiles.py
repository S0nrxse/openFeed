"""Household profiles: resolution, the cookie boundary, tokens, kids, and isolation.

The unit tests run everywhere. ProfileDatabaseTests needs OPENFEED_TEST_DATABASE_URL and
runs each test in its own temporary schema, like the token schema tests.
"""

from __future__ import annotations

import os
import re
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from openfeed import db, main, profiles, tokens
from openfeed.tests.support import offline_db

SECRET = "test-only-session-signing-key"
ACCOUNT = "account-a"
OTHER = "account-b"


def _row(account: str, name: str, *, main_profile: bool = False, kids: bool = False, color: str = "graphite") -> dict:
    return {
        "id": str(uuid.uuid4()), "account_key": account, "name": name, "color": color, "is_kids": kids,
        "is_main": main_profile, "position": 0,
        "user_key": account if main_profile else f"{account}:p:{uuid.uuid4().hex[:16]}",
    }


class _Request:
    def __init__(self, cookies: dict[str, str] | None = None, path: str = "/") -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.scope: dict = {}
        self.url = type("U", (), {"path": path, "query": ""})()


class ProfileResolutionTests(unittest.TestCase):
    """_current_user resolves the profile from the cookie, but only within the account."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_SESSION_SECRET": SECRET}, clear=True))
        self.main = _row(ACCOUNT, "Main", main_profile=True)
        self.kid = _row(ACCOUNT, "Kid", kids=True)
        self.other_main = _row(OTHER, "Other", main_profile=True)
        self.other_kid = _row(OTHER, "Other kid")
        rows = {ACCOUNT: [self.main, self.kid], OTHER: [self.other_main, self.other_kid]}
        self.lookup = self.enterContext(patch.object(profiles, "list_profiles", side_effect=lambda key: rows.get(key, [])))

    def resolve(self, account: str, cookie: str | None) -> dict:
        user = {"authenticated": True, "user_key": account, "sub": account}
        request = _Request({main.PROFILE_COOKIE: cookie} if cookie else {})
        return main._with_profile(request, user)

    def test_valid_cookie_switches_user_key_and_keeps_the_account(self) -> None:
        user = self.resolve(ACCOUNT, main._profile_cookie_value(ACCOUNT, self.kid["id"]))
        self.assertEqual(user["user_key"], self.kid["user_key"])
        self.assertEqual(user["account_key"], ACCOUNT)
        self.assertTrue(user["profile_chosen"])

    def test_no_cookie_acts_as_the_main_profile(self) -> None:
        user = self.resolve(ACCOUNT, None)
        self.assertEqual(user["user_key"], ACCOUNT)
        self.assertFalse(user["profile_chosen"])
        self.assertTrue(main._needs_profile_choice(user))

    def test_cookie_naming_another_accounts_profile_is_rejected(self) -> None:
        # Signed for the other account: the account binding fails.
        signed_elsewhere = main._profile_cookie_value(OTHER, self.other_kid["id"])
        # Signed for this account but naming the other account's profile id: not listed here.
        relabelled = main._profile_cookie_value(ACCOUNT, self.other_kid["id"])
        for cookie in (signed_elsewhere, relabelled, self.other_kid["id"], "garbage", main._sign_payload({"a": ACCOUNT})):
            with self.subTest(cookie=cookie[:24]):
                user = self.resolve(ACCOUNT, cookie)
                self.assertEqual(user["user_key"], ACCOUNT)
                self.assertFalse(user["profile_chosen"])

    def test_a_signed_cookie_cannot_be_forged_with_another_key(self) -> None:
        body = main._sign_payload({"a": ACCOUNT, "p": self.kid["id"]})
        encoded = body.rsplit(".", 1)[0]
        forged = encoded + ".AAAA"
        self.assertEqual(self.resolve(ACCOUNT, forged)["user_key"], ACCOUNT)

    def test_unsigned_cookie_works_only_without_a_secret(self) -> None:
        self.assertEqual(self.resolve(ACCOUNT, self.kid["id"])["user_key"], ACCOUNT)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main._profile_cookie_value(ACCOUNT, self.kid["id"]), self.kid["id"])
            self.assertEqual(self.resolve(ACCOUNT, self.kid["id"])["user_key"], self.kid["user_key"])
            # Still bounded by the account's own rows.
            self.assertEqual(self.resolve(ACCOUNT, self.other_kid["id"])["user_key"], ACCOUNT)

    def test_database_failure_falls_back_to_the_account_key(self) -> None:
        self.lookup.side_effect = RuntimeError("no database")
        user = self.resolve(ACCOUNT, main._profile_cookie_value(ACCOUNT, self.kid["id"]))
        self.assertEqual(user["user_key"], ACCOUNT)
        self.assertEqual(user["profiles"], [])

    def test_profiles_are_read_once_per_request(self) -> None:
        request = _Request({main.PROFILE_COOKIE: main._profile_cookie_value(ACCOUNT, self.kid["id"])})
        for _ in range(4):
            main._with_profile(request, {"authenticated": True, "user_key": ACCOUNT})
        self.assertEqual(self.lookup.call_count, 1)


class ProfileRouteTests(unittest.TestCase):
    """Real middleware and routes over a signed-in session, with profile storage mocked."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {
            "OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "family",
            "OPENFEED_SESSION_SECRET": SECRET,
        }, clear=True))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        self.main = _row(ACCOUNT, "Main", main_profile=True)
        self.kid = _row(ACCOUNT, "Kid", kids=True)
        self.adult = _row(ACCOUNT, "Sam", color="plum")
        self.other_kid = _row(OTHER, "Other kid")
        self.rows = {ACCOUNT: [self.main, self.kid, self.adult], OTHER: [_row(OTHER, "Other", main_profile=True), self.other_kid]}
        self.enterContext(patch.object(profiles, "list_profiles", side_effect=lambda key: self.rows.get(key, [])))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
            "sub": ACCOUNT, "groups": ["family"], "iat": main._utc_timestamp(), "expires_at": main._utc_timestamp() + 600,
        }))

    def choose(self, profile: dict, account: str = ACCOUNT) -> None:
        self.client.cookies.set(main.PROFILE_COOKIE, main._profile_cookie_value(account, profile["id"]))

    def test_pages_ask_whos_watching_until_a_profile_is_chosen(self) -> None:
        response = self.client.get("/history", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/profiles?next=/history")
        with offline_db():
            picker = self.client.get("/profiles?next=/history")
        self.assertEqual(picker.status_code, 200)
        for profile in self.rows[ACCOUNT]:
            self.assertIn(f'value="{profile["id"]}"', picker.text)
        self.assertNotIn(self.other_kid["id"], picker.text)
        self.assertIn("keeps its own subscriptions", picker.text)

    def test_the_api_acts_as_the_main_profile_without_a_choice(self) -> None:
        with patch.object(db, "list_playlists", return_value=[]) as playlists:
            self.assertEqual(self.client.get("/api/playlists").status_code, 200)
        playlists.assert_called_once_with(ACCOUNT)

    def test_api_requests_act_as_the_chosen_profile(self) -> None:
        self.choose(self.adult)
        with patch.object(db, "list_playlists", return_value=[]) as playlists, \
                patch.object(db, "create_playlist", return_value={"id": "x"}) as create:
            self.client.get("/api/playlists")
            self.client.post("/api/playlists", json={"name": "Mine", "user_key": ACCOUNT})
        playlists.assert_called_once_with(self.adult["user_key"])
        create.assert_called_once_with("Mine", None, self.adult["user_key"])

    def test_select_sets_a_signed_httponly_cookie_for_own_profiles_only(self) -> None:
        response = self.client.post("/profiles/select", data={"profile_id": self.kid["id"], "next": "/watch/abc"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/watch/abc")
        cookie = response.headers["set-cookie"]
        self.assertIn(f"{main.PROFILE_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn(self.kid["user_key"], cookie)

        refused = self.client.post("/profiles/select", data={"profile_id": self.other_kid["id"], "next": "/"}, follow_redirects=False)
        self.assertEqual(refused.status_code, 303)
        self.assertTrue(refused.headers["location"].startswith("/profiles"))
        self.assertNotIn("set-cookie", refused.headers)

    def test_select_refuses_an_offsite_next(self) -> None:
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example/", "/\\/evil.example"):
            with self.subTest(target=target):
                response = self.client.post("/profiles/select", data={"profile_id": self.adult["id"], "next": target}, follow_redirects=False)
                self.assertEqual(response.headers["location"], "/")

    def test_cookie_naming_another_accounts_profile_never_reaches_its_data(self) -> None:
        self.choose(self.other_kid, account=OTHER)
        with patch.object(db, "list_playlists", return_value=[]) as playlists:
            self.client.get("/api/playlists")
        playlists.assert_called_once_with(ACCOUNT)
        self.choose(self.other_kid, account=ACCOUNT)
        with patch.object(db, "list_playlists", return_value=[]) as playlists:
            self.client.get("/api/playlists")
        playlists.assert_called_once_with(ACCOUNT)

    def test_management_is_refused_on_a_kids_profile(self) -> None:
        self.choose(self.kid)
        with patch.object(profiles, "create_profile") as create, patch.object(profiles, "delete_profile") as delete:
            self.assertEqual(self.client.post("/api/profiles", json={"name": "New"}).json()["detail"], "kids_profile")
            self.assertEqual(self.client.delete(f"/api/profiles/{self.adult['id']}").status_code, 403)
            self.assertEqual(self.client.patch(f"/api/profiles/{self.kid['id']}", json={"is_kids": False}).status_code, 403)
        create.assert_not_called()
        delete.assert_not_called()
        listing = self.client.get("/api/profiles").json()
        self.assertFalse(listing["can_manage"])
        self.assertEqual(listing["active"], self.kid["id"])

    def test_management_passes_the_account_never_a_profile_key(self) -> None:
        self.choose(self.adult)
        with patch.object(profiles, "delete_profile", return_value=None) as delete:
            self.assertEqual(self.client.delete(f"/api/profiles/{self.other_kid['id']}").status_code, 404)
        delete.assert_called_once_with(ACCOUNT, self.other_kid["id"])

    def test_deleting_the_main_profile_is_refused(self) -> None:
        error = profiles.ProfileError("main_profile", "no", 409)
        with patch.object(profiles, "delete_profile", side_effect=error):
            response = self.client.delete(f"/api/profiles/{self.main['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "main_profile")


class ProfileTokenTests(unittest.TestCase):
    """A token acts as the profile it was minted under, and ignores the profile cookie."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {
            "OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_REQUIRED_GROUP": "family", "OPENFEED_SESSION_SECRET": SECRET,
        }, clear=True))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        self.kid = _row(ACCOUNT, "Kid", kids=True)
        self.adult = _row(ACCOUNT, "Sam")
        self.enterContext(patch.object(profiles, "list_profiles", return_value=[_row(ACCOUNT, "Main", main_profile=True), self.kid, self.adult]))
        self.plaintext, prefix, digest = tokens.generate()
        self.record = {"id": str(uuid.uuid4()), "user_key": self.kid["user_key"], "name": "kid script", "token_prefix": prefix,
                       "token_hash": digest, "read_only": False, "revoked_at": None}
        self.enterContext(patch.object(db, "find_api_token", return_value=self.record))
        self.enterContext(patch.object(db, "touch_api_token"))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.client.headers["authorization"] = f"Bearer {self.plaintext}"

    def test_token_acts_as_its_profile_whatever_the_cookie_says(self) -> None:
        self.client.cookies.set(main.PROFILE_COOKIE, main._profile_cookie_value(ACCOUNT, self.adult["id"]))
        with patch.object(db, "list_playlists", return_value=[]) as playlists, \
                patch.object(profiles, "profile_for_user_key", return_value=self.kid):
            self.assertEqual(self.client.get("/api/playlists").status_code, 200)
            index = self.client.get("/api/").json()
        playlists.assert_called_once_with(self.kid["user_key"])
        self.assertEqual(index["user_key"], self.kid["user_key"])
        self.assertEqual(index["profile"]["name"], "Kid")

    def test_tokens_cannot_switch_or_manage_profiles(self) -> None:
        with patch.object(profiles, "create_profile") as create:
            self.assertEqual(self.client.post("/api/profiles", json={"name": "x"}).status_code, 403)
            self.assertEqual(self.client.post("/profiles/select", data={"profile_id": self.adult["id"]}).status_code, 403)
        create.assert_not_called()

    def test_new_tokens_are_minted_for_the_active_profile(self) -> None:
        self.client.headers.pop("authorization")
        self.client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
            "sub": ACCOUNT, "groups": ["family"], "iat": main._utc_timestamp(), "expires_at": main._utc_timestamp() + 600,
        }))
        self.client.cookies.set(main.PROFILE_COOKIE, main._profile_cookie_value(ACCOUNT, self.adult["id"]))
        minted = dict(self.record, user_key=self.adult["user_key"], created_at=None, last_used_at=None)
        with patch.object(db, "create_api_token", return_value=minted) as create, \
                patch.object(db, "list_api_tokens", return_value=[]) as listing:
            self.assertEqual(self.client.post("/api/tokens", json={"name": "sam"}).status_code, 201)
            self.client.get("/api/tokens")
        self.assertEqual(create.call_args.args[4], self.adult["user_key"])
        listing.assert_called_once_with(self.adult["user_key"], False)


class ProfileSurfaceTests(unittest.TestCase):
    def user(self, active: dict, rows: list[dict]) -> dict:
        return {"authenticated": False, "user_key": active["user_key"], "account_key": "local", "display_name": "Local OpenFeed",
                "profiles": rows, "profile": active, "profile_chosen": True}

    def test_account_menu_lists_other_profiles_as_post_forms(self) -> None:
        main_row, kid = _row("local", "Main", main_profile=True), _row("local", "Kid <b>", kids=True, color="sage")
        with offline_db(), patch.object(main, "_notification_items", return_value=([], 0)):
            html = main._header_actions(self.user(main_row, [main_row, kid]), main_row["user_key"])
        self.assertIn('action="/profiles/select"', html)
        self.assertIn(f'value="{kid["id"]}"', html)
        self.assertIn("Kid &lt;b&gt;", html)
        self.assertNotIn("Kid <b>", html)
        self.assertIn("Manage profiles", html)
        self.assertIn('data-profile-color="graphite"', html)

    def test_kids_profiles_get_no_manage_row_and_locked_switches(self) -> None:
        main_row, kid = _row("local", "Main", main_profile=True), _row("local", "Kid", kids=True)
        with offline_db(get_feed_preferences={"kids_profile": True, "exclude_shorts": True, "show_comments": False}), \
                patch.object(main, "_notification_items", return_value=([], 0)):
            header = main._header_actions(self.user(kid, [main_row, kid]), kid["user_key"])
            dialog = main._settings_dialog(self.user(kid, [main_row, kid]), kid["user_key"])
        self.assertNotIn("Manage profiles", header)
        self.assertRegex(dialog, r'data-setting-shorts disabled')
        self.assertRegex(dialog, r'data-setting-comments disabled')
        self.assertNotIn("data-profile-add", dialog)
        self.assertNotIn("data-profile-delete", dialog)

    def test_settings_pane_says_what_is_shared_and_what_kids_does(self) -> None:
        with offline_db():
            html = main._settings_dialog(user={"authenticated": False})
        pane = html.split('data-settings-pane-body="profiles"')[1].split("data-settings-pane-body=")[0]
        self.assertIn("Subscriptions", pane)
        self.assertIn("data-profile-add", pane)
        self.assertIn("hides comments and Shorts", pane)
        self.assertIn("not available through Invidious", pane)

    def test_single_profile_menu_offers_to_add_one(self) -> None:
        with offline_db(), patch.object(main, "_notification_items", return_value=([], 0)):
            html = main._header_actions({"authenticated": False, "user_key": "local", "profiles": []}, "local")
        self.assertIn("Add a profile", html)
        self.assertNotIn("Switch profile", html)

    def test_kids_block_age_restricted_but_not_unknown(self) -> None:
        self.assertTrue(main._age_restricted({"metadata": {"isFamilyFriendly": False}}))
        self.assertFalse(main._age_restricted({"metadata": {"isFamilyFriendly": True}}))
        self.assertFalse(main._age_restricted({"metadata": {}}))


class ProfileSchemaCoverageTests(unittest.TestCase):
    def test_every_user_key_table_is_deleted_with_its_profile(self) -> None:
        schema = db.SCHEMA_PATH.read_text(encoding="utf-8")
        tables = set()
        for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", schema, re.S):
            if re.search(r"^\s*user_key TEXT", match.group(2), re.M):
                tables.add(match.group(1))
        tables |= set(re.findall(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS user_key", schema))
        tables.discard("account_profiles")
        self.assertTrue(tables)
        self.assertEqual(tables - set(profiles.USER_KEY_TABLES), set())

    def test_names_and_colours_are_validated(self) -> None:
        self.assertEqual(profiles.clean_name("  Ana   Lu "), "Ana Lu")
        for bad in ("", "   ", "x" * (profiles.NAME_MAX + 1)):
            with self.assertRaises(profiles.ProfileError):
                profiles.clean_name(bad)
        with self.assertRaises(profiles.ProfileError):
            profiles.clean_color("#ff0000")


@unittest.skipUnless(os.environ.get("OPENFEED_TEST_DATABASE_URL"), "requires an isolated PostgreSQL test database")
class ProfileDatabaseTests(unittest.TestCase):
    """Real SQL, one temporary schema per test: isolation and deletion end to end."""

    def setUp(self) -> None:
        self.dsn = os.environ["OPENFEED_TEST_DATABASE_URL"]
        self.schema = "profile_test_" + uuid.uuid4().hex
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.enterContext(patch.object(db, "connect", self.connect))
        db.init_db()
        with self.connect() as conn:
            for vid in ("vidA", "vidB", "vidC"):
                conn.execute("INSERT INTO videos (youtube_video_id, title, duration_seconds) VALUES (%s, %s, 600)", (vid, f"Video {vid}"))

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            yield conn

    def drop_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def write_everything(self, key: str, video: str) -> None:
        db.record_video_event(video, "watch_page_open", {}, key)
        db.update_playback_progress(video, 300, 600, "progress", {}, key, watch_delta_seconds=300)
        db.set_video_feedback(video, "like", {}, key)
        playlist = db.create_playlist(f"List {key[-4:]}", None, key)
        db.add_video_to_playlist(str(playlist["id"]), video, key)
        db.set_feed_preferences(show_comments=True, exclude_shorts=False, discovery_level=0.9, user_key=key)
        db.create_api_token("t", uuid.uuid4().hex[:12], "hash", False, key)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO feed_impressions (video_id, youtube_video_id, metadata, shown_at) "
                "SELECT id, youtube_video_id, jsonb_build_object('user_key', %s::text), now() - interval '2 days' FROM videos WHERE youtube_video_id = %s",
                (key, video),
            )

    def test_migration_is_idempotent_and_main_keeps_the_account_key(self) -> None:
        db.init_db()
        db.init_db()
        kid = profiles.create_profile("acct", "Kid", "sage", True, main_name="Parent")
        rows = profiles.list_profiles("acct")
        self.assertEqual([r["name"] for r in rows], ["Parent", "Kid"])
        self.assertEqual(rows[0]["user_key"], "acct")
        self.assertTrue(rows[0]["is_main"])
        self.assertNotEqual(kid["user_key"], "acct")
        self.assertTrue(kid["user_key"].startswith("acct:p:"))

    def test_limits_names_and_main_rules(self) -> None:
        for index in range(profiles.MAX_PROFILES - 1):
            profiles.create_profile("acct", f"P{index}")
        with self.assertRaises(profiles.ProfileError) as limit:
            profiles.create_profile("acct", "Seventh")
        self.assertEqual(limit.exception.code, "profile_limit")
        rows = profiles.list_profiles("acct")
        self.assertEqual(len(rows), profiles.MAX_PROFILES)
        with self.assertRaises(profiles.ProfileError) as taken:
            profiles.update_profile("acct", rows[2]["id"], name="p0")
        self.assertEqual(taken.exception.code, "name_taken")
        with self.assertRaises(profiles.ProfileError):
            profiles.update_profile("acct", rows[0]["id"], is_kids=True)
        with self.assertRaises(profiles.ProfileError):
            profiles.delete_profile("acct", rows[0]["id"])
        # Another account cannot touch these rows, whatever ids it presents.
        self.assertIsNone(profiles.update_profile("intruder", rows[1]["id"], name="Mine now"))
        self.assertIsNone(profiles.delete_profile("intruder", rows[1]["id"]))
        self.assertEqual(len(profiles.list_profiles("acct")), profiles.MAX_PROFILES)

    def test_main_can_be_renamed_before_any_profile_exists(self) -> None:
        row = profiles.update_profile("acct", "main", name="Household", main_name="x")
        self.assertEqual(row["name"], "Household")
        self.assertEqual(row["user_key"], "acct")

    def test_profiles_are_fully_isolated_and_deletion_removes_only_their_rows(self) -> None:
        from openfeed import ranker

        kid = profiles.create_profile("acct", "Kid", is_kids=True)
        sam = profiles.create_profile("acct", "Sam")
        self.write_everything("acct", "vidA")
        self.write_everything(sam["user_key"], "vidB")

        for key, own, other in (("acct", "vidA", "vidB"), (sam["user_key"], "vidB", "vidA")):
            with self.subTest(key=key):
                history = {r["youtube_video_id"] for r in db.list_history_videos(20, key)}
                liked = {r["youtube_video_id"] for r in db.list_liked_videos(20, key)}
                self.assertIn(own, history)
                self.assertNotIn(other, history)
                self.assertEqual(liked, {own})
                self.assertEqual(len(db.list_playlists(key)), 1)
                self.assertEqual(len(db.list_api_tokens(key)), 1)
                self.assertTrue(db.get_feed_preferences(key)["show_comments"])
        # Continue watching, history, likes and playlists are empty for the untouched profile.
        self.assertEqual(db.list_history_videos(20, kid["user_key"]), [])
        self.assertEqual(db.list_liked_videos(20, kid["user_key"]), [])
        self.assertEqual(db.list_playlists(kid["user_key"]), [])
        self.assertFalse(db.get_feed_preferences(kid["user_key"])["show_comments"])

        # The ranker reads signals and impressions per key and fingerprints per key.
        with self.connect() as conn, conn.cursor() as cur:
            signals = {key: ranker.load_signals(cur, key) for key in ("acct", sam["user_key"], kid["user_key"])}
            prints = {key: ranker.fingerprint(cur, key) for key in signals}
        video_ids = {}
        with self.connect() as conn:
            for row in conn.execute("SELECT id, youtube_video_id FROM videos").fetchall():
                video_ids[row["youtube_video_id"]] = row["id"]
        self.assertIn(video_ids["vidA"], signals["acct"].positives)
        self.assertNotIn(video_ids["vidB"], signals["acct"].positives)
        self.assertIn(video_ids["vidB"], signals[sam["user_key"]].positives)
        self.assertEqual(set(signals["acct"].impression_days), {video_ids["vidA"]})
        self.assertEqual(set(signals[sam["user_key"]].impression_days), {video_ids["vidB"]})
        self.assertEqual(signals[kid["user_key"]].positives, {})
        self.assertEqual(len(set(prints.values())), 3)

        result = profiles.delete_profile("acct", sam["id"])
        self.assertEqual(result["removed"]["playback_progress"], 1)
        self.assertEqual(result["removed"]["feed_impressions"], 1)
        self.assertEqual(result["removed"]["users"], 1)
        with self.connect() as conn:
            for table in profiles.USER_KEY_TABLES:
                count = conn.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE user_key = %s").format(sql.Identifier(table)), (sam["user_key"],)).fetchone()["n"]
                self.assertEqual(count, 0, table)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM users WHERE authentik_sub = %s", (sam["user_key"],)).fetchone()["n"], 0)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM playlist_items").fetchone()["n"], 1)
        # The main profile kept everything.
        self.assertEqual({r["youtube_video_id"] for r in db.list_liked_videos(20, "acct")}, {"vidA"})
        self.assertEqual(len(db.list_playlists("acct")), 1)
        self.assertEqual(len(db.list_api_tokens("acct")), 1)
        self.assertEqual([r["name"] for r in profiles.list_profiles("acct")], ["Main", "Kid"])

    def test_kids_profile_forces_comments_and_shorts_off(self) -> None:
        kid = profiles.create_profile("acct", "Kid", is_kids=True)
        db.set_feed_preferences(show_comments=True, exclude_shorts=False, user_key=kid["user_key"])
        prefs = db.get_feed_preferences(kid["user_key"])
        self.assertTrue(prefs["kids_profile"])
        self.assertFalse(prefs["show_comments"])
        self.assertTrue(prefs["exclude_shorts"])
        profiles.update_profile("acct", kid["id"], is_kids=False)
        prefs = db.get_feed_preferences(kid["user_key"])
        self.assertFalse(prefs["kids_profile"])
        self.assertTrue(prefs["show_comments"])

    def test_browser_end_to_end_isolation_and_foreign_cookie(self) -> None:
        sam = profiles.create_profile("acct", "Sam")
        foreign = profiles.create_profile("other-acct", "Theirs")
        self.write_everything(foreign["user_key"], "vidC")
        with patch.dict(os.environ, {"OPENFEED_REQUIRE_LOGIN": "true", "OPENFEED_SESSION_SECRET": SECRET}, clear=True):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            client.cookies.set(main.get_settings().session_cookie, main._sign_payload({
                "sub": "acct", "groups": [], "iat": main._utc_timestamp(), "expires_at": main._utc_timestamp() + 600,
            }))
            self.assertEqual(client.get("/history", follow_redirects=False).headers["location"], "/profiles?next=/history")
            picked = client.post("/profiles/select", data={"profile_id": sam["id"], "next": "/"}, follow_redirects=False)
            self.assertEqual(picked.status_code, 303)
            self.assertEqual(client.post("/api/feedback", json={"youtube_video_id": "vidB", "event_type": "like"}).status_code, 200)
            self.assertEqual({r["youtube_video_id"] for r in db.list_liked_videos(20, sam["user_key"])}, {"vidB"})
            self.assertEqual(db.list_liked_videos(20, "acct"), [])

            # A cookie naming another account's profile: refused at select, ignored if planted.
            refused = client.post("/profiles/select", data={"profile_id": foreign["id"]}, follow_redirects=False)
            self.assertTrue(refused.headers["location"].startswith("/profiles"))
            self.assertNotIn("set-cookie", refused.headers)
            listing = client.get("/api/profiles").json()
            self.assertEqual({item["name"] for item in listing["items"]}, {"Main", "Sam"})
            self.assertEqual(listing["active"], sam["id"])

            planted = TestClient(main.app)
            self.addCleanup(planted.close)
            planted.cookies.set(main.get_settings().session_cookie, main._sign_payload({
                "sub": "acct", "groups": [], "iat": main._utc_timestamp(), "expires_at": main._utc_timestamp() + 600,
            }))
            for cookie in (main._profile_cookie_value("acct", foreign["id"]), main._profile_cookie_value("other-acct", foreign["id"])):
                planted.cookies.set(main.PROFILE_COOKIE, cookie)
                self.assertEqual(planted.get("/api/videos/vidC").json()["feedback"], {})
                self.assertEqual(planted.get("/api/").json()["user_key"], "acct")
                self.assertEqual(planted.get("/history", follow_redirects=False).status_code, 303)
