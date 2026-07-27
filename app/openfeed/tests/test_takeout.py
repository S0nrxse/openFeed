from __future__ import annotations

import io
import json
import unittest
import zipfile

from openfeed import takeout

ROOT = "Takeout/YouTube and YouTube Music"


def _archive(**files: bytes | str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            path = name.replace("__", "/")
            archive.writestr(path, payload if isinstance(payload, bytes) else payload.encode("utf-8"))
    return buffer.getvalue()


def _watch(entries: list[dict]) -> str:
    return json.dumps(entries)


def _entry(video_id: str, channel_id: str = "UC" + "a" * 22, channel: str = "Chan", when: str = "2025-01-02T03:04:05Z") -> dict:
    return {
        "header": "YouTube",
        "title": f"Watched something {video_id}",
        "titleUrl": f"https://www.youtube.com/watch?v={video_id}",
        "subtitles": [{"name": channel, "url": f"https://www.youtube.com/channel/{channel_id}"}],
        "time": when,
    }


class SubscriptionParsingTests(unittest.TestCase):
    CSV = "Channel ID,Channel URL,Channel title\nUCaaaaaaaaaaaaaaaaaaaaaa,http://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa,Veritasium\n"

    def test_channels_are_read(self) -> None:
        data = _archive(**{f"{ROOT}/subscriptions/subscriptions.csv".replace("/", "__"): self.CSV})
        rows = takeout.inspect(data)["subscriptions"]
        self.assertEqual(rows, [{"youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa", "title": "Veritasium"}])

    def test_localised_headers_still_work(self) -> None:
        # Takeout translates its column headers, so columns are found by shape.
        csv_text = "Identifiant,Adresse,Titre\nUCaaaaaaaaaaaaaaaaaaaaaa,http://youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa,Chaîne\n"
        data = _archive(**{f"{ROOT}/abonnements/subscriptions.csv".replace("/", "__"): csv_text})
        rows = takeout.inspect(data)["subscriptions"]
        self.assertEqual(rows[0]["youtube_channel_id"], "UCaaaaaaaaaaaaaaaaaaaaaa")

    def test_rows_without_a_channel_id_are_skipped(self) -> None:
        csv_text = "Channel ID,Channel URL,Channel title\n,,\nUCbbbbbbbbbbbbbbbbbbbbbb,,Real\n"
        data = _archive(**{f"{ROOT}/subscriptions/subscriptions.csv".replace("/", "__"): csv_text})
        self.assertEqual(len(takeout.inspect(data)["subscriptions"]), 1)


class WatchHistoryTests(unittest.TestCase):
    def _parsed(self, entries: list[dict]) -> dict:
        return takeout.inspect(_archive(**{f"{ROOT}/history/watch-history.json".replace("/", "__"): _watch(entries)}))

    def test_video_ids_and_channels_are_extracted(self) -> None:
        parsed = self._parsed([_entry("dQw4w9WgXcQ")])
        self.assertEqual(parsed["watched_videos"][0]["youtube_video_id"], "dQw4w9WgXcQ")
        self.assertEqual(parsed["watched_channels"][0]["watch_count"], 1)

    def test_repeat_views_raise_the_channel_count_but_not_the_video_list(self) -> None:
        parsed = self._parsed([_entry("dQw4w9WgXcQ"), _entry("dQw4w9WgXcQ"), _entry("CASfTzI_ca4")])
        self.assertEqual(len(parsed["watched_videos"]), 2)
        self.assertEqual(parsed["watched_channels"][0]["watch_count"], 3)

    def test_channels_come_back_most_watched_first(self) -> None:
        entries = [_entry("aaaaaaaaaaa", "UC" + "b" * 22, "Rare")]
        entries += [_entry(f"vid{i:08d}", "UC" + "c" * 22, "Often") for i in range(4)]
        channels = self._parsed(entries)["watched_channels"]
        self.assertEqual(channels[0]["title"], "Often")

    def test_shorts_urls_are_recognised(self) -> None:
        entry = _entry("dQw4w9WgXcQ")
        entry["titleUrl"] = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        self.assertEqual(self._parsed([entry])["watched_videos"][0]["youtube_video_id"], "dQw4w9WgXcQ")

    def test_entries_without_a_video_are_ignored(self) -> None:
        parsed = self._parsed([{"header": "YouTube", "title": "Visited the homepage", "titleUrl": "https://www.youtube.com/"}, _entry("dQw4w9WgXcQ")])
        self.assertEqual(len(parsed["watched_videos"]), 1)
        self.assertEqual(parsed["watch_entries_seen"], 2)

    def test_a_removed_video_with_no_channel_still_counts_as_watched(self) -> None:
        entry = _entry("dQw4w9WgXcQ")
        del entry["subtitles"]
        parsed = self._parsed([entry])
        self.assertEqual(len(parsed["watched_videos"]), 1)
        self.assertEqual(parsed["watched_channels"], [])

    def test_timestamps_are_parsed(self) -> None:
        parsed = self._parsed([_entry("dQw4w9WgXcQ", when="2025-01-02T03:04:05Z")])
        self.assertEqual(parsed["watched_videos"][0]["watched_at"].year, 2025)

    def test_an_unparseable_timestamp_does_not_lose_the_entry(self) -> None:
        parsed = self._parsed([_entry("dQw4w9WgXcQ", when="not a date")])
        self.assertEqual(len(parsed["watched_videos"]), 1)
        self.assertIsNone(parsed["watched_videos"][0]["watched_at"])

    def test_the_video_list_is_capped(self) -> None:
        entries = [_entry(f"v{i:010d}") for i in range(takeout.MAX_WATCH_ENTRIES + 50)]
        parsed = self._parsed(entries)
        self.assertEqual(len(parsed["watched_videos"]), takeout.MAX_WATCH_ENTRIES)
        # The channel tally still sees everything, so affinity is not distorted by the cap.
        self.assertEqual(parsed["watched_channels"][0]["watch_count"], takeout.MAX_WATCH_ENTRIES + 50)


class SearchHistoryTests(unittest.TestCase):
    def test_queries_are_extracted_most_frequent_first(self) -> None:
        entries = [{"header": "YouTube", "title": "Searched for rust borrow checker"}] * 3
        entries += [{"header": "YouTube", "title": "Searched for sourdough"}]
        data = _archive(**{f"{ROOT}/history/search-history.json".replace("/", "__"): json.dumps(entries)})
        self.assertEqual(takeout.inspect(data)["search_terms"][0], "rust borrow checker")


class ErrorTests(unittest.TestCase):
    def test_a_non_zip_upload_says_so_plainly(self) -> None:
        with self.assertRaises(takeout.TakeoutError) as caught:
            takeout.inspect(b"this is not a zip")
        self.assertIn(".zip", str(caught.exception))

    def test_an_html_export_explains_the_fix(self) -> None:
        # The single most common mistake: Takeout defaults History to HTML.
        data = _archive(**{f"{ROOT}/history/watch-history.html".replace("/", "__"): "<html></html>"})
        with self.assertRaises(takeout.TakeoutError) as caught:
            takeout.inspect(data)
        self.assertIn("JSON", str(caught.exception))

    def test_an_archive_with_no_youtube_data_is_rejected(self) -> None:
        with self.assertRaises(takeout.TakeoutError) as caught:
            takeout.inspect(_archive(**{"Takeout__Chrome__Bookmarks.html": "<html></html>"}))
        self.assertIn("No YouTube data", str(caught.exception))

    def test_an_oversized_archive_is_refused_before_parsing(self) -> None:
        with self.assertRaises(takeout.TakeoutError) as caught:
            takeout.inspect(b"x" * (takeout.MAX_ARCHIVE_BYTES + 1))
        self.assertIn("2 GB", str(caught.exception))


class SummaryTests(unittest.TestCase):
    def test_the_summary_counts_each_category(self) -> None:
        data = _archive(**{
            f"{ROOT}/history/watch-history.json".replace("/", "__"): _watch([_entry("dQw4w9WgXcQ"), _entry("CASfTzI_ca4")]),
            f"{ROOT}/subscriptions/subscriptions.csv".replace("/", "__"): "Channel ID,URL,Title\nUCaaaaaaaaaaaaaaaaaaaaaa,,Veritasium\n",
        })
        summary = takeout.summarize(takeout.inspect(data))
        self.assertEqual(summary["watched_videos"], 2)
        self.assertEqual(summary["subscriptions"], 1)
        self.assertEqual(summary["top_channels"][0]["watch_count"], 2)


if __name__ == "__main__":
    unittest.main()
