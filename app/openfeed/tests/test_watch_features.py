from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import main


class DownloadFilenameTests(unittest.TestCase):
    def test_unsafe_characters_are_stripped(self) -> None:
        name = main._download_filename('AC/DC: "Live" <2024>', "abc123")
        self.assertNotIn("/", name)
        self.assertNotIn('"', name)
        self.assertTrue(name.endswith("[abc123].mp4"))

    def test_the_video_id_is_always_present(self) -> None:
        self.assertEqual(main._download_filename("", "xyz"), "xyz [xyz].mp4")
        self.assertEqual(main._download_filename("...", "xyz"), "xyz [xyz].mp4")

    def test_long_titles_are_truncated(self) -> None:
        name = main._download_filename("word " * 80, "abc123")
        self.assertLessEqual(len(name), 140)

    def test_extension_follows_the_container(self) -> None:
        self.assertTrue(main._download_filename("clip", "id", "webm").endswith(".webm"))

    def test_unicode_titles_survive(self) -> None:
        self.assertIn("café", main._download_filename("café música", "id"))


class DownloadStreamSelectionTests(unittest.TestCase):
    def test_only_muxed_streams_are_offered(self) -> None:
        from openfeed import playback

        video = {
            "formatStreams": [
                {"url": "http://a/360", "qualityLabel": "360p", "type": "video/mp4"},
                {"url": "http://a/720", "qualityLabel": "720p", "type": "video/mp4"},
            ],
            "adaptiveFormats": [{"url": "http://a/1080", "qualityLabel": "1080p"}],
        }
        self.assertEqual(playback.select_stream(video)["qualityLabel"], "720p")

    def test_no_muxed_stream_means_no_download(self) -> None:
        from openfeed import playback

        self.assertIsNone(playback.select_stream({"formatStreams": [], "adaptiveFormats": [{"url": "x"}]}))


class CommentReplyTests(unittest.TestCase):
    def _payload(self, continuation: str = "TOKEN") -> dict:
        return {
            "commentCount": 12,
            "comments": [
                {
                    "author": "@someone",
                    "content": "hello",
                    "likeCount": 3,
                    "replies": {"replyCount": 7, "continuation": continuation},
                }
            ],
        }

    def _call(self, continuation: str = "") -> dict:
        class _Response:
            status_code = 200

            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def json(self) -> dict:
                return self._payload

        captured: dict = {}

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, url: str, params=None):
                captured["params"] = params
                return _Response(self._outer._payload())

        _Client._outer = self
        with patch.object(main.db, "get_feed_preferences", return_value={"show_comments": True}):
            with patch.object(main, "_user_key", return_value="local"):
                with patch.object(main, "_login_required_redirect", return_value=None):
                    with patch.object(main.httpx, "Client", lambda *a, **k: _Client()):
                        result = main.api_comments(None, "vid", continuation)
        result["_params"] = captured.get("params")
        return result

    def test_reply_token_is_exposed_to_the_client(self) -> None:
        data = self._call()
        self.assertEqual(data["items"][0]["reply_continuation"], "TOKEN")
        self.assertEqual(data["items"][0]["reply_count"], 7)
        self.assertFalse(data["is_replies"])
        self.assertIsNone(data["_params"])

    def test_a_continuation_requests_replies_instead(self) -> None:
        data = self._call("TOKEN")
        self.assertTrue(data["is_replies"])
        self.assertEqual(data["_params"], {"continuation": "TOKEN"})


if __name__ == "__main__":
    unittest.main()


class ThumbnailProxyTests(unittest.TestCase):
    """Thumbnails must not send the viewer's browser to Google on every card."""

    def test_ytimg_thumbnails_are_served_from_our_own_proxy(self) -> None:
        video = {"youtube_video_id": "abc12345678", "thumbnail_url": "https://i.ytimg.com/vi/abc12345678/hqdefault.jpg"}
        self.assertEqual(main._thumbnail_for(video), "/vi/abc12345678/hqdefault.jpg")

    def test_invidious_thumbnails_are_proxied_whatever_the_host(self) -> None:
        for host in ("http://localhost:8061", "http://openfeed-invidious:3000", "http://192.0.2.10:8061"):
            video = {"youtube_video_id": "abc12345678", "thumbnail_url": f"{host}/vi/abc12345678/maxres.jpg"}
            self.assertEqual(main._thumbnail_for(video), "/vi/abc12345678/hqdefault.jpg")

    def test_a_missing_thumbnail_still_resolves_from_the_video_id(self) -> None:
        self.assertEqual(main._thumbnail_for({"youtube_video_id": "abc12345678"}), "/vi/abc12345678/hqdefault.jpg")

    def test_an_unrelated_host_is_left_alone(self) -> None:
        video = {"youtube_video_id": "abc12345678", "thumbnail_url": "https://cdn.example.com/custom.png"}
        self.assertEqual(main._thumbnail_for(video), "https://cdn.example.com/custom.png")

    def test_no_video_id_means_nothing_to_rebuild_from(self) -> None:
        self.assertEqual(main._thumbnail_for({"thumbnail_url": "https://i.ytimg.com/vi/x/hq.jpg"}), "https://i.ytimg.com/vi/x/hq.jpg")
