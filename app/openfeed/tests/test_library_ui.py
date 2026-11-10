"""History rows, playlist totals, the save picker's API and the thumbnail proxy."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from html import escape
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from openfeed import db, main


def _settings() -> SimpleNamespace:
    return SimpleNamespace(invidious_public_url="")


def _video(**overrides):
    video = {
        "youtube_video_id": "vid1",
        "youtube_channel_id": "UC123",
        "title": "Some Title",
        "channel_title": "Some Channel",
        "duration_seconds": 600,
    }
    video.update(overrides)
    return video


class WatchStateTests(unittest.TestCase):
    def test_partial_progress_is_stated_in_words_with_a_fraction(self) -> None:
        note, fraction, finished = main._watch_state(_video(furthest_seconds=150))
        self.assertEqual(note, "Watched 2:30 of 10:00")
        self.assertAlmostEqual(fraction, 0.25)
        self.assertFalse(finished)

    def test_the_resume_mark_wins_over_the_furthest_point(self) -> None:
        note, _, _ = main._watch_state(_video(resume_seconds=60, furthest_seconds=300))
        self.assertEqual(note, "Watched 1:00 of 10:00")

    def test_the_recorded_duration_wins_over_the_catalog_one(self) -> None:
        note, _, _ = main._watch_state(_video(furthest_seconds=30, progress_duration_seconds=60))
        self.assertEqual(note, "Watched 0:30 of 1:00")

    def test_near_the_end_counts_as_finished(self) -> None:
        self.assertEqual(main._watch_state(_video(furthest_seconds=590)), ("Finished", None, True))

    def test_the_stored_completed_flag_is_trusted(self) -> None:
        self.assertEqual(main._watch_state(_video(completed=True, furthest_seconds=10)), ("Finished", None, True))

    def test_a_marked_video_says_so_rather_than_claiming_it_was_finished(self) -> None:
        self.assertEqual(main._watch_state(_video(manually_marked_watched=True)), ("Marked watched", None, True))

    def test_an_unknown_duration_gets_no_fraction(self) -> None:
        self.assertEqual(main._watch_state(_video(duration_seconds=None, furthest_seconds=90)), ("Watched 1:30", None, False))

    def test_opening_a_video_is_not_watching_it(self) -> None:
        self.assertEqual(main._watch_state(_video()), ("Opened, not played", None, False))


class HistoryRowTests(unittest.TestCase):
    def test_a_partial_row_carries_the_resume_bar_and_no_finished_mark(self) -> None:
        html = main._history_row(_video(furthest_seconds=150), _settings())
        self.assertIn('class="progress-rail"', html)
        self.assertIn("width:25%", html)
        self.assertNotIn("row-finished", html)

    def test_a_finished_row_swaps_the_bar_for_the_mark(self) -> None:
        html = main._history_row(_video(completed=True), _settings())
        self.assertIn("row-finished", html)
        self.assertNotIn("progress-rail", html)

    def test_remove_is_on_the_row_and_in_the_menu(self) -> None:
        html = main._history_row(_video(), _settings())
        self.assertEqual(html.count('data-remove-history="vid1"'), 2)

    def test_the_activity_string_is_gone(self) -> None:
        html = main._history_row(_video(latest_event_at=datetime.now(timezone.utc)), _settings())
        self.assertNotIn("activity", html)

    def test_everything_interpolated_stays_escaped(self) -> None:
        evil = '"><script>alert(1)</script>'
        html = main._history_row(_video(title=evil, channel_title=evil, youtube_video_id="vid1"), _settings())
        self.assertNotIn("<script>", html)
        self.assertIn(f'data-filter-item="{escape(evil, quote=True)}', html)

    def test_days_read_as_words_then_dates(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(main._history_day_label(now), "Today")
        self.assertEqual(main._history_day_label(now - timedelta(days=1)), "Yesterday")
        self.assertRegex(main._history_day_label(now - timedelta(days=30)), r"^\d{1,2} \w{3} \d{4}$")
        self.assertEqual(main._history_day_label(None), "Earlier")

    def test_an_empty_history_says_how_it_fills_and_where_it_lives(self) -> None:
        html = main._history_section_html([], _settings())
        self.assertIn("empty-state", html)
        self.assertIn("Stored on this server only.", html)
        self.assertNotIn("data-filter-input", html)


class TotalsTests(unittest.TestCase):
    def test_runtime_reads_in_hours_and_minutes(self) -> None:
        self.assertEqual(main._runtime_text(11520), "3h 12m")
        self.assertEqual(main._runtime_text(2520), "42m")
        self.assertEqual(main._runtime_text(20), "1m")

    def test_no_known_durations_means_no_runtime(self) -> None:
        self.assertEqual(main._runtime_text(0, 0, 4), "")

    def test_a_partial_total_is_marked(self) -> None:
        self.assertEqual(main._runtime_text(7200, 3, 4), "2h 0m+")
        self.assertEqual(main._runtime_text(7200, 4, 4), "2h 0m")

    def test_an_empty_playlist_has_a_designed_cover_and_no_count_pill(self) -> None:
        html = main._cover_media("", 0)
        self.assertIn("No videos yet", html)
        self.assertNotIn("playlist-count", html)

    def test_a_cover_keeps_the_fallback_for_a_failed_image(self) -> None:
        html = main._cover_media("/vi/a/hqdefault.jpg", 3)
        self.assertIn("playlist-cover-empty", html)
        self.assertIn('onerror="this.remove()"', html)
        self.assertIn("playlist-count", html)

    def test_a_card_reports_runtime_and_keeps_its_hooks(self) -> None:
        html = main._playlist_card({"id": "p1", "name": "Mine", "video_count": 2, "runtime_seconds": 3600, "runtime_known": 2})
        self.assertIn("2 videos · 1h 0m", html)
        self.assertIn('data-edit-playlist="p1"', html)
        self.assertIn('data-delete-playlist="p1"', html)

    def test_a_smart_list_cannot_be_renamed_or_deleted(self) -> None:
        html = main._smart_list_card("Liked videos", "/liked", "like", 3, "3 videos", None)
        self.assertNotIn("data-edit-playlist", html)
        self.assertNotIn("data-delete-playlist", html)


class DownloadRowTests(unittest.TestCase):
    ROW = {"youtube_video_id": "vid1", "title": "T", "quality": "1080p", "size_bytes": 2_518_150}

    def test_the_quality_chip_replaces_the_trash_on_the_thumbnail(self) -> None:
        html = main._download_card(dict(self.ROW), _settings())
        self.assertIn('<span class="quality-chip">1080p</span>', html)
        self.assertNotIn("download-chip", html)

    def test_a_row_without_a_stored_path_is_not_called_missing(self) -> None:
        self.assertNotIn("no longer on disk", main._download_card(dict(self.ROW), _settings()))

    def test_a_row_whose_file_is_gone_says_so(self) -> None:
        html = main._download_card({**self.ROW, "path": "/nonexistent/file.mp4"}, _settings())
        self.assertIn("The file is no longer on disk", html)


class StorageStripTests(unittest.TestCase):
    def test_it_reports_only_what_the_filesystem_reported(self) -> None:
        usage = {"bytes": 2 * 1024**3, "free_bytes": 100 * 1024**3, "total_bytes": 200 * 1024**3}
        with patch.object(main.downloads, "disk_usage", return_value=usage):
            html = main._storage_strip()
        self.assertIn("2.0 GB in downloads", html)
        self.assertIn("100 GB free", html)
        self.assertIn("storage-bar", html)

    def test_no_disk_figures_means_no_bar_and_no_free_claim(self) -> None:
        with patch.object(main.downloads, "disk_usage", return_value={"bytes": 0, "free_bytes": None, "total_bytes": None}):
            html = main._storage_strip()
        self.assertNotIn("storage-bar", html)
        self.assertNotIn("free", html)


class LibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_removing_a_history_row_is_scoped_to_the_caller(self) -> None:
        with patch.object(main.db, "remove_history_video", return_value=2) as remove, \
             patch.object(main, "_user_key", return_value="owner-1"):
            response = self.client.delete("/api/history/vid1")
        self.assertEqual(response.status_code, 200)
        remove.assert_called_once_with("vid1", "owner-1")

    def test_removing_what_is_not_in_history_is_a_404(self) -> None:
        with patch.object(main.db, "remove_history_video", return_value=0):
            self.assertEqual(self.client.delete("/api/history/vid1").status_code, 404)

    def test_the_picker_asks_about_one_video_and_gets_a_proxied_cover(self) -> None:
        row = {"id": "p1", "name": "Mine", "has_video": True, "cover_youtube_video_id": "vid1", "cover_thumbnail_url": ""}
        with patch.object(main.db, "list_playlists", return_value=[row]) as listing, \
             patch.object(main, "_user_key", return_value="owner-1"):
            body = self.client.get("/api/playlists?video=vid1").json()
        listing.assert_called_once_with("owner-1", "vid1")
        self.assertEqual(body["items"][0]["cover_url"], "/vi/vid1/hqdefault.jpg")
        self.assertTrue(body["items"][0]["has_video"])

    def test_the_downloads_api_does_not_expose_server_paths(self) -> None:
        with patch.object(main.db, "list_downloads", return_value=[{"youtube_video_id": "a", "path": "/data/a.mp4"}]):
            body = self.client.get("/api/downloads").json()
        self.assertNotIn("path", body["items"][0])


class RemoveHistoryQueryTests(unittest.TestCase):
    def test_it_touches_only_this_owners_history_events_and_progress(self) -> None:
        cursor = MagicMock(rowcount=2)
        cursor.__enter__.return_value = cursor
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        with patch.object(db, "connect", return_value=connection):
            self.assertEqual(db.remove_history_video("vid1", "owner-1"), 2)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("event_type IN ('watch_page_open', 'mark_watched')", statements[0])
        self.assertIn("playback_progress", statements[1])
        for call in cursor.execute.call_args_list:
            self.assertEqual(call.args[1], ("vid1", "owner-1"))
        connection.commit.assert_called_once()


class ThumbnailProxyTests(unittest.TestCase):
    """Invidious sends a missing thumbnail as 404 headers and a body it never finishes."""

    def _proxy(self, status: int):
        upstream = MagicMock(status_code=status, headers=httpx.Headers({"content-type": "image/jpeg"}))
        client = MagicMock()
        client.send.return_value = upstream
        request = MagicMock(method="GET", headers={})
        request.url.query = ""
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_invidious_client", return_value=client), \
             patch.object(main, "get_settings", return_value=SimpleNamespace(invidious_base_url="http://inv")):
            return main.proxy_invidious_vi("abc/hqdefault.jpg", request), upstream

    def test_a_missing_thumbnail_is_answered_at_once(self) -> None:
        response, upstream = self._proxy(404)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.body, b"")
        upstream.close.assert_called_once()
        upstream.iter_bytes.assert_not_called()

    def test_a_real_thumbnail_still_streams(self) -> None:
        response, upstream = self._proxy(200)
        self.assertEqual(response.status_code, 200)
        upstream.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
