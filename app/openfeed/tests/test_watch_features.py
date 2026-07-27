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


if __name__ == "__main__":
    unittest.main()


class DescriptionFormattingTests(unittest.TestCase):
    """Creators lay descriptions out deliberately; that layout has to survive."""

    def test_line_breaks_are_kept(self) -> None:
        text = main._description_text("Chapters:\n0:00 Intro\n1:30 Setup")
        self.assertEqual(text, "Chapters:\n0:00 Intro\n1:30 Setup")

    def test_the_card_excerpt_helper_would_have_destroyed_them(self) -> None:
        # This is the behaviour that flattened every description into one paragraph.
        self.assertNotIn("\n", main._text_excerpt("Chapters:\n0:00 Intro"))

    def test_br_tags_become_real_breaks_before_tags_are_stripped(self) -> None:
        self.assertEqual(main._description_text("one<br>two<br/>three"), "one\ntwo\nthree")

    def test_runs_of_blank_lines_collapse_to_one(self) -> None:
        self.assertEqual(main._description_text("a\n\n\n\n\nb"), "a\n\nb")

    def test_trailing_whitespace_per_line_is_trimmed(self) -> None:
        self.assertEqual(main._description_text("a   \n   b"), "a\nb")

    def test_timestamps_become_seek_buttons(self) -> None:
        html = main._rich_text("Intro at 1:23 and 1:02:03 later")
        self.assertIn('data-seek="83"', html)
        self.assertIn('data-seek="3723"', html)

    def test_a_bare_number_is_not_mistaken_for_a_timestamp(self) -> None:
        self.assertNotIn("data-seek", main._rich_text("costs 1200 dollars"))

    def test_urls_become_links_without_swallowing_trailing_punctuation(self) -> None:
        html = main._rich_text("see https://example.com/a?b=1&c=2.")
        self.assertIn('href="https://example.com/a?b=1&c=2"', html)
        self.assertTrue(html.endswith("."))

    def test_markup_in_the_description_cannot_become_markup(self) -> None:
        html = main._rich_text('<img src=x onerror="alert(1)">')
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_a_javascript_url_is_not_turned_into_a_link(self) -> None:
        self.assertNotIn("<a", main._rich_text("javascript:alert(1)"))

    def test_handles_and_hashtags_are_linked(self) -> None:
        html = main._rich_text("by @somecreator about #space")
        self.assertIn("youtube.com/@somecreator", html)
        self.assertIn("/search?q=%23space", html)

    def test_newlines_survive_into_the_markup(self) -> None:
        self.assertIn("<br>", main._rich_text("a\nb"))


class CommentIdentityTests(unittest.TestCase):
    """Invidious returns no comment id, so a stable one has to be derived."""

    RAW = {"authorId": "UC1", "published": 1700000000, "content": "great video"}

    def test_the_same_comment_hashes_the_same_way(self) -> None:
        self.assertEqual(main._comment_identity(dict(self.RAW)), main._comment_identity(dict(self.RAW)))

    def test_different_comments_differ(self) -> None:
        other = dict(self.RAW, content="different")
        self.assertNotEqual(main._comment_identity(self.RAW), main._comment_identity(other))

    def test_the_same_text_from_two_people_is_not_one_comment(self) -> None:
        other = dict(self.RAW, authorId="UC2")
        self.assertNotEqual(main._comment_identity(self.RAW), main._comment_identity(other))

    def test_the_id_holds_no_comment_text(self) -> None:
        self.assertNotIn("great", main._comment_identity(self.RAW))

    def test_it_is_a_fixed_length_regardless_of_input(self) -> None:
        long_one = dict(self.RAW, content="x" * 5000)
        self.assertEqual(len(main._comment_identity(self.RAW)), len(main._comment_identity(long_one)))


class CommentVoteWeightTests(unittest.TestCase):
    def test_comment_votes_are_accepted_as_feedback_events(self) -> None:
        self.assertIn("comment_like", main.ALLOWED_FEEDBACK_EVENTS)
        self.assertIn("comment_dislike", main.ALLOWED_FEEDBACK_EVENTS)

    def test_they_weigh_far_less_than_rating_the_video_itself(self) -> None:
        import inspect

        from openfeed import db

        source = inspect.getsource(db.list_recommended_videos)
        self.assertIn("WHEN event_type = 'comment_like' THEN 1", source)
        self.assertIn("WHEN event_type = 'comment_dislike' THEN -1.5", source)
        # The video's own rating stays the dominant signal.
        self.assertIn("WHEN event_type = 'more_like_this' THEN 5", source)


class ThumbIconConsistencyTests(unittest.TestCase):
    def test_the_script_thumbs_come_from_the_server_icon_table(self) -> None:
        # A hand-copied duplicate had already drifted from _THUMB_PATH.
        script = main._home_script()
        self.assertIn(f"const LIKE_ICON = '{main._icon('like')}'", script)
        self.assertIn(f"const DISLIKE_ICON = '{main._icon('dislike')}'", script)

    def test_dislike_is_the_like_thumb_rotated(self) -> None:
        self.assertIn('<g transform="rotate(180 12 12)">', main._icon("dislike"))
