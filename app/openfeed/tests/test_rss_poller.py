from __future__ import annotations

import contextlib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from openfeed import rss
from openfeed.rss import ChannelFeedResult, channel_feed_url, fetch_channel_feed, parse_channel_feed

CHANNEL_ID = "UC" + "a" * 22
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

FEED_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
  <link rel="self" href="{FEED_URL}"/>
  <id>yt:channel:{CHANNEL_ID}</id>
  <yt:channelId>{CHANNEL_ID}</yt:channelId>
  <title>Some Channel</title>
  <link rel="alternate" href="https://www.youtube.com/channel/{CHANNEL_ID}"/>
  <author>
    <name>Some Channel</name>
    <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
  </author>
  <published>2025-01-01T00:00:00+00:00</published>
  <entry>
    <id>yt:video:dQw4w9WgXcQ</id>
    <yt:videoId>dQw4w9WgXcQ</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>First video title</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"/>
    <author>
      <name>Some Channel</name>
      <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
    </author>
    <published>2025-01-02T03:04:05+00:00</published>
    <updated>2025-01-03T00:00:00+00:00</updated>
    <media:group>
      <media:title>First video title</media:title>
      <media:content url="https://www.youtube.com/v/dQw4w9WgXcQ?version=3" type="application/x-shockwave-flash" width="640" height="390"/>
      <media:thumbnail url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" width="480" height="360"/>
      <media:description>First video description here.</media:description>
      <media:community>
        <media:starRating count="10" average="5.00" min="1" max="5"/>
        <media:statistics views="12345"/>
      </media:community>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:9bZkp7q19f0</id>
    <yt:videoId>9bZkp7q19f0</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>Second video title</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=9bZkp7q19f0"/>
    <author>
      <name>Some Channel</name>
      <uri>https://www.youtube.com/channel/{CHANNEL_ID}</uri>
    </author>
    <published>2025-02-03T04:05:06Z</published>
    <updated>2025-02-04T00:00:00+00:00</updated>
    <media:group>
      <media:title>Second video title</media:title>
      <media:thumbnail url="https://i.ytimg.com/vi/9bZkp7q19f0/hqdefault.jpg" width="480" height="360"/>
      <media:community>
        <media:statistics views="678"/>
      </media:community>
    </media:group>
  </entry>
</feed>
"""

EXPECTED_ENTRY_KEYS = {
    "id",
    "youtube_video_id",
    "youtube_channel_id",
    "subscription_id",
    "channel_title",
    "feed_url",
    "title",
    "url",
    "thumbnail_url",
    "author",
    "summary",
    "published_at",
    "is_read",
    "is_starred",
    "raw",
}


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeHttpxClient:
    """Stands in for httpx.Client; handler decides the response per call."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        call = {"url": url, "headers": dict(headers or {}), "timeout": timeout}
        self.calls.append(call)
        return self._handler(call)


class ParseChannelFeedTests(unittest.TestCase):
    def test_two_entries_parse_with_normalize_entry_shape(self) -> None:
        entries = parse_channel_feed(FEED_XML, FEED_URL)
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(set(entry), EXPECTED_ENTRY_KEYS)

        first, second = entries
        self.assertEqual(first["youtube_video_id"], "dQw4w9WgXcQ")
        self.assertEqual(first["id"], "yt:video:dQw4w9WgXcQ")
        self.assertEqual(first["youtube_channel_id"], CHANNEL_ID)
        self.assertIsNone(first["subscription_id"])
        self.assertEqual(first["channel_title"], "Some Channel")
        self.assertEqual(first["feed_url"], FEED_URL)
        self.assertEqual(first["title"], "First video title")
        self.assertEqual(first["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(first["thumbnail_url"], "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")
        self.assertEqual(first["author"], "Some Channel")
        self.assertEqual(first["summary"], "First video description here.")
        self.assertEqual(first["published_at"], datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertFalse(first["is_read"])
        self.assertFalse(first["is_starred"])

        self.assertEqual(second["youtube_video_id"], "9bZkp7q19f0")
        self.assertEqual(second["url"], "https://www.youtube.com/watch?v=9bZkp7q19f0")
        self.assertEqual(second["thumbnail_url"], "https://i.ytimg.com/vi/9bZkp7q19f0/hqdefault.jpg")

    def test_missing_description_becomes_none(self) -> None:
        entries = parse_channel_feed(FEED_XML, FEED_URL)
        self.assertIsNone(entries[1]["summary"])

    def test_zulu_timestamp_is_timezone_aware(self) -> None:
        entries = parse_channel_feed(FEED_XML, FEED_URL)
        published = entries[1]["published_at"]
        self.assertEqual(published, datetime(2025, 2, 3, 4, 5, 6, tzinfo=timezone.utc))
        self.assertIsNotNone(published.tzinfo)

    def test_entries_without_a_video_id_are_skipped(self) -> None:
        xml = FEED_XML.replace("<yt:videoId>9bZkp7q19f0</yt:videoId>", "").replace(
            "<id>yt:video:9bZkp7q19f0</id>", "<id>yt:video:</id>"
        )
        entries = parse_channel_feed(xml, FEED_URL)
        self.assertEqual([entry["youtube_video_id"] for entry in entries], ["dQw4w9WgXcQ"])

    def test_channel_id_falls_back_to_feed_url(self) -> None:
        xml = FEED_XML.replace(f"<yt:channelId>{CHANNEL_ID}</yt:channelId>", "")
        entries = parse_channel_feed(xml, FEED_URL)
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(entry["youtube_channel_id"], CHANNEL_ID)

    def test_entry_id_falls_back_to_video_id(self) -> None:
        xml = FEED_XML.replace("<yt:videoId>dQw4w9WgXcQ</yt:videoId>", "").replace(
            "<id>yt:video:dQw4w9WgXcQ</id>", "<id>yt:video:dQw4w9WgXcQ</id>"
        )
        entries = parse_channel_feed(xml, FEED_URL)
        self.assertEqual(entries[0]["youtube_video_id"], "dQw4w9WgXcQ")

    def test_malformed_fallback_id_is_skipped_not_minted(self) -> None:
        xml = FEED_XML.replace("<yt:videoId>9bZkp7q19f0</yt:videoId>", "").replace(
            "<id>yt:video:9bZkp7q19f0</id>", "<id>yt:video:not a video id!!!</id>"
        )
        entries = parse_channel_feed(xml, FEED_URL)
        self.assertEqual([entry["youtube_video_id"] for entry in entries], ["dQw4w9WgXcQ"])

    def test_overlong_fractional_seconds_are_trimmed(self) -> None:
        xml = FEED_XML.replace(
            "<published>2025-01-02T03:04:05+00:00</published>",
            "<published>2025-01-02T03:04:05.123456789+00:00</published>",
        )
        entries = parse_channel_feed(xml, FEED_URL)
        self.assertEqual(entries[0]["published_at"], datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc))

    def test_feed_thumbnail_beats_the_synthesized_one(self) -> None:
        xml = FEED_XML.replace(
            '<media:thumbnail url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"',
            '<media:thumbnail url="https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"',
        )
        entries = parse_channel_feed(xml, FEED_URL)
        self.assertEqual(entries[0]["thumbnail_url"], "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg")


class FetchChannelFeedTests(unittest.TestCase):
    def test_conditional_headers_are_sent_and_304_has_no_text(self) -> None:
        client = _FakeHttpxClient(lambda call: _FakeResponse(304, headers={"etag": '"abc"'}))
        result = fetch_channel_feed(client, FEED_URL, etag='"abc"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
        self.assertEqual(result.status, 304)
        self.assertIsNone(result.text)
        self.assertEqual(client.calls[0]["headers"].get("If-None-Match"), '"abc"')
        self.assertEqual(
            client.calls[0]["headers"].get("If-Modified-Since"), "Wed, 01 Jan 2025 00:00:00 GMT"
        )

    def test_no_conditional_headers_without_validators(self) -> None:
        client = _FakeHttpxClient(lambda call: _FakeResponse(200, text="body"))
        result = fetch_channel_feed(client, FEED_URL)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.text, "body")
        self.assertNotIn("If-None-Match", client.calls[0]["headers"])
        self.assertNotIn("If-Modified-Since", client.calls[0]["headers"])

    def test_200_returns_body_and_validators(self) -> None:
        client = _FakeHttpxClient(
            lambda call: _FakeResponse(200, text="<feed/>", headers={"etag": '"xyz"', "last-modified": "Tue, 02 Jan 2025 00:00:00 GMT"})
        )
        result = fetch_channel_feed(client, FEED_URL, etag='"old"')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.text, "<feed/>")
        self.assertEqual(result.etag, '"xyz"')
        self.assertEqual(result.last_modified, "Tue, 02 Jan 2025 00:00:00 GMT")

    def test_network_errors_become_status_none(self) -> None:
        def raise_connect(call):
            raise httpx.ConnectError("connection refused")

        client = _FakeHttpxClient(raise_connect)
        result = fetch_channel_feed(client, FEED_URL)
        self.assertIsNone(result.status)
        self.assertIsNone(result.text)

    def test_timeouts_become_status_none(self) -> None:
        def raise_timeout(call):
            raise httpx.ReadTimeout("timed out")

        client = _FakeHttpxClient(raise_timeout)
        result = fetch_channel_feed(client, FEED_URL)
        self.assertIsNone(result.status)

    def test_http_error_status_is_returned_not_raised(self) -> None:
        client = _FakeHttpxClient(lambda call: _FakeResponse(500, text="boom"))
        result = fetch_channel_feed(client, FEED_URL)
        self.assertEqual(result.status, 500)
        self.assertIsNone(result.text)


class SyncRssOnceTests(unittest.TestCase):
    def _due_channels(self) -> list[dict]:
        return [
            {
                "id": "sub304",
                "youtube_channel_id": CHANNEL_ID,
                "feed_url": FEED_URL,
                "etag": '"etag-304"',
                "last_modified": None,
            },
            {
                "id": "subfail",
                "youtube_channel_id": "UC" + "f" * 22,
                "feed_url": channel_feed_url("UC" + "f" * 22),
                "etag": None,
                "last_modified": None,
            },
            {
                "id": "sub200",
                "youtube_channel_id": CHANNEL_ID,
                "feed_url": FEED_URL,
                "etag": None,
                "last_modified": None,
            },
        ]

    def _patched_db(self, stack: contextlib.ExitStack, due: list[dict]):
        """Mock every db function sync_rss_once touches (create=True: the RSS
        helpers may not exist in db.py yet — another agent owns that file)."""
        list_due = stack.enter_context(patch.object(rss.db, "list_rss_due_channels", create=True, return_value=due))
        record = stack.enter_context(patch.object(rss.db, "record_rss_fetch", create=True))
        sub_for_channel = stack.enter_context(
            patch.object(rss.db, "subscription_id_for_channel", create=True, return_value="feed/9")
        )
        create_run = stack.enter_context(patch.object(rss.db, "create_sync_run", create=True, return_value=42))
        finish_run = stack.enter_context(patch.object(rss.db, "finish_sync_run", create=True))
        upsert_channel = stack.enter_context(patch.object(rss.db, "upsert_channel", create=True, return_value=7))
        upsert_video = stack.enter_context(patch.object(rss.db, "upsert_video", create=True, return_value=11))
        upsert_entry = stack.enter_context(patch.object(rss.db, "upsert_entry", create=True))
        return list_due, record, sub_for_channel, create_run, finish_run, upsert_channel, upsert_video, upsert_entry

    def _patched_fetch(self, stack: contextlib.ExitStack, mapping: dict):
        return stack.enter_context(
            patch.object(rss, "fetch_channel_feed", side_effect=lambda client, url, **kwargs: mapping[url])
        )

    def test_end_to_end_counts_and_per_channel_handling(self) -> None:
        due = self._due_channels()
        mapping = {
            FEED_URL: ChannelFeedResult(status=200, text=FEED_XML, etag='"new"', last_modified=None),
            channel_feed_url("UC" + "f" * 22): ChannelFeedResult(status=None, text=None),
        }
        # The 304 channel shares FEED_URL with the 200 channel, so dispatch on
        # the validators the sync passes through instead of the URL alone.
        def fetch(client, url, etag=None, last_modified=None, timeout=20.0):
            if etag == '"etag-304"':
                return ChannelFeedResult(status=304, text=None, etag='"etag-304"', last_modified=None)
            return mapping[url]

        with contextlib.ExitStack() as stack:
            _, record, _, _, finish_run, upsert_channel, _, upsert_entry = self._patched_db(stack, due)
            stack.enter_context(patch.object(rss, "fetch_channel_feed", side_effect=fetch))
            result = rss.sync_rss_once(channel_limit=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["sync_run_id"], 42)
        # All three channels were polled (304, failed, 200); two entries stored.
        self.assertEqual(result["subscriptions_count"], 3)
        self.assertEqual(result["entries_count"], 2)
        self.assertIn("3", result["message"])

        # The 304 channel is skipped without any upserts.
        self.assertEqual(upsert_channel.call_count, 2)
        self.assertEqual(upsert_entry.call_count, 2)
        # subscription_id is filled in before upsert_entry is called.
        for call in upsert_entry.call_args_list:
            entry, _, subscription_id = call.args
            self.assertEqual(entry["subscription_id"], "feed/9")
            self.assertEqual(subscription_id, "feed/9")

        # Each channel's fetch is recorded; the failing one does not stop the loop.
        recorded = {call.args[0]: call.kwargs.get("ok", True) for call in record.call_args_list}
        self.assertEqual(recorded, {"sub304": True, "subfail": False, "sub200": True})

        status = finish_run.call_args.args[1]
        self.assertEqual(status, "success")
        self.assertEqual(finish_run.call_args.args[3], 3)
        self.assertEqual(finish_run.call_args.args[4], 2)

    def test_channel_limit_defaults_to_100(self) -> None:
        with contextlib.ExitStack() as stack:
            list_due, _, _, _, _, _, _, _ = self._patched_db(stack, [])
            self._patched_fetch(stack, {})
            rss.sync_rss_once()
            list_due.assert_called_once_with(100)

    def test_explicit_channel_limit_is_passed_through(self) -> None:
        with contextlib.ExitStack() as stack:
            list_due, _, _, _, _, _, _, _ = self._patched_db(stack, [])
            self._patched_fetch(stack, {})
            rss.sync_rss_once(channel_limit=5)
            list_due.assert_called_once_with(5)

    def test_upsert_exception_is_recorded_and_loop_continues(self) -> None:
        other_id = "UC" + "b" * 22
        other_url = channel_feed_url(other_id)
        due = [
            {"id": "subbad", "youtube_channel_id": CHANNEL_ID, "feed_url": FEED_URL, "etag": None, "last_modified": None},
            {"id": "subgood", "youtube_channel_id": other_id, "feed_url": other_url, "etag": None, "last_modified": None},
        ]
        mapping = {
            FEED_URL: ChannelFeedResult(status=200, text=FEED_XML, etag=None, last_modified=None),
            other_url: ChannelFeedResult(status=200, text=FEED_XML, etag=None, last_modified=None),
        }
        with contextlib.ExitStack() as stack:
            _, record, _, _, _, _, upsert_video, _ = self._patched_db(stack, due)
            self._patched_fetch(stack, mapping)
            upsert_video.side_effect = [RuntimeError("db down"), 11, 11]
            result = rss.sync_rss_once(channel_limit=10)

        self.assertTrue(result["ok"])
        # The bad channel still counts as polled; only the good channel's entries stored.
        self.assertEqual(result["subscriptions_count"], 2)
        self.assertEqual(result["entries_count"], 2)
        recorded = {call.args[0]: call.kwargs.get("ok", True) for call in record.call_args_list}
        self.assertEqual(recorded, {"subbad": False, "subgood": True})

    def test_feed_url_fallback_when_channel_id_missing(self) -> None:
        due = [{"id": "suburl", "youtube_channel_id": None, "feed_url": FEED_URL, "etag": None, "last_modified": None}]
        mapping = {FEED_URL: ChannelFeedResult(status=200, text=FEED_XML, etag=None, last_modified=None)}
        with contextlib.ExitStack() as stack:
            _, record, _, _, _, upsert_channel, _, upsert_entry = self._patched_db(stack, due)
            fetch_mock = self._patched_fetch(stack, mapping)
            result = rss.sync_rss_once(channel_limit=10)

        fetch_mock.assert_called_once()
        self.assertEqual(fetch_mock.call_args.args[1], FEED_URL)
        self.assertTrue(result["ok"])
        self.assertEqual(result["subscriptions_count"], 1)
        self.assertEqual(result["entries_count"], 2)
        self.assertEqual(upsert_entry.call_count, 2)
        # Channel id falls back to the feed_url query param during parsing.
        for call in upsert_channel.call_args_list:
            self.assertEqual(call.args[1], CHANNEL_ID)
        record.assert_called_once()
        self.assertTrue(record.call_args.kwargs.get("ok", True))


if __name__ == "__main__":
    unittest.main()
