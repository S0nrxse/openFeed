"""OPML import (opml.py, POST /api/import/subscriptions.opml): the caller's list only."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import db, main, opml

CHANNEL = "UC" + "a" * 22
DOC = f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><head><title>Feeds</title></head><body>
  <outline text="YouTube" title="YouTube">
    <outline type="rss" text="Chan &amp; Co" title="Chan &amp; Co" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}" htmlUrl="https://www.youtube.com/channel/{CHANNEL}"/>
    <outline type="rss" text="Again" xmlUrl="http://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"/>
    <outline type="rss" text="Blog" xmlUrl="https://example.com/rss"/>
    <outline type="rss" text="Bad" xmlUrl="javascript:alert(1)"/>
    <outline text="Folder only"/>
  </outline>
</body></opml>""".encode()


class ParseTests(unittest.TestCase):
    def test_feeds_are_read_and_deduplicated(self) -> None:
        feeds = opml.parse(DOC)
        self.assertEqual([feed["feed_url"] for feed in feeds], [
            f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}", "https://example.com/rss",
        ])
        self.assertEqual(feeds[0]["youtube_channel_id"], CHANNEL)
        self.assertEqual(feeds[0]["title"], "Chan & Co")
        self.assertIsNone(feeds[1]["youtube_channel_id"])

    def test_entities_and_doctypes_are_refused(self) -> None:
        bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><opml><body><outline xmlUrl="https://x/&lol;"/></body></opml>'
        with self.assertRaises(opml.OpmlError):
            opml.parse(bomb)

    def test_other_documents_and_limits_are_refused(self) -> None:
        for data in (b"", b"not xml", b"<rss><channel/></rss>", b"<opml><body/></opml>", b" " * (opml.MAX_BYTES + 1)):
            with self.subTest(data=data[:20]), self.assertRaises(opml.OpmlError):
                opml.parse(data)
        many = "".join(f'<outline xmlUrl="https://example.com/{i}"/>' for i in range(opml.MAX_FEEDS + 1))
        with self.assertRaises(opml.OpmlError):
            opml.parse(f"<opml><body>{many}</body></opml>".encode())


class EndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_the_import_subscribes_only_the_caller(self) -> None:
        results = iter([{"feed_id": "a", "created": True}, {"feed_id": "b", "created": False}])
        with patch.object(main, "_user_key", return_value="kid"), \
             patch.object(db, "subscribe", side_effect=lambda *args: next(results)) as subscribe, \
             patch.object(db, "upsert_channel"), \
             patch.object(main.threading, "Thread") as thread:
            response = self.client.post("/api/import/subscriptions.opml", files={"file": ("subs.opml", DOC, "text/x-opml")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "total": 2, "subscribed": 1, "already": 1})
        self.assertEqual({call.args[0] for call in subscribe.call_args_list}, {"kid"})
        first = subscribe.call_args_list[0].args
        self.assertEqual(first[1]["id"], f"feed/https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}")
        self.assertEqual(first[2], CHANNEL)
        # Only the newly created feed is fetched, in the background.
        self.assertEqual(thread.call_args.kwargs["args"], ([(f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}", CHANNEL)],))

    def test_a_bad_file_is_a_422_and_changes_nothing(self) -> None:
        with patch.object(main, "_user_key", return_value="kid"), patch.object(db, "subscribe") as subscribe:
            response = self.client.post("/api/import/subscriptions.opml", files={"file": ("x.opml", b"<html/>", "text/html")})
        self.assertEqual(response.status_code, 422)
        self.assertIn("OPML", response.json()["detail"])
        subscribe.assert_not_called()

    def test_settings_offer_it_and_the_script_ships(self) -> None:
        html = main._data_export_settings_html({"display_name": "Me"})
        self.assertIn("data-opml-file", html)
        self.assertIn('aria-label="Choose an OPML file to import"', html)
        self.assertIn("/api/import/subscriptions.opml", main._home_script())
        self.assertIn(".opml-import:focus-within", main._home_styles())


if __name__ == "__main__":
    unittest.main()
