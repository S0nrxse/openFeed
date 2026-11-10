"""Library tools: History pause and clear, bulk edits, sort, playlist order and privacy,
YouTube playlist import, undo for deleted playlists and collections, Download again.

No database and no network: SQL is captured by a scripted cursor, Invidious is an
httpx.MockTransport, and page routes run against patched reads.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openfeed import db, library_tools as lt, main, profiles

PLAYLIST_ID = "11111111-1111-1111-1111-111111111111"
UNDO_ID = "22222222-2222-2222-2222-222222222222"


class _Cursor:
    """Records every statement; fetchone answers from a script, in order."""

    def __init__(self, answers: list[Any] | None = None, rowcount: int = 3) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.answers = list(answers or [])
        self.rowcount = rowcount

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((" ".join(query.split()), params))

    def fetchone(self) -> Any:
        return self.answers.pop(0) if self.answers else None

    def fetchall(self) -> list[Any]:
        answer = self.answers.pop(0) if self.answers else []
        return answer if isinstance(answer, list) else [answer]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _sql(callback: Any, answers: list[Any] | None = None) -> tuple[Any, _Cursor, _Connection]:
    cursor = _Cursor(answers)
    connection = _Connection(cursor)
    with patch.object(db, "connect", return_value=connection):
        result = callback()
    return result, cursor, connection


def _video(vid: str, **extra: Any) -> dict[str, Any]:
    return {"youtube_video_id": vid, "title": vid, "channel_title": "Chan", "youtube_channel_id": "UC1", **extra}


def _ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


class SortTests(unittest.TestCase):
    ROWS = [
        _video("b", title="banana", added_at=_ago(3), published_at=_ago(30), view_count=10, duration_seconds=300),
        _video("a", title="Apple", added_at=_ago(1), published_at=None, view_count=None, duration_seconds=60),
        _video("c", title="cherry", added_at=_ago(2), published_at=_ago(5), view_count=500, duration_seconds=None),
    ]

    def order(self, key: str) -> list[str]:
        return [row["youtube_video_id"] for row in lt.sort_playlist_videos(self.ROWS, key)]

    def test_manual_is_the_stored_order(self) -> None:
        self.assertEqual(self.order("manual"), ["b", "a", "c"])
        self.assertEqual(self.order("nonsense"), ["b", "a", "c"])

    def test_date_added_both_ways(self) -> None:
        self.assertEqual(self.order("added-newest"), ["a", "c", "b"])
        self.assertEqual(self.order("added-oldest"), ["b", "c", "a"])

    def test_unknown_values_go_last_whichever_way_it_runs(self) -> None:
        self.assertEqual(self.order("published-newest"), ["c", "b", "a"])
        self.assertEqual(self.order("published-oldest"), ["b", "c", "a"])
        self.assertEqual(self.order("popular"), ["c", "b", "a"])
        self.assertEqual(self.order("duration"), ["a", "b", "c"])

    def test_title_ignores_case(self) -> None:
        self.assertEqual(self.order("title"), ["a", "b", "c"])

    def test_clean_sort_falls_back_to_the_default(self) -> None:
        self.assertEqual(lt.clean_sort("OLDEST", lt.HISTORY_SORTS), "oldest")
        self.assertEqual(lt.clean_sort("", lt.LIKED_SORTS), "recent")
        self.assertEqual(lt.clean_sort("drop table", lt.PLAYLIST_SORTS), "manual")


class MoveTests(unittest.TestCase):
    ORDER = ["a", "b", "c", "d"]

    def test_moves(self) -> None:
        self.assertEqual(lt.apply_move(self.ORDER, "c", "top"), ["c", "a", "b", "d"])
        self.assertEqual(lt.apply_move(self.ORDER, "b", "bottom"), ["a", "c", "d", "b"])
        self.assertEqual(lt.apply_move(self.ORDER, "c", "up"), ["a", "c", "b", "d"])
        self.assertEqual(lt.apply_move(self.ORDER, "b", "down"), ["a", "c", "b", "d"])

    def test_the_ends_do_not_wrap(self) -> None:
        self.assertEqual(lt.apply_move(self.ORDER, "a", "up"), self.ORDER)
        self.assertEqual(lt.apply_move(self.ORDER, "d", "down"), self.ORDER)

    def test_a_drop_index_is_where_it_ends_up(self) -> None:
        self.assertEqual(lt.apply_move(self.ORDER, "d", index=1), ["a", "d", "b", "c"])
        self.assertEqual(lt.apply_move(self.ORDER, "a", index=99), ["b", "c", "d", "a"])
        self.assertEqual(lt.apply_move(self.ORDER, "c", index=-4), ["c", "a", "b", "d"])

    def test_unknown_video_or_move_changes_nothing(self) -> None:
        self.assertEqual(lt.apply_move(self.ORDER, "zz", "top"), self.ORDER)
        self.assertEqual(lt.apply_move(self.ORDER, "a", "sideways"), self.ORDER)

    def test_move_persists_the_whole_renumbered_order(self) -> None:
        with patch.object(lt, "playlist_order", return_value=list(self.ORDER)), \
             patch.object(lt, "set_playlist_order") as save:
            order = lt.move_playlist_item(PLAYLIST_ID, "d", "owner", "top")
        self.assertEqual(order, ["d", "a", "b", "c"])
        save.assert_called_once_with(PLAYLIST_ID, ["d", "a", "b", "c"], "owner")
        with patch.object(lt, "playlist_order", return_value=list(self.ORDER)):
            self.assertIsNone(lt.move_playlist_item(PLAYLIST_ID, "zz", "owner", "top"))

    def test_the_order_update_is_owner_scoped_and_numbered_from_one(self) -> None:
        _, cursor, _ = _sql(lambda: lt.set_playlist_order(PLAYLIST_ID, ["x", "y"], "owner"))
        query, params = cursor.calls[0]
        self.assertIn("WITH ORDINALITY", query)
        self.assertIn("p.user_key = %s", query)
        self.assertEqual(params, (["x", "y"], PLAYLIST_ID, "owner"))


class PlaylistIdTests(unittest.TestCase):
    def test_links_and_ids(self) -> None:
        pid = "PLBCF2DAC6FFB574DE"
        for text in (
            f"https://www.youtube.com/playlist?list={pid}",
            f"https://youtube.com/watch?v=dQw4w9WgXcQ&list={pid}&index=3",
            f"https://music.youtube.com/playlist?list={pid}",
            f"https://youtu.be/dQw4w9WgXcQ?list={pid}",
            f"www.youtube.com/playlist?list={pid}",
            f"  {pid} ",
        ):
            with self.subTest(text=text):
                self.assertEqual(lt.parse_playlist_id(text), pid)

    def test_refusals_say_why(self) -> None:
        cases = {
            "": "Paste",
            "https://example.com/playlist?x=1": "isn't a YouTube link",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "no playlist",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ": "Mix",
            "https://www.youtube.com/playlist?list=WL": "Google account",
            "LL": "Google account",
            "short": "doesn't look like",
        }
        for text, words in cases.items():
            with self.subTest(text=text), self.assertRaises(lt.ImportRefused) as caught:
                lt.parse_playlist_id(text)
            self.assertIn(words, str(caught.exception))


def _invidious(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _entry(index: int, vid: str | None = None) -> dict[str, Any]:
    vid = vid or f"vid{index:08d}"[:11]
    return {"type": "video", "title": f"Video {index}", "videoId": vid, "author": "Chan", "authorId": "UCchan", "index": index, "lengthSeconds": 60 + index}


class FetchPlaylistTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = SimpleNamespace(invidious_base_url="http://inv", enrichment_timeout_seconds=5)
        self.enterContext(patch.object(lt, "get_settings", return_value=settings))

    def test_overlapping_pages_are_stitched_by_index_in_order(self) -> None:
        total = 450
        pages_asked: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            pages_asked.append(page)
            # Invidious: page 1 is 0-199, page n starts 50 before (n-1)*100 and runs 200.
            start = 0 if page == 1 else (page - 1) * 100 - 50
            entries = [_entry(i, f"v{i:010d}") for i in range(start, min(total, start + 200))]
            return httpx.Response(200, json={"title": "Big", "author": "Chan", "videoCount": total, "description": "d", "videos": entries})

        progress: list[tuple[int, int]] = []
        result = lt.fetch_youtube_playlist("PLbig0000000", lambda done, all_: progress.append((done, all_)), client=_invidious(handler))
        ids = [video["youtube_video_id"] for video in result["videos"]]
        self.assertEqual(len(ids), total)
        self.assertEqual(ids, [f"v{i:010d}" for i in range(total)])
        self.assertEqual(result["title"], "Big")
        self.assertEqual(progress[-1], (total, total))
        self.assertEqual(pages_asked, [1, 2, 3, 4])

    def test_a_page_that_adds_nothing_ends_the_walk(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"title": "Liar", "videoCount": 5000, "videos": [_entry(0, "aaaaaaaaaaa"), _entry(1, "bbbbbbbbbbb")]})

        result = lt.fetch_youtube_playlist("PLliar000000", client=_invidious(handler))
        self.assertEqual([video["youtube_video_id"] for video in result["videos"]], ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_duplicates_and_deleted_entries_are_skipped(self) -> None:
        entries = [_entry(0, "aaaaaaaaaaa"), _entry(1, "aaaaaaaaaaa"),
                   {"type": "video", "title": "[Deleted video]", "videoId": "ccccccccccc", "index": 2}, _entry(3, "ddddddddddd"),
                   {"type": "video", "title": "", "videoId": "eeeeeeeeeee", "author": "", "authorId": "", "index": 4, "lengthSeconds": 0}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"title": "T", "videoCount": 4, "videos": entries})

        result = lt.fetch_youtube_playlist("PLdupes00000", client=_invidious(handler))
        self.assertEqual([video["youtube_video_id"] for video in result["videos"]], ["aaaaaaaaaaa", "ddddddddddd"])
        self.assertEqual(result["listed"], 5)

    def test_real_videos_without_an_uploader_are_kept(self) -> None:
        # YouTube's playlist pages often omit the channel; those are real videos (QA, 2026-11-09).
        entries = [{"type": "video", "title": "If You Only Watch One Wealth Video", "videoId": "Ys3o-iqGcoE",
                    "author": "", "authorId": "", "index": 0, "lengthSeconds": 600},
                   {"type": "video", "title": "[Private video]", "videoId": "ppppppppppp", "author": "", "authorId": "", "index": 1}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"title": "T", "videoCount": 2, "videos": entries})

        result = lt.fetch_youtube_playlist("PLnoauthor00", client=_invidious(handler))
        self.assertEqual([video["youtube_video_id"] for video in result["videos"]], ["Ys3o-iqGcoE"])

    def test_missing_and_private_playlists_are_explained(self) -> None:
        for status, body, words in (
            (404, {"error": "Playlist does not exist."}, "doesn't exist"),
            (500, {"error": "This playlist is private."}, "private"),
            (500, {"error": "Something odd"}, "Something odd"),
        ):
            with self.subTest(status=status), self.assertRaises(lt.ImportRefused) as caught:
                lt.fetch_youtube_playlist("PLgone000000", client=_invidious(lambda request, s=status, b=body: httpx.Response(s, json=b)))
            self.assertIn(words, str(caught.exception))


class ImportJobTests(unittest.TestCase):
    def test_a_finished_import_creates_the_playlist_in_youtube_order(self) -> None:
        fetched = {"title": "Talks", "description": "about", "video_count": 3, "listed": 3,
                   "videos": [_video("aaaaaaaaaaa"), _video("bbbbbbbbbbb"), _video("ccccccccccc")]}
        with patch.object(lt, "fetch_youtube_playlist", return_value=fetched), \
             patch.object(db, "upsert_discovered_videos", return_value=3) as upsert, \
             patch.object(lt, "create_playlist", return_value={"id": PLAYLIST_ID, "name": "Talks"}) as create, \
             patch.object(lt, "add_videos_to_playlist", return_value=2) as add:
            job = lt.start_import("https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE", "owner", background=False)
        self.assertEqual(job["status"], "done")
        self.assertEqual((job["playlist_id"], job["added"], job["skipped"]), (PLAYLIST_ID, 2, 1))
        self.assertNotIn("user_key", job)
        upsert.assert_called_once_with(fetched["videos"], "playlist_import")
        self.assertEqual(create.call_args.kwargs, {"visibility": "private", "source_playlist_id": "PLBCF2DAC6FFB574DE"})
        add.assert_called_once_with(PLAYLIST_ID, ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"], "owner")
        # Another profile cannot read the job.
        self.assertIsNone(lt.get_import(job["id"], "someone-else"))

    def test_a_failed_fetch_leaves_no_playlist(self) -> None:
        with patch.object(lt, "fetch_youtube_playlist", side_effect=lt.ImportRefused("That playlist is private on YouTube.")), \
             patch.object(lt, "create_playlist") as create:
            job = lt.start_import("PLBCF2DAC6FFB574DE", "owner", background=False)
        self.assertEqual((job["status"], job["error"]), ("error", "That playlist is private on YouTube."))
        create.assert_not_called()

    def test_a_bad_link_is_refused_before_a_job_exists(self) -> None:
        with self.assertRaises(lt.ImportRefused):
            lt.start_import("https://example.com", "owner", background=False)


class HistorySqlTests(unittest.TestCase):
    def test_clear_takes_history_events_and_progress_for_one_profile_only(self) -> None:
        with patch.object(lt, "forget_ranking_caches") as forget:
            result, cursor, connection = _sql(lambda: lt.clear_history("kid"))
        events, progress, provenance = cursor.calls
        self.assertIn("DELETE FROM user_video_events WHERE user_key = %s AND event_type = ANY(%s)", events[0])
        self.assertEqual(events[1], ("kid", ["watch_page_open", "mark_watched"]))
        self.assertEqual(progress, ("DELETE FROM playback_progress WHERE user_key = %s", ("kid",)))
        # What the shared catalog remembers of that browsing goes too (catalog_scope.py).
        self.assertIn("DELETE FROM video_provenance WHERE user_key = %s", provenance[0])
        self.assertNotIn("'playlist'", provenance[0])
        self.assertEqual(provenance[1], ("kid",))
        self.assertTrue(connection.committed)
        self.assertEqual(result, {"events": 3, "progress": 3})
        forget.assert_called_once_with("kid")

    def test_the_pause_flag_is_an_upsert_per_profile(self) -> None:
        result, cursor, _ = _sql(lambda: lt.set_history_paused("kid", True), [{"paused": True}])
        self.assertTrue(result)
        self.assertIn("ON CONFLICT (user_key)", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], ("kid", True, True))

    def test_reading_the_flag_without_a_database_says_not_paused(self) -> None:
        with patch.object(db, "connect", side_effect=RuntimeError("DATABASE_URL is not configured")):
            self.assertFalse(lt.history_paused("kid"))

    def test_the_flag_is_cached_briefly_and_a_change_replaces_it(self) -> None:
        lt._PAUSED_CACHE.pop("cached-kid", None)
        first, cursor, _ = _sql(lambda: lt.history_paused("cached-kid"), [{"paused": False}])
        self.assertFalse(first)
        with patch.object(db, "connect", side_effect=AssertionError("should be cached")):
            self.assertFalse(lt.history_paused("cached-kid"))
        _sql(lambda: lt.set_history_paused("cached-kid", True), [{"paused": True}])
        with patch.object(db, "connect", side_effect=AssertionError("should be cached")):
            self.assertTrue(lt.history_paused("cached-kid"))
        lt._PAUSED_CACHE.pop("cached-kid", None)

    def test_bulk_history_removal_is_scoped_and_deduplicated(self) -> None:
        with patch.object(lt, "forget_ranking_caches"):
            _, cursor, _ = _sql(lambda: lt.remove_history_videos(["a", "a", " b ", ""], "kid"))
        self.assertEqual(cursor.calls[0][1], (["a", "b"], "kid", ["watch_page_open", "mark_watched"]))
        self.assertIn("DELETE FROM playback_progress WHERE youtube_video_id = ANY(%s) AND user_key = %s", cursor.calls[1][0])

    def test_nothing_to_remove_touches_nothing(self) -> None:
        with patch.object(db, "connect") as connect:
            self.assertEqual(lt.remove_history_videos([], "kid"), 0)
            self.assertEqual(lt.remove_liked_videos(["", None], "kid"), 0)
        connect.assert_not_called()

    def test_unliking_clears_the_reaction_the_taps_and_the_star(self) -> None:
        with patch.object(lt, "forget_ranking_caches"):
            count, cursor, _ = _sql(
                lambda: lt.remove_liked_videos(["a", "b"], "kid"),
                [[{"youtube_video_id": "a"}], [{"youtube_video_id": "b"}], []],
            )
        reaction, taps, star = (call[0] for call in cursor.calls)
        self.assertIn("SET reaction = NULL", reaction)
        self.assertIn("event_type = 'more_like_this'", taps)
        self.assertIn("SET is_starred = false", star)
        self.assertEqual(count, 2)


class TrashSqlTests(unittest.TestCase):
    def test_delete_snapshots_then_deletes_and_returns_the_undo_id(self) -> None:
        undo, cursor, connection = _sql(lambda: lt.delete_playlist(PLAYLIST_ID, "owner"), [{"id": UNDO_ID}])
        snapshot, delete, purge = (call[0] for call in cursor.calls)
        self.assertIn("INSERT INTO library_trash", snapshot)
        self.assertIn("to_jsonb(p)", snapshot)
        self.assertEqual(cursor.calls[1], ("DELETE FROM playlists WHERE id = %s AND user_key = %s", (PLAYLIST_ID, "owner")))
        self.assertIn("interval '1 day'", purge)
        self.assertEqual(undo, UNDO_ID)
        self.assertTrue(connection.committed)

    def test_deleting_someone_elses_playlist_deletes_nothing(self) -> None:
        undo, cursor, _ = _sql(lambda: lt.delete_collection(PLAYLIST_ID, "owner"), [None])
        self.assertIsNone(undo)
        self.assertEqual(len(cursor.calls), 1)

    def test_restore_puts_the_row_and_its_items_back(self) -> None:
        restored, cursor, _ = _sql(
            lambda: lt.restore_deleted(UNDO_ID, "owner"),
            [{"id": UNDO_ID, "kind": "playlist", "item_id": PLAYLIST_ID}, {"id": PLAYLIST_ID}],
        )
        self.assertEqual(restored, {"kind": "playlist", "id": PLAYLIST_ID})
        statements = [call[0] for call in cursor.calls]
        self.assertIn("user_key = %s FOR UPDATE", statements[0])
        self.assertIn("jsonb_populate_record(NULL::playlists", statements[1])
        self.assertIn("JOIN videos v ON v.id = r.video_id", statements[2])
        self.assertEqual(statements[3], "DELETE FROM library_trash WHERE id = %s")

    def test_a_taken_id_or_name_rolls_back(self) -> None:
        restored, cursor, connection = _sql(
            lambda: lt.restore_deleted(UNDO_ID, "owner"),
            [{"id": UNDO_ID, "kind": "collection", "item_id": PLAYLIST_ID}, None],
        )
        self.assertIsNone(restored)
        self.assertTrue(connection.rolled_back)
        self.assertNotIn("DELETE FROM library_trash WHERE id = %s", [call[0] for call in cursor.calls])

    def test_new_tables_are_deleted_with_their_profile(self) -> None:
        self.assertIn("user_history_settings", profiles.USER_KEY_TABLES)
        self.assertIn("library_trash", profiles.USER_KEY_TABLES)


class PrivacySqlTests(unittest.TestCase):
    def test_a_viewer_sees_their_own_or_a_shared_playlist(self) -> None:
        _, cursor, _ = _sql(lambda: lt.get_viewable_playlist(PLAYLIST_ID, "viewer"))
        query, params = cursor.calls[0]
        self.assertIn("(p.user_key = %s OR p.visibility IN ('unlisted', 'public'))", query)
        self.assertEqual(params, ("viewer", PLAYLIST_ID, "viewer"))

    def test_only_public_playlists_of_others_are_listed(self) -> None:
        _, cursor, _ = _sql(lambda: lt.list_shared_playlists("viewer"))
        self.assertIn("p.visibility = 'public' AND p.user_key <> %s", cursor.calls[0][0])

    def test_owner_name_is_a_name_never_a_user_key(self) -> None:
        # Profile name, then the account's display name, then the users row's name unless
        # that row only holds the key itself (db._ensure_local_user_id names it so).
        for call in (lambda: lt.get_viewable_playlist(PLAYLIST_ID, "viewer"), lambda: lt.list_shared_playlists("viewer")):
            _, cursor, _ = _sql(call)
            query = cursor.calls[0][0]
            with self.subTest(query=query[:40]):
                self.assertIn("LEFT JOIN accounts acct ON acct.user_key = p.user_key", query)
                self.assertIn("COALESCE(NULLIF(BTRIM(MAX(ap.name)), ''), NULLIF(BTRIM(MAX(acct.display_name)), ''), "
                              "NULLIF(NULLIF(BTRIM(MAX(u.display_name)), ''), p.user_key)) AS owner_name", query)
                self.assertLess(query.index("MAX(ap.name)"), query.index("MAX(acct.display_name)"))
                self.assertLess(query.index("MAX(acct.display_name)"), query.index("MAX(u.display_name)"))

    def test_visibility_is_cleaned_before_it_is_stored(self) -> None:
        _, cursor, _ = _sql(lambda: lt.update_playlist_settings(PLAYLIST_ID, "owner", visibility="EVERYONE"))
        self.assertEqual(cursor.calls[0][1][0], "private")
        self.assertEqual(lt.clean_visibility("Unlisted"), "unlisted")
        self.assertEqual(lt.visibility_label("public"), "Public")


class LibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.enterContext(patch.object(main, "_user_key", return_value="owner"))

    def post(self, path: str, body: Any = None):
        return self.client.post(path, content=json.dumps(body or {}), headers={"content-type": "application/json"})

    def test_pause_and_clear(self) -> None:
        with patch.object(lt, "set_history_paused", return_value=True) as pause:
            self.assertTrue(self.post("/api/history/pause", {"paused": True}).json()["paused"])
        pause.assert_called_once_with("owner", True)
        with patch.object(lt, "clear_history", return_value={"events": 4, "progress": 2}) as clear:
            self.assertEqual(self.post("/api/history/clear").json()["removed"], {"events": 4, "progress": 2})
        clear.assert_called_once_with("owner")

    def test_bulk_removal_routes(self) -> None:
        with patch.object(lt, "remove_history_videos", return_value=2) as history:
            self.assertEqual(self.post("/api/history/remove", {"youtube_video_ids": ["a", "b"]}).json()["removed"], 2)
        history.assert_called_once_with(["a", "b"], "owner")
        with patch.object(lt, "remove_liked_videos", return_value=1) as liked:
            self.post("/api/liked/remove", {"youtube_video_ids": ["a"]})
        liked.assert_called_once_with(["a"], "owner")
        with patch.object(db, "get_playlist", return_value={"id": PLAYLIST_ID}), \
             patch.object(lt, "remove_playlist_items", return_value=1) as items:
            self.post(f"/api/playlists/{PLAYLIST_ID}/items/remove", {"youtube_video_ids": ["a"]})
        items.assert_called_once_with(PLAYLIST_ID, ["a"], "owner")
        self.assertEqual(self.post("/api/playlists/not-a-uuid/items/remove", {"youtube_video_ids": ["a"]}).status_code, 404)

    def test_removing_from_someone_elses_playlist_is_not_found(self) -> None:
        # Even a public one: the other write routes answer 404, and so does this one.
        with patch.object(db, "get_playlist", return_value=None) as owned, \
             patch.object(lt, "remove_playlist_items", return_value=0) as items:
            response = self.post(f"/api/playlists/{PLAYLIST_ID}/items/remove", {"youtube_video_ids": ["a"]})
        self.assertEqual(response.status_code, 404)
        owned.assert_called_once_with(PLAYLIST_ID, "owner")
        items.assert_not_called()

    def test_items_of_a_shared_playlist_are_readable_like_its_page(self) -> None:
        shared = {"id": PLAYLIST_ID, "name": "Road trip", "user_key": "u-df44fed09486b0692088", "visibility": "unlisted",
                  "is_owner": False, "owner_name": "Sam"}
        videos = [_video("a"), _video("b")]
        with patch.object(db, "get_playlist", return_value=None), \
             patch.object(lt, "get_viewable_playlist", return_value=shared) as viewable, \
             patch.object(db, "list_playlist_videos", return_value=videos) as listed:
            response = self.client.get(f"/api/playlists/{PLAYLIST_ID}/items")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        viewable.assert_called_once_with(PLAYLIST_ID, "owner")
        listed.assert_called_once_with(PLAYLIST_ID, 200, "u-df44fed09486b0692088")
        self.assertEqual([item["youtube_video_id"] for item in body["items"]], ["a", "b"])
        self.assertFalse(body["playlist"]["is_owner"])
        self.assertTrue(body["playlist"]["read_only"])
        self.assertEqual(body["playlist"]["owner_name"], "Sam")
        self.assertNotIn("user_key", body["playlist"], "the owner's internal key stays on the server")
        self.assertNotIn("u-df44fed09486b0692088", response.text)

    def test_items_of_a_private_or_missing_playlist_are_not_found(self) -> None:
        with patch.object(db, "get_playlist", return_value=None), \
             patch.object(lt, "get_viewable_playlist", return_value=None):
            self.assertEqual(self.client.get(f"/api/playlists/{PLAYLIST_ID}/items").status_code, 404)

    def test_items_of_an_owned_playlist(self) -> None:
        with patch.object(db, "get_playlist", return_value={"id": PLAYLIST_ID, "user_key": "owner"}), \
             patch.object(db, "list_playlist_videos", return_value=[_video("a")]) as listed:
            body = self.client.get(f"/api/playlists/{PLAYLIST_ID}/items?limit=5").json()
        listed.assert_called_once_with(PLAYLIST_ID, 5, "owner")
        self.assertTrue(body["playlist"]["is_owner"])
        self.assertFalse(body["playlist"]["read_only"])

    def test_bulk_add_needs_an_owned_playlist(self) -> None:
        with patch.object(db, "get_playlist", return_value=None):
            self.assertEqual(self.post(f"/api/playlists/{PLAYLIST_ID}/items/bulk", {"youtube_video_ids": ["a"]}).status_code, 404)
        with patch.object(db, "get_playlist", return_value={"id": PLAYLIST_ID}), \
             patch.object(lt, "add_videos_to_playlist", return_value=1) as add:
            self.assertEqual(self.post(f"/api/playlists/{PLAYLIST_ID}/items/bulk", {"youtube_video_ids": ["a", "b"]}).json()["added"], 1)
        add.assert_called_once_with(PLAYLIST_ID, ["a", "b"], "owner")

    def test_move_route(self) -> None:
        self.assertEqual(self.post(f"/api/playlists/{PLAYLIST_ID}/items/move", {"youtube_video_id": "a", "move": "left"}).status_code, 400)
        with patch.object(lt, "move_playlist_item", return_value=["b", "a"]) as move:
            body = self.post(f"/api/playlists/{PLAYLIST_ID}/items/move", {"youtube_video_id": "a", "index": 1}).json()
        self.assertEqual(body["order"], ["b", "a"])
        move.assert_called_once_with(PLAYLIST_ID, "a", "owner", "", 1)
        with patch.object(lt, "move_playlist_item", return_value=None):
            self.assertEqual(self.post(f"/api/playlists/{PLAYLIST_ID}/items/move", {"youtube_video_id": "z", "move": "top"}).status_code, 404)

    def test_settings_route(self) -> None:
        with patch.object(lt, "update_playlist_settings", return_value={"id": PLAYLIST_ID, "visibility": "unlisted"}) as update:
            self.assertEqual(self.post(f"/api/playlists/{PLAYLIST_ID}/settings", {"visibility": "unlisted"}).status_code, 200)
        update.assert_called_once_with(PLAYLIST_ID, "owner", visibility="unlisted", sort_key=None)

    def test_import_routes(self) -> None:
        self.assertIn("Mix", self.post("/api/playlists/import", {"url": "https://youtube.com/watch?v=x&list=RDxxxxxxxxxxxx"}).json()["detail"])
        with patch.object(lt, "start_import", return_value={"id": "job1", "status": "queued"}) as start:
            self.assertEqual(self.post("/api/playlists/import", {"url": "PLBCF2DAC6FFB574DE", "name": "N"}).json()["job"]["id"], "job1")
        start.assert_called_once_with("PLBCF2DAC6FFB574DE", "owner", "N", "private")
        self.assertEqual(self.client.get("/api/playlists/import/nope").status_code, 404)

    def test_delete_returns_an_undo_id_and_restore_uses_it(self) -> None:
        with patch.object(lt, "delete_playlist", return_value=UNDO_ID) as delete:
            body = self.client.delete(f"/api/playlists/{PLAYLIST_ID}").json()
        self.assertEqual(body["undo_id"], UNDO_ID)
        delete.assert_called_once_with(PLAYLIST_ID, "owner")
        with patch.object(lt, "delete_collection", return_value=UNDO_ID):
            self.assertEqual(self.client.delete(f"/api/subscription-collections/{PLAYLIST_ID}").json()["undo_id"], UNDO_ID)
        with patch.object(lt, "delete_playlist", return_value=None):
            self.assertEqual(self.client.delete(f"/api/playlists/{PLAYLIST_ID}").status_code, 404)
        with patch.object(lt, "restore_deleted", return_value={"kind": "playlist", "id": PLAYLIST_ID}) as restore:
            self.assertEqual(self.post(f"/api/library/restore/{UNDO_ID}").json()["id"], PLAYLIST_ID)
        restore.assert_called_once_with(UNDO_ID, "owner")
        with patch.object(lt, "restore_deleted", return_value=None):
            self.assertEqual(self.post(f"/api/library/restore/{UNDO_ID}").status_code, 409)

    def test_creating_a_shared_playlist_sets_its_visibility(self) -> None:
        with patch.object(db, "create_playlist", return_value={"id": PLAYLIST_ID, "visibility": "private"}), \
             patch.object(lt, "update_playlist_settings", return_value={"id": PLAYLIST_ID, "visibility": "public"}) as update:
            body = self.post("/api/playlists", {"name": "Mine", "visibility": "public"}).json()
        self.assertEqual(body["playlist"]["visibility"], "public")
        update.assert_called_once_with(PLAYLIST_ID, "owner", visibility="public")


class PausedHistoryTests(unittest.TestCase):
    """Paused: no history event, no resume point, no watch time. Explicit actions still work."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.enterContext(patch.object(main, "_user_key", return_value="owner"))
        self.enterContext(patch.object(lt, "history_paused", return_value=True))

    def test_opening_a_video_records_nothing(self) -> None:
        with patch.object(db, "record_video_event") as record, patch.object(db, "update_playback_progress") as progress:
            body = self.client.post("/api/feedback", json={"youtube_video_id": "vid", "event_type": "watch_page_open"}).json()
        self.assertTrue(body["paused"])
        record.assert_not_called()
        progress.assert_not_called()

    def test_progress_reports_are_dropped(self) -> None:
        with patch.object(db, "update_playback_progress") as progress:
            body = self.client.post("/api/playback/progress", json={"youtube_video_id": "vid", "current_seconds": 30, "watch_delta_seconds": 10}).json()
        self.assertEqual(body, {"ok": True, "paused": True, "progress": None})
        progress.assert_not_called()

    def test_a_like_is_still_a_like(self) -> None:
        with patch.object(db, "record_video_event", return_value={"id": 1}) as record, \
             patch.object(db, "set_video_feedback", return_value={"reaction": "like"}):
            self.assertEqual(self.client.post("/api/feedback", json={"youtube_video_id": "vid", "event_type": "like"}).status_code, 200)
        record.assert_called_once()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(invidious_public_url="")


class RenderTests(unittest.TestCase):
    def test_history_controls_and_banner(self) -> None:
        self.assertIn("Pause watch history", main._history_controls(False))
        paused = main._history_controls(True)
        self.assertIn("Turn on watch history", paused)
        self.assertIn('data-history-pause="off"', paused)
        self.assertIn("Clear all watch history", paused)
        self.assertIn("Watch history is paused", main._history_paused_banner())

    def test_history_page_shows_tools_and_pause_state(self) -> None:
        request = SimpleNamespace(query_params={}, headers={}, cookies={}, url=SimpleNamespace(path="/history", query=""))
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="owner"), \
             patch.object(main, "get_settings", return_value=_settings()), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: content), \
             patch.object(db, "list_history_videos", return_value=[_video("aaaaaaaaaaa", latest_event_at=_ago(0))]) as listing, \
             patch.object(lt, "history_paused", return_value=True):
            html = main.history_page(request, sort="oldest")
        listing.assert_called_once_with(121, "owner", oldest=True)
        self.assertIn("Watch history is paused", html)
        self.assertIn('data-select-kind="history"', html)
        self.assertIn("data-select-toggle", html)
        self.assertIn('aria-label="Sort by: Oldest watched"', html)

    def test_sort_menu_marks_the_current_order(self) -> None:
        html = main._sort_menu(lt.LIKED_SORTS, "oldest", lambda key: f"/liked?sort={key}")
        self.assertIn('href="/liked?sort=oldest" role="menuitem" aria-current="true"', html)
        self.assertEqual(html.count('role="menuitem"'), 2)

    def test_a_missing_download_offers_download_again(self) -> None:
        row = {"youtube_video_id": "vid1", "title": "T", "quality": "720p", "height": 720, "container": "mkv", "path": "/nonexistent/x.mkv"}
        html = main._download_card(row, _settings())
        self.assertIn("File missing", html)
        self.assertIn('data-redownload="vid1" data-height="720" data-container="mkv"', html)
        self.assertEqual(html.count("data-redownload="), 2)  # the row button and the sheet row
        present = main._download_card({k: v for k, v in row.items() if k != "path"}, _settings())
        self.assertNotIn("data-redownload", present)

    def test_playlist_cards_say_their_visibility_and_shared_ones_have_no_edit_menu(self) -> None:
        mine = main._playlist_card({"id": PLAYLIST_ID, "name": "Mine", "video_count": 2, "visibility": "unlisted"})
        self.assertIn("2 videos · Unlisted", mine)
        self.assertIn('data-playlist-visibility="unlisted"', mine)
        theirs = main._playlist_card({"id": PLAYLIST_ID, "name": "Theirs", "video_count": 2, "owner_name": "Sam"}, owner_view=False)
        self.assertIn("by Sam · 2 videos", theirs)
        self.assertNotIn("data-delete-playlist", theirs)

    def test_the_visibility_switch_offers_youtubes_three(self) -> None:
        html = main._visibility_menu(PLAYLIST_ID, "public")
        for key in ("public", "unlisted", "private"):
            self.assertIn(f'data-playlist-visibility="{key}"', html)
        self.assertIn("Anyone signed in to this OpenFeed with the link can view", html)
        self.assertIn(f'data-copy-url="/playlist/{PLAYLIST_ID}"', html)

    def _playlist_page(self, playlist: dict[str, Any], videos: list[dict[str, Any]], sort: str = "") -> str:
        request = SimpleNamespace(query_params={}, headers={"accept": "text/html"}, cookies={}, url=SimpleNamespace(path="/playlist", query=""))
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="viewer"), \
             patch.object(main, "get_settings", return_value=_settings()), \
             patch.object(main, "_app_shell", side_effect=lambda title, active, content, **_: content), \
             patch.object(main, "_viewable_playlist", return_value=playlist), \
             patch.object(db, "list_playlist_videos", return_value=videos) as listing:
            html = main.playlist_page(request, PLAYLIST_ID, sort)
        listing.assert_called_once_with(PLAYLIST_ID, 300, playlist.get("user_key") or "viewer")
        return html

    def test_the_owner_can_reorder_in_manual_order(self) -> None:
        videos = [_video("aaaaaaaaaaa", title="b"), _video("bbbbbbbbbbb", title="a")]
        html = self._playlist_page({"id": PLAYLIST_ID, "name": "P", "is_owner": True, "user_key": "viewer", "visibility": "private"}, videos)
        self.assertIn("data-reorder-list", html)
        self.assertEqual(html.count("data-drag-handle"), 2)
        self.assertIn('data-playlist-move="top"', html)
        self.assertIn("data-delete-playlist", html)
        self.assertIn('data-playlist-sort="title"', html)
        sorted_html = self._playlist_page({"id": PLAYLIST_ID, "name": "P", "is_owner": True, "user_key": "viewer"}, videos, "title")
        self.assertNotIn("data-drag-handle", sorted_html)
        self.assertLess(sorted_html.index('data-video-id="bbbbbbbbbbb"'), sorted_html.index('data-video-id="aaaaaaaaaaa"'))

    def test_a_shared_playlist_is_read_only_for_another_profile(self) -> None:
        videos = [_video("aaaaaaaaaaa"), _video("bbbbbbbbbbb")]
        html = self._playlist_page({"id": PLAYLIST_ID, "name": "P", "is_owner": False, "user_key": "owner", "owner_name": "Sam", "visibility": "unlisted"}, videos)
        for hook in ("data-drag-handle", "data-delete-playlist", "data-edit-playlist", "data-remove-playlist-item", "data-playlist-visibility", "data-playlist-sort"):
            self.assertNotIn(hook, html)
        self.assertIn("by Sam", html)
        self.assertIn("data-select-readonly", html)
        self.assertIn("data-list-play", html)

    def test_dialogs_carry_visibility_and_import(self) -> None:
        html = main._playlist_dialogs()
        self.assertIn('<select class="field-input" name="visibility">', html)
        self.assertIn("data-open-playlist-import", html)
        self.assertIn('id="playlist-import-dialog"', html)
        self.assertNotIn("data-import-progress", html)  # the Takeout import owns that hook

    def test_the_shell_script_deletes_with_undo_and_bundles_the_tools(self) -> None:
        script = main._home_script()
        self.assertIn("window.ofDeleteWithUndo = async", script)
        self.assertEqual(script.count("await window.ofDeleteWithUndo("), 2)
        self.assertNotIn("Delete this playlist?", script)
        self.assertIn("'/api/history/clear'", script)
        self.assertIn(".library-toolbar", main._home_styles())

    def test_watch_lists_play_a_playlist_in_its_chosen_sort(self) -> None:
        videos = [_video("aaaaaaaaaaa", title="zeta"), _video("bbbbbbbbbbb", title="alpha")]
        with patch.object(main, "_viewable_playlist", return_value={"name": "P", "sort_key": "title", "user_key": "owner"}), \
             patch.object(db, "list_playlist_videos", return_value=videos) as listing:
            resolved = main._resolve_watch_list(PLAYLIST_ID, "viewer")
        listing.assert_called_once_with(PLAYLIST_ID, main.WATCH_LIST_LIMIT, "owner")
        self.assertEqual([video["youtube_video_id"] for video in resolved["videos"]], ["bbbbbbbbbbb", "aaaaaaaaaaa"])


class VisibilityHandlerTests(unittest.TestCase):
    """The Edit button carries data-playlist-visibility too (to prefill the dialog), so the
    visibility menu's click handler must not match it. It did, which sent
    POST /api/playlists/undefined/settings and a "Could not change the visibility" toast on
    every Edit (QA, 2026-11-09)."""

    def test_only_menu_rows_with_a_playlist_id_change_visibility(self) -> None:
        from pathlib import Path
        script = (Path(main.__file__).parent / "assets" / "js" / "ui-library-tools.js").read_text()
        self.assertIn("closest('[data-playlist-visibility][data-playlist-id]')", script)
        self.assertNotIn("closest('[data-playlist-visibility]')", script)

    def test_the_edit_button_has_no_playlist_id_attribute(self) -> None:
        import re
        from pathlib import Path
        source = Path(main.__file__).read_text()
        edits = re.findall(r"data-edit-playlist=[^>]*", source)
        self.assertTrue(edits)
        for attrs in edits:
            self.assertNotIn("data-playlist-id=", attrs)



if __name__ == "__main__":
    unittest.main()
