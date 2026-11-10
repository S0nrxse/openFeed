"""Two accounts on one server: neither may see or change the other's data.

Every route that takes an id or reads personal data is called as Bob with something that
belongs to Alice. The storage functions are replaced by owner-aware fakes (`_owned`): they
answer only when the user_key they are given is the owner's, which is what the real SQL
does (each query filters on user_key; tests/test_db_queries and test_library_tools check
the SQL itself). So these tests prove two things per route: the route passes the caller's
own key, never one taken from the request, and a miss reads as "not found".

Also covered: in-process caches keyed per user, the install-wide routes and settings that
only an admin may use (403 for a member), downloads (rights, quota, shared files), the
per-account rate limits, and what a member's Settings and Downloads show.

Sign-in runs for real (accounts_support.FakeStore stands in for the accounts tables),
as in test_accounts_flows.
"""

from __future__ import annotations

import inspect
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from openfeed import (
    accounts, admin_pages, dataexport, db, downloads, instance, library_tools, main, passwords, policy,
    profiles, ratelimit, search_extras, signin,
)
from openfeed.tests.accounts_support import FakeStore, fake_accounts
from openfeed.tests.support import offline_db

ORIGIN = "http://testserver"
PASSWORD = "tangerine bicycle orbit"
PLAYLIST = str(uuid.uuid4())
COLLECTION = str(uuid.uuid4())
TRASH = str(uuid.uuid4())
TOKEN_ID = str(uuid.uuid4())
PROFILE = str(uuid.uuid4())
VIDEO = "dQw4w9WgXcQ"
# The real functions, before any test (or offline_db) swaps one for a mock: their
# signatures say where the user_key argument is.
_ORIGINALS = {id(module): dict(vars(module)) for module in (db, library_tools, profiles, search_extras, dataexport, main.channel_tools)}


_FAST_HASHER = __import__("argon2").PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class Recorder:
    """A fake for one storage function that answers only for its owner's user_key."""

    def __init__(self, real: Callable[..., Any], owner: str, value: Any, empty: Any, key_names: tuple[str, ...]) -> None:
        self.signature = inspect.signature(real)
        self.owner, self.value, self.empty, self.key_names = owner, value, empty, key_names
        self.keys: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        key = next(str(bound.arguments[name]) for name in self.key_names if name in bound.arguments)
        self.keys.append(key)
        return self.value if key == self.owner else self.empty


class _TwoAccounts(unittest.TestCase):
    env = {"OPENFEED_AUTH_MODE": "accounts", "OPENFEED_SECRET_KEY": "isolation-test-secret", "OPENFEED_PUBLIC_URL": "http://testserver"}

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        # Cheap Argon2 parameters: these tests are about who sees what, not password cost.
        self.enterContext(patch.object(passwords, "_hasher", _FAST_HASHER))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        ratelimit.reset_client()
        self.addCleanup(ratelimit.reset_client)
        self.enterContext(patch.object(ratelimit, "_redis", side_effect=ConnectionError("no redis in tests")))
        instance.forget()
        self.addCleanup(instance.forget)
        self.store = self.enterContext(fake_accounts(FakeStore()))
        self.enterContext(offline_db())
        for name, value in (("list_api_tokens", []), ("find_api_token", None), ("touch_api_token", None)):
            self.enterContext(patch.object(db, name, return_value=value))
        self.notifications = self.enterContext(patch.object(main, "_notification_items", return_value=([], 0)))
        self.enterContext(patch.object(main, "_appearance_choice", return_value=""))
        self.enterContext(patch.object(main, "_rss_poller_status", return_value={"ok": False}))
        self.enterContext(patch.object(main, "_dearrow_body_attrs", return_value=""))
        self.root = self.make("rootadmin", role="admin")
        self.alice = self.make("alice")
        self.bob = self.make("bob")
        self.A, self.B = self.alice["user_key"], self.bob["user_key"]
        self.admin_client = self.client_for("rootadmin")
        self.a = self.client_for("alice")
        self.b = self.client_for("bob")

    def make(self, username: str, role: str = "member") -> dict[str, Any]:
        return self.store.create_account(username, username.title(), role=role, password_hash=passwords.hash_password(PASSWORD))

    def client_for(self, username: str) -> TestClient:
        client = TestClient(main.app)
        client.headers["origin"] = ORIGIN
        self.addCleanup(client.close)
        response = client.post("/auth/login", data={"username": username, "password": PASSWORD, "next": "/"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text[:300])
        return client

    def own(self, target: Any, name: str, value: Any, empty: Any = None, owner: str | None = None,
            key_names: tuple[str, ...] = ("user_key", "viewer_key", "account_key")) -> Recorder:
        real = _ORIGINALS.get(id(target), {}).get(name) or getattr(target, name)
        recorder = Recorder(real, owner or self.A, value, empty, key_names)
        self.enterContext(patch.object(target, name, side_effect=recorder))
        return recorder

    def assertOnlyBob(self, recorder: Recorder) -> None:
        self.assertTrue(recorder.keys, "the storage function was never asked")
        self.assertEqual(set(recorder.keys), {self.B}, "a route passed someone else's key")


class PlaylistIsolationTests(_TwoAccounts):
    def setUp(self) -> None:
        super().setUp()
        playlist = {"id": PLAYLIST, "user_key": self.A, "name": "Alice's mix", "visibility": "private", "video_count": 1}
        self.get = self.own(db, "get_playlist", playlist)
        self.videos = self.own(db, "list_playlist_videos", [{"youtube_video_id": VIDEO, "title": "Secret"}], [])
        self.update = self.own(db, "update_playlist", playlist)
        self.add = self.own(db, "add_video_to_playlist", {"id": 1})
        self.remove = self.own(db, "remove_video_from_playlist", 1, 0)
        self.delete = self.own(library_tools, "delete_playlist", TRASH)
        self.settings = self.own(library_tools, "update_playlist_settings", playlist)
        self.bulk_add = self.own(library_tools, "add_videos_to_playlist", 3, 0)
        self.bulk_remove = self.own(library_tools, "remove_playlist_items", 3, 0)
        self.move = self.own(library_tools, "move_playlist_item", [VIDEO])
        self.viewable = self.own(library_tools, "get_viewable_playlist", None)

    def test_another_accounts_playlist_is_not_found_through_every_route(self) -> None:
        calls = [
            self.b.get(f"/api/playlists/{PLAYLIST}/items"),
            self.b.patch(f"/api/playlists/{PLAYLIST}", json={"name": "Mine now"}),
            self.b.delete(f"/api/playlists/{PLAYLIST}"),
            self.b.post(f"/api/playlists/{PLAYLIST}/items", json={"youtube_video_id": VIDEO}),
            self.b.delete(f"/api/playlists/{PLAYLIST}/items/{VIDEO}"),
            self.b.post(f"/api/playlists/{PLAYLIST}/items/bulk", json={"youtube_video_ids": [VIDEO]}),
            self.b.post(f"/api/playlists/{PLAYLIST}/items/move", json={"youtube_video_id": VIDEO, "move": "top"}),
            self.b.post(f"/api/playlists/{PLAYLIST}/settings", json={"visibility": "public"}),
        ]
        self.assertEqual([response.status_code for response in calls], [404] * len(calls))
        removed = self.b.post(f"/api/playlists/{PLAYLIST}/items/remove", json={"youtube_video_ids": [VIDEO]})
        self.assertEqual(removed.status_code, 404, "like every other write to someone else's playlist")
        self.assertFalse(self.bulk_remove.keys, "refused before the storage function")
        for recorder in (self.get, self.update, self.add, self.remove, self.delete, self.settings, self.move):
            self.assertOnlyBob(recorder)
        self.assertNotIn("Secret", str([response.text for response in calls]))

    def test_the_owner_still_gets_it(self) -> None:
        self.assertEqual(self.a.get(f"/api/playlists/{PLAYLIST}/items").status_code, 200)

    def test_a_private_playlist_page_is_not_found_and_a_public_one_is_read_only(self) -> None:
        page = self.b.get(f"/playlist/{PLAYLIST}")
        self.assertIn("Playlist not found", page.text)
        self.assertNotIn("Secret", page.text)
        self.assertOnlyBob(self.viewable)
        public = {"id": PLAYLIST, "user_key": self.A, "name": "Alice's mix", "visibility": "public", "is_owner": False,
                  "owner_name": "Alice", "video_count": 1}
        with patch.object(library_tools, "get_viewable_playlist", return_value=public):
            shared = self.b.get(f"/playlist/{PLAYLIST}")
        self.assertIn("Alice&#x27;s mix", shared.text)
        self.assertNotIn("data-delete-playlist", shared.text)
        self.assertNotIn("data-remove-playlist-item", shared.text)
        # The API agrees with the page: readable, read-only, without Alice's key; writes stay 404.
        with patch.object(library_tools, "get_viewable_playlist", return_value=public):
            items = self.b.get(f"/api/playlists/{PLAYLIST}/items")
            removed = self.b.post(f"/api/playlists/{PLAYLIST}/items/remove", json={"youtube_video_ids": [VIDEO]})
        self.assertEqual(items.status_code, 200)
        self.assertTrue(items.json()["playlist"]["read_only"])
        self.assertNotIn(self.A, items.text)
        self.assertEqual(removed.status_code, 404)


class CollectionIsolationTests(_TwoAccounts):
    def test_another_accounts_collection_is_empty_or_missing(self) -> None:
        update = self.own(db, "update_subscription_collection", {"id": COLLECTION})
        delete = self.own(library_tools, "delete_collection", TRASH)
        members = self.own(db, "list_collection_subscriptions", [{"id": "feed/1", "title": "Alice's channel"}], [])
        videos = self.own(db, "list_collection_videos", [{"youtube_video_id": VIDEO, "title": "Secret"}], [])
        add = self.own(db, "add_subscription_to_collection", {"id": 1})
        remove = self.own(db, "remove_subscription_from_collection", 1, 0)
        self.assertEqual(self.b.patch(f"/api/subscription-collections/{COLLECTION}", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/subscription-collections/{COLLECTION}").status_code, 404)
        self.assertEqual(self.b.get(f"/api/subscription-collections/{COLLECTION}/subscriptions").json()["items"], [])
        self.assertEqual(self.b.get(f"/api/subscription-collections/{COLLECTION}/videos").json()["items"], [])
        self.assertEqual(self.b.post(f"/api/subscription-collections/{COLLECTION}/members", json={"subscription_id": "feed/1"}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/subscription-collections/{COLLECTION}/members/feed/1").status_code, 404)
        for recorder in (update, delete, members, videos, add, remove):
            self.assertOnlyBob(recorder)

    def test_trash_restore_ids_are_per_account(self) -> None:
        restore = self.own(library_tools, "restore_deleted", {"kind": "playlist", "id": PLAYLIST})
        response = self.b.post(f"/api/library/restore/{TRASH}")
        self.assertNotEqual(response.status_code, 200)
        self.assertOnlyBob(restore)
        self.assertEqual(self.a.post(f"/api/library/restore/{TRASH}").status_code, 200)


class DownloadIsolationTests(_TwoAccounts):
    def setUp(self) -> None:
        super().setUp()
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in self.folder.iterdir()] and None)
        self.enterContext(patch.dict(os.environ, {"OPENFEED_DOWNLOAD_DIR": str(self.folder)}))
        self.file = self.folder / "job1.mp4"
        self.file.write_bytes(b"alice's video")
        self.record = {"youtube_video_id": VIDEO, "path": str(self.file), "filename": "Secret.mp4", "user_key": self.A, "size_bytes": 13}

    def test_another_accounts_kept_file_cannot_be_fetched_played_or_deleted(self) -> None:
        get = self.own(db, "get_download", self.record)
        delete = self.own(db, "delete_download", self.record)
        self.assertEqual(self.b.get(f"/api/downloads/{VIDEO}/file").status_code, 404)
        self.assertEqual(self.b.get(f"/watch/{VIDEO}/local").status_code, 404)
        self.assertEqual(self.b.delete(f"/api/downloads/{VIDEO}").status_code, 404)
        self.assertTrue(self.file.exists())
        self.assertOnlyBob(get)
        self.assertOnlyBob(delete)
        # The owner can, and the API never names the path on the server.
        self.assertEqual(self.a.get(f"/api/downloads/{VIDEO}/file").content, b"alice's video")
        with patch.object(db, "get_video_by_youtube_id", return_value={"youtube_video_id": VIDEO}), \
                patch.object(db, "get_video_feedback", return_value=None), patch.object(db, "get_playback_progress", return_value=None):
            payload = self.a.get(f"/api/videos/{VIDEO}").json()
        self.assertEqual(payload["download"]["filename"], "Secret.mp4")
        self.assertNotIn("path", payload["download"])

    def test_there_is_no_route_from_a_path_to_a_file(self) -> None:
        # Files are only ever found through the caller's own row; a path in the URL is a 404.
        for path in (f"/api/downloads/{self.file}/file", f"/api/downloads/..%2F{self.file.name}/file", "/api/downloads/job1.mp4/file"):
            with patch.object(db, "get_download", return_value=None):
                self.assertIn(self.b.get(path).status_code, {404, 400})

    def test_download_jobs_belong_to_who_started_them(self) -> None:
        job = downloads.DownloadJob(id="job1", youtube_video_id=VIDEO, height=720, container="mp4", user_key=self.A,
                                    status="done", path=str(self.file), filename="Secret.mp4")
        with patch.object(downloads.manager, "get", return_value=job), patch.object(downloads.manager, "list_jobs", return_value=[job]), \
                patch.object(downloads.manager, "cancel", return_value=True) as cancel:
            self.assertEqual(self.b.get("/api/download/jobs/job1").status_code, 404)
            self.assertEqual(self.b.get("/api/download/jobs/job1/file").status_code, 404)
            self.assertEqual(self.b.delete("/api/download/jobs/job1").status_code, 404)
            self.assertEqual(self.b.get("/api/download/jobs").json()["items"], [])
            cancel.assert_not_called()
            self.assertEqual(self.a.get("/api/download/jobs/job1/file").content, b"alice's video")

    def test_a_list_holds_only_the_callers_rows(self) -> None:
        listing = self.own(db, "list_downloads", [self.record], [])
        self.assertEqual(self.b.get("/api/downloads").json()["items"], [])
        self.assertNotIn("Secret", self.b.get("/downloads").text)
        self.assertOnlyBob(listing)


class SharedFileTests(unittest.TestCase):
    """Identical bytes kept by two people are one file; deleting one copy keeps the other."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.enterContext(patch.dict(os.environ, {"OPENFEED_DOWNLOAD_DIR": str(self.folder)}))

    def _cursor(self, paths: list[str]) -> MagicMock:
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"path": path} for path in paths]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        return connection

    def test_an_identical_kept_file_is_reused_and_a_different_one_is_not(self) -> None:
        first = self.folder / "a.mp4"
        first.write_bytes(b"same bytes")
        other = self.folder / "b.mp4"
        other.write_bytes(b"different!")
        new = self.folder / "c.mp4"
        new.write_bytes(b"same bytes")
        with patch.object(db, "connect", return_value=self._cursor([str(other), str(first)])):
            self.assertEqual(downloads.identical_kept_file(new, VIDEO), first.resolve())
        with patch.object(db, "connect", return_value=self._cursor([str(other)])):
            self.assertIsNone(downloads.identical_kept_file(new, VIDEO))

    def test_a_file_outside_the_folder_is_never_reused(self) -> None:
        outside = Path(tempfile.mkdtemp()) / "x.mp4"
        outside.write_bytes(b"same bytes")
        new = self.folder / "c.mp4"
        new.write_bytes(b"same bytes")
        with patch.object(db, "connect", return_value=self._cursor([str(outside)])):
            self.assertIsNone(downloads.identical_kept_file(new, VIDEO))

    def test_deleting_one_owners_row_leaves_a_shared_file_in_place(self) -> None:
        shared = self.folder / "shared.mp4"
        shared.write_bytes(b"kept twice")

        class _Lock:
            def __init__(self, refs: list[str]) -> None:
                self.refs = refs

            def __enter__(self) -> list[str]:
                return self.refs

            def __exit__(self, *exc: object) -> None:
                return None

        with patch.object(db, "locked_download_paths", return_value=_Lock([str(shared)])):
            self.assertFalse(downloads.unlink_unreferenced_download(shared))
        self.assertTrue(shared.exists())
        with patch.object(db, "locked_download_paths", return_value=_Lock([])):
            self.assertTrue(downloads.unlink_unreferenced_download(shared))
        self.assertFalse(shared.exists())


class PersonalRecordTests(_TwoAccounts):
    def test_tokens_profiles_and_imports_are_per_account(self) -> None:
        revoke = self.own(db, "revoke_api_token", {"id": TOKEN_ID})
        self.assertEqual(self.b.delete(f"/api/tokens/{TOKEN_ID}").status_code, 404)
        self.assertOnlyBob(revoke)
        update = self.own(profiles, "update_profile", {"id": PROFILE})
        delete = self.own(profiles, "delete_profile", {"id": PROFILE, "user_key": f"{self.A}:p:{PROFILE}", "removed": {}})
        self.assertEqual(self.b.patch(f"/api/profiles/{PROFILE}", json={"name": "Mine"}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/profiles/{PROFILE}").status_code, 404)
        self.assertOnlyBob(update)
        self.assertOnlyBob(delete)
        # Choosing another account's profile id is refused and asks again.
        picked = self.b.post("/profiles/select", data={"profile_id": PROFILE, "next": "/"}, follow_redirects=False)
        self.assertTrue(picked.headers["location"].startswith("/profiles"))
        job = self.own(library_tools, "get_import", {"id": "job", "status": "done"})
        self.assertEqual(self.b.get("/api/playlists/import/job").status_code, 404)
        self.assertOnlyBob(job)

    def test_history_feedback_and_not_interested_change_only_the_callers_rows(self) -> None:
        one = self.own(db, "remove_history_video", 1, 0)
        many = self.own(library_tools, "remove_history_videos", 2, 0)
        liked = self.own(library_tools, "remove_liked_videos", 2, 0)
        undo = self.own(search_extras, "undo_feedback", True, False)
        clear = self.own(search_extras, "clear_not_interested", 1, 0)
        self.assertEqual(self.b.delete(f"/api/history/{VIDEO}").status_code, 404)
        self.assertEqual(self.b.post("/api/history/remove", json={"youtube_video_ids": [VIDEO]}).json()["removed"], 0)
        self.assertEqual(self.b.post("/api/liked/remove", json={"youtube_video_ids": [VIDEO]}).json()["removed"], 0)
        self.assertEqual(self.b.post("/api/feedback/undo", json={"youtube_video_id": VIDEO, "event_id": 7}).status_code, 404)
        self.assertEqual(self.b.delete(f"/api/not-interested/{VIDEO}").status_code, 404)
        for recorder in (one, many, liked, undo, clear):
            self.assertOnlyBob(recorder)

    def test_progress_votes_and_comment_reactions_are_the_callers(self) -> None:
        progress = self.own(db, "get_playback_progress", {"percent_completed": 0.5}, None)
        self.assertFalse(self.b.get(f"/api/playback/progress/{VIDEO}").json()["found"])
        self.assertOnlyBob(progress)
        vote = self.own(db, "set_poll_vote", 1, 1)
        self.b.post("/api/posts/Ugkx1234567890abcdefghijklmnopqrstu/vote", json={"choice_index": 1})
        self.assertOnlyBob(vote)
        reaction = self.own(db, "set_comment_reaction", "like", "like")
        event = self.own(db, "record_video_event", {}, {})
        self.b.post("/api/comments/reaction", json={"youtube_video_id": VIDEO, "comment_id": "c1", "reaction": "like"})
        self.assertOnlyBob(reaction)
        self.assertOnlyBob(event)

    def test_exports_imports_and_remote_saves_use_the_callers_key(self) -> None:
        export = self.own(dataexport, "build_export", {"secret": "alice"}, {"mine": True})
        opml = self.own(dataexport, "subscription_channels", [{"title": "Alice's channel"}], [])
        self.assertNotIn("alice", self.b.get("/api/export").text)
        self.assertNotIn("Alice", self.b.get("/api/export/subscriptions.opml").text)
        self.assertOnlyBob(export)
        self.assertOnlyBob(opml)
        subscribe = self.own(db, "subscribe", {"created": True}, {"created": True})
        with patch.object(db, "upsert_channel", return_value=1), patch.object(main.threading, "Thread"):
            opml_file = b'<?xml version="1.0"?><opml version="2.0"><body><outline type="rss" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv" text="x"/></body></opml>'
            self.b.post("/api/import/subscriptions.opml", files={"file": ("subs.opml", opml_file, "text/x-opml")})
        self.assertOnlyBob(subscribe)
        copy = self.own(main.channel_tools, "copy_into_new_playlist", {"id": PLAYLIST, "added": 1}, {"id": PLAYLIST, "added": 1})
        with patch.object(main.channel_tools, "remote_playlist", return_value=({"title": "Remote"}, [{"youtube_video_id": VIDEO}])):
            self.b.post("/api/remote-playlists/PLabcdefghijklmnop/save")
        self.assertOnlyBob(copy)
        takeout_clear = self.own(db, "clear_takeout_data", {"x": 1}, {})
        self.b.delete("/api/takeout")
        self.assertOnlyBob(takeout_clear)

    def test_the_bell_and_the_recommendations_feed_are_the_callers(self) -> None:
        self.b.get("/api/notifications")
        self.assertEqual({call.args[0] for call in self.notifications.call_args_list}, {self.B})
        feed = self.own(db, "list_recommended_videos", [{"youtube_video_id": VIDEO, "title": "Alice's pick"}], [])
        xml = self.b.get("/feeds/recommended.xml").text
        self.assertNotIn("Alice", xml)
        self.assertOnlyBob(feed)


class CatalogTests(_TwoAccounts):
    """The shared catalog is everything every account followed, searched and watched."""

    def test_a_member_lists_only_their_own_channels_and_uploads(self) -> None:
        catalog = [{"youtube_channel_id": "UCalice", "title": "Alice's niche"}, {"youtube_channel_id": "UCbob", "title": "Bob's"}]
        with patch.object(db, "list_channels", return_value=catalog), \
                patch.object(db, "list_subscriptions", side_effect=lambda limit, recent_first=False, *, user_key: [{"youtube_channel_id": "UCbob"}] if user_key == self.B else []), \
                patch.object(db, "list_recent_videos", return_value=[{"title": "Alice's niche video"}]) as recent, \
                patch.object(db, "list_subscription_uploads", return_value=[]) as uploads:
            channels = self.b.get("/api/channels").json()["items"]
            videos = self.b.get("/api/videos").json()["items"]
            self.assertEqual([row["youtube_channel_id"] for row in channels], ["UCbob"])
            self.assertEqual(videos, [])
            recent.assert_not_called()
            self.assertEqual(uploads.call_args.args[1], self.B)
            # An admin runs the server but is a person too: on a shared install they get their
            # own channels, since the stored list names what other accounts searched for.
            self.assertEqual(self.admin_client.get("/api/channels").json()["items"], [])


class CacheTests(_TwoAccounts):
    def test_watch_time_and_takeout_caches_are_per_user(self) -> None:
        db._WATCH_TIME_CACHE[self.A] = (time.time() + 60, {"channels": {"alice": 1.0}})
        db._TAKEOUT_CACHE[self.A] = (time.time() + 60, {"channels": {"alice": 1.0}})
        self.addCleanup(db._WATCH_TIME_CACHE.pop, self.A, None)
        self.addCleanup(db._TAKEOUT_CACHE.pop, self.A, None)
        with self.assertRaises(AssertionError):  # Bob's read goes to the database, not Alice's entry
            db.watch_time_profile(self.B)
        with self.assertRaises(AssertionError):
            db.takeout_profile(self.B)

    def test_the_ranker_never_hands_one_users_ranking_to_another(self) -> None:
        from openfeed import ranker

        ranker.clear_user_cache()
        self.addCleanup(ranker.clear_user_cache)
        index = MagicMock(version=1)
        with patch.object(ranker, "fingerprint", return_value=("same",)), \
                patch.object(ranker, "load_signals", side_effect=lambda cur, key: key), \
                patch.object(ranker, "prepare_user", side_effect=lambda idx, signals, key: MagicMock(key=key, who=signals)):
            alice = ranker._cached_entry(MagicMock(), index, self.A)
            bob = ranker._cached_entry(MagicMock(), index, self.B)
        self.assertEqual((alice.who, bob.who), (self.A, self.B))

    def test_search_suggestions_are_cached_per_account(self) -> None:
        search_extras._suggest_cache.clear()
        self.addCleanup(search_extras._suggest_cache.clear)
        response = MagicMock()
        response.json.return_value = ["rare medical question", ["rare medical question answers"]]
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = response
        with patch.object(search_extras.httpx, "Client", return_value=client):
            self.a.get("/api/search/suggestions?q=rare medical")
            self.b.get("/api/search/suggestions?q=rare medical")
            self.b.get("/api/search/suggestions?q=rare medical")
        # Alice's lookup did not warm Bob's cache (no instant answer that tells him what she typed).
        self.assertEqual(client.get.call_count, 2)


class AdminOnlyTests(_TwoAccounts):
    ROUTES = (
        ("POST", "/api/sync/rss"), ("GET", "/api/sync/runs"), ("POST", "/api/discover/related"),
        ("GET", "/api/enrich/status"), ("POST", "/api/enrich/channels"), ("POST", "/api/enrich/subscription-art"),
        ("POST", "/api/enrich/recent"), ("POST", "/api/recommendations/rebuild-vectors"),
        ("GET", "/api/recommendations/evaluate"), ("POST", "/api/ryd/refresh"), ("GET", "/api/playback/diagnose"),
        ("POST", "/api/playback/sources/test"), ("GET", "/api/status/full"), ("GET", "/api/status/panel"),
        ("GET", "/api/admin/settings"), ("PUT", "/api/admin/settings"),
    )

    def test_a_member_gets_403_on_every_install_wide_route(self) -> None:
        with patch.object(main, "sync_rss_once") as sync:
            for method, path in self.ROUTES:
                with self.subTest(path=path):
                    response = self.b.request(method, path, json={})
                    self.assertEqual(response.status_code, 403)
            sync.assert_not_called()

    def test_the_admin_gets_through(self) -> None:
        with patch.object(main, "sync_rss_once", return_value={"ok": True}):
            self.assertEqual(self.admin_client.post("/api/sync/rss").status_code, 200)
        self.assertEqual(self.admin_client.get("/api/admin/settings").status_code, 200)

    def test_admin_pages_are_403_for_a_member(self) -> None:
        for path in ("/admin/server", "/admin/status", "/admin/users"):
            self.assertEqual(self.b.get(path).status_code, 403)
        self.assertEqual(self.b.post("/admin/server", data={"section": "lookups"}).status_code, 403)
        self.assertEqual(self.b.post("/admin/server/run/sync").status_code, 403)

    def test_the_admin_changes_a_server_switch_for_everyone(self) -> None:
        page = self.admin_client.get("/admin/server")
        self.assertEqual(page.status_code, 200)
        for heading in ("Players from other sites", "Lookups this server makes", "Downloads", "Limits", "Maintenance"):
            self.assertIn(heading, page.text)
        saved = self.admin_client.post("/admin/server", data={"section": "lookups", "dearrow_enabled": "on"}, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(self.store.settings[instance.SETTING_KEY], {"sponsorblock_enabled": False, "dearrow_enabled": True, "ryd_enabled": False})
        self.assertFalse(main.get_settings().ryd_enabled)
        self.assertFalse(main.get_settings().sponsorblock_enabled)
        self.assertTrue(main.get_settings().dearrow_enabled)

    def test_a_stale_confirmation_is_asked_again_before_a_change(self) -> None:
        for session in self.store.sessions.values():
            session["reauth_at"] = time.time() - 3600
        response = self.admin_client.post("/admin/server", data={"section": "lookups"}, follow_redirects=False)
        self.assertTrue(response.headers["location"].startswith("/auth/confirm"))
        self.assertNotIn(instance.SETTING_KEY, self.store.settings)
        api = self.admin_client.put("/api/admin/settings", json={"ryd_enabled": False})
        self.assertEqual(api.json()["detail"], "reauth_required")

    def test_bad_values_are_refused_not_stored(self) -> None:
        for payload in ({"download_quota_mb": -1}, {"limits": {"search": "lots"}}, {"limits": {"nope": 1}}, {"unknown": 1},
                        {"public_instances": ["ftp://yewtu.be"]}, {"public_instances": ["https://user:pw@yewtu.be"]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.admin_client.put("/api/admin/settings", json=payload).status_code, 400)
        self.assertNotIn(instance.SETTING_KEY, self.store.settings)

    def test_a_demoted_admins_token_loses_admin_rights(self) -> None:
        record = {"id": TOKEN_ID, "user_key": self.root["user_key"], "token_hash": "", "read_only": False, "delegated_admin": True}
        with patch.object(main.tokens, "matches", return_value=True), patch.object(db, "find_api_token", return_value=record):
            headers = {"authorization": "Bearer of_abcdefgh_secretsecretsecret"}
            with patch.object(main.tokens, "split", return_value="abcdefgh"):
                self.assertEqual(TestClient(main.app).get("/api/admin/settings", headers=headers).status_code, 200)
                self.store.accounts[self.root["id"]]["role"] = "member"
                self.assertEqual(TestClient(main.app).get("/api/admin/settings", headers=headers).status_code, 403)

    def test_a_members_player_report_does_not_move_shared_source_health(self) -> None:
        with patch.object(main, "mark_source_result") as mark:
            self.b.post("/api/playback/report", json={"source_id": "invidious", "ok": False})
            mark.assert_not_called()
            self.admin_client.post("/api/playback/report", json={"source_id": "invidious", "ok": False})
            mark.assert_called_once_with("invidious", False)

    def test_a_members_refresh_does_not_run_install_wide_jobs(self) -> None:
        with patch.object(main.discovery, "grow_library") as grow, patch("openfeed.topics.rebuild_vectors") as vectors, \
                patch("openfeed.topics.profile_vector", return_value={}), patch.object(db, "takeout_profile", return_value={"channels": {}}):
            body = self.b.post("/api/feed/refresh").json()
        grow.assert_not_called()
        vectors.assert_not_called()
        self.assertEqual(body["install_wide"], "skipped: admin only")


class SettingsViewTests(_TwoAccounts):
    def test_a_member_sees_only_personal_settings(self) -> None:
        member = main._settings_dialog(main._current_user(self._request(self.b)), self.B)
        admin = main._settings_dialog(main._current_user(self._request(self.admin_client)), self.root["user_key"])
        for marker in ('data-settings-pane="status"', 'data-settings-pane="services"', "data-playback-test", "data-playback-diagnose",
                       "Required group", 'href="/admin/server"'):
            self.assertNotIn(marker, member)
            self.assertIn(marker, admin)
        for personal in ("playback", "content", "privacy", "integrations", "import", "api-tokens", "account"):
            self.assertIn(f'data-settings-pane="{personal}"', member)

    def test_the_account_menu_offers_admin_only_to_admins(self) -> None:
        with patch.object(db, "list_downloads", return_value=[]):
            self.assertNotIn('href="/admin/server"', self.b.get("/downloads").text)
            self.assertIn('href="/admin/server"', self.admin_client.get("/downloads").text)

    def _request(self, client: TestClient) -> Any:
        cookie = next(c.value for c in client.cookies.jar if c.name == main.get_settings().session_cookie)
        request = MagicMock()
        request.cookies = {main.get_settings().session_cookie: cookie}
        request.headers = {}
        request.scope = {}
        request.url.path = "/"
        return request


class DownloadRightsTests(_TwoAccounts):
    def test_members_may_be_kept_from_downloading_while_admins_may_not(self) -> None:
        instance.prime({"members_may_download": False})
        for method, path in (("GET", f"/api/download/{VIDEO}/formats"), ("GET", f"/api/download/{VIDEO}/info"), ("GET", f"/api/download/{VIDEO}"),
                             ("POST", f"/api/download/{VIDEO}/jobs")):
            with self.subTest(path=path):
                response = self.b.request(method, path, json={"height": 720, "destination": "server"})
                self.assertEqual(response.status_code, 403)
                self.assertIn("turned off for members", response.json()["detail"])
        with patch.object(main, "download_options", return_value={"formats": []}), patch.object(main.ytdlp, "available", return_value=False):
            self.assertEqual(self.admin_client.get(f"/api/download/{VIDEO}/formats").status_code, 200)
        self.assertNotIn("data-download-video", main._watch_options_menu(VIDEO, "", "", can_download=False))

    def test_a_full_quota_refuses_a_kept_download_but_not_one_to_the_device(self) -> None:
        instance.prime({"download_quota_mb": 1})
        with patch.object(downloads, "account_usage_bytes", return_value=2 * 1024 * 1024), \
                patch.object(main.ytdlp, "available", return_value=True), patch.object(db, "get_video_by_youtube_id", return_value={}), \
                patch.object(downloads.manager, "start", return_value=downloads.DownloadJob(id="j", youtube_video_id=VIDEO, height=720, container="mp4")) as start:
            refused = self.b.post(f"/api/download/{VIDEO}/jobs", json={"height": 720, "destination": "server"})
            self.assertEqual(refused.status_code, 403)
            self.assertIn("of your 1.0 MB", refused.json()["detail"])
            self.assertEqual(self.b.post(f"/api/download/{VIDEO}/jobs", json={"height": 720, "destination": "local"}).status_code, 200)
            self.assertEqual(self.admin_client.post(f"/api/download/{VIDEO}/jobs", json={"height": 720, "destination": "server"}).status_code, 200)
        self.assertEqual(start.call_args_list[0].kwargs["quota_bytes"], 0)  # local: no quota
        self.assertEqual(start.call_args_list[1].kwargs["quota_bytes"], 0)  # admin: none

    def test_a_finished_download_over_quota_is_not_kept(self) -> None:
        folder = Path(tempfile.mkdtemp())
        produced = folder / "job9.mp4"
        produced.write_bytes(b"x" * 2048)
        job = downloads.DownloadJob(id="job9", youtube_video_id=VIDEO, height=720, container="mp4", destination="server",
                                    user_key=self.B, quota_bytes=1024, quota_account=self.B)
        with patch.dict(os.environ, {"OPENFEED_DOWNLOAD_DIR": str(folder)}), \
                patch.object(downloads.ytdlp, "available", return_value=True), \
                patch.object(downloads.manager, "_command", return_value=["true"]), \
                patch.object(downloads, "account_usage_bytes", return_value=0), \
                patch.object(db, "register_download") as register, \
                patch.object(downloads, "unlink_unreferenced_download", side_effect=lambda path: path.unlink() or True):
            downloads.manager._run(job)
        register.assert_not_called()
        self.assertEqual(job.status, "error")
        self.assertIn("storage quota", job.error)
        self.assertFalse(produced.exists())

    def test_downloads_show_a_member_their_quota_not_the_server_disk(self) -> None:
        instance.prime({"download_quota_mb": 100})
        rows = [{"youtube_video_id": VIDEO, "title": "Mine", "size_bytes": 50 * 1024 * 1024, "download_id": 1}]
        with patch.object(db, "list_downloads", return_value=rows), patch.object(downloads, "account_usage_bytes", return_value=50 * 1024 * 1024), \
                patch.object(downloads, "disk_usage", return_value={"bytes": 9 * 1024 ** 3, "free_bytes": 1, "total_bytes": 10 * 1024 ** 3}):
            member = self.b.get("/downloads").text
            admin = self.admin_client.get("/downloads").text
        self.assertIn("50 MB of your 100 MB", member)
        self.assertNotIn("in downloads", member)
        self.assertIn("in downloads", admin)


class RateLimitTests(_TwoAccounts):
    def test_a_member_is_limited_with_a_clear_message_and_others_are_not(self) -> None:
        instance.prime({"limits": {"search": 2}})
        with patch.object(db, "search_videos", return_value=[]), patch.object(main.discovery, "search_youtube", return_value=[]), \
                patch.object(main, "_search_local_videos", return_value=[]), patch.object(main, "_search_channels", return_value=([], "", False)), \
                patch.object(db, "store_search_results", return_value=0):
            codes = [self.b.get("/api/search?q=cats").status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            refused = self.b.get("/api/search?q=cats")
            self.assertIn("searched a lot", refused.json()["detail"])
            self.assertTrue(int(refused.headers["retry-after"]) > 0)
            page = self.b.get("/search?q=cats")
            self.assertEqual(page.status_code, 429)
            self.assertIn("Slow down a little", page.text)
            # Alice has her own counter, and the admin has none.
            self.assertEqual(self.a.get("/api/search?q=cats").status_code, 200)
            self.assertEqual([self.admin_client.get("/api/search?q=cats").status_code for _ in range(4)], [200] * 4)

    def test_profiles_and_tokens_share_their_accounts_counter(self) -> None:
        self.assertEqual(policy.account_key({"user_key": f"{self.A}:p:{PROFILE}", "account_key": self.A}), self.A)
        self.assertEqual(policy.account_key({"user_key": f"{self.A}:p:{PROFILE}"}), self.A)

    def test_every_expensive_route_is_counted(self) -> None:
        for method, path, key in (
            ("GET", "/search", "search"), ("GET", "/api/search/html", "search"), ("POST", "/api/playlists/import", "playlist_import"),
            ("POST", "/api/remote-playlists/PLx/save", "playlist_import"), ("POST", "/api/takeout/import", "takeout_import"),
            ("POST", "/api/import/subscriptions.opml", "opml_import"), ("POST", f"/api/download/{VIDEO}/jobs", "downloads"),
            ("GET", f"/api/download/{VIDEO}", "downloads"), ("GET", f"/channel/UC{'a' * 22}", "invidious"),
            ("GET", "/playlist", "invidious"), ("GET", f"/api/comments/{VIDEO}", "invidious"), ("GET", f"/watch/{VIDEO}", "invidious"),
        ):
            with self.subTest(path=path):
                self.assertEqual(policy.limit_for(method, path).key, key)
        for method, path in (("GET", "/api/download/jobs"), ("GET", "/api/download/jobs/x"), ("GET", "/history"), ("GET", f"/watch/{VIDEO}/player")):
            self.assertIsNone(policy.limit_for(method, path))

    def test_zero_turns_a_limit_off(self) -> None:
        instance.prime({"limits": {"search": 0}})
        self.assertTrue(all(policy.check_limit(instance.LIMITS_BY_KEY["search"], self.B)[0] for _ in range(50)))


class SingleUserTests(unittest.TestCase):
    """Sign-in off: one user, who is the admin; nothing is limited or hidden."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "disabled"}, clear=True))
        instance.forget()
        self.addCleanup(instance.forget)
        self.enterContext(offline_db())
        self.enterContext(patch.object(main, "_notification_items", return_value=([], 0)))
        self.enterContext(patch.object(main, "_appearance_choice", return_value=""))
        self.enterContext(patch.object(main, "_rss_poller_status", return_value={"ok": False}))
        self.enterContext(patch.object(main, "_dearrow_body_attrs", return_value=""))
        self.enterContext(patch.object(profiles, "list_profiles", return_value=[]))
        for name, value in (("list_api_tokens", []), ("find_api_token", None)):
            self.enterContext(patch.object(db, name, return_value=value))
        self.client = TestClient(main.app)

    def test_the_one_user_runs_the_server(self) -> None:
        instance.prime({"limits": {"search": 1}})
        with patch.object(main, "sync_rss_once", return_value={"ok": True}):
            self.assertEqual(self.client.post("/api/sync/rss").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/settings").status_code, 200)
        page = self.client.get("/admin/server")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('href="/admin/users"', page.text.split("admin-tabs", 1)[1][:400])
        with patch.object(db, "search_videos", return_value=[]), patch.object(main.discovery, "search_youtube", return_value=[]):
            self.assertEqual([self.client.get("/api/search?q=x").status_code for _ in range(3)], [200] * 3)
        dialog = main._settings_dialog(main._current_user(None), "local")
        self.assertIn("data-playback-test", dialog)
        self.assertFalse(policy.is_member(main._current_user(None)))


class InstanceSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        instance.forget()
        self.addCleanup(instance.forget)

    def test_env_is_the_default_and_saved_values_override_it(self) -> None:
        with patch.dict(os.environ, {"OPENFEED_RYD_ENABLED": "false", "OPENFEED_PLAYBACK_FALLBACK": "youtube",
                                     "OPENFEED_RATE_LIMIT_SEARCH": "7"}, clear=True):
            self.assertFalse(main.get_settings().ryd_enabled)
            self.assertEqual(instance.limit_count("search"), 7)
            instance.prime({"ryd_enabled": True, "youtube_player": False, "limits": {"search": 9}})
            settings = main.get_settings()
            self.assertTrue(settings.ryd_enabled)
            self.assertFalse(settings.youtube_fallback_enabled)
            self.assertEqual(instance.limit_count("search"), 9)
            self.assertEqual(instance.limit_count("downloads"), instance.LIMITS_BY_KEY["downloads"].default)

    def test_an_unreadable_store_falls_back_to_the_environment(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"}, clear=True), \
                patch.object(instance, "_read_stored", side_effect=OSError("down")):
            self.assertEqual(instance.stored(), {})
            self.assertTrue(main.get_settings().sponsorblock_enabled)

    def test_a_stored_row_with_junk_keeps_only_valid_fields(self) -> None:
        self.assertEqual(instance.clean({"ryd_enabled": "yes", "limits": {"search": -4}, "evil": 1, "download_quota_mb": "x"}),
                         {"ryd_enabled": True})


if __name__ == "__main__":
    unittest.main()
