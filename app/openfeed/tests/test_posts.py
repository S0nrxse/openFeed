from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from openfeed import posts


class ReadOnlyTests(unittest.TestCase):
    def test_the_module_never_writes_to_youtube(self) -> None:
        # No posting, commenting or vote submission: all need an authenticated account.
        with open(posts.__file__ or "", encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in ("client.post(", "client.put(", "client.delete(", '"POST"'):
            self.assertNotIn(forbidden, text)


class IdValidationTests(unittest.TestCase):
    def test_channel_ids_must_look_like_channel_ids(self) -> None:
        self.assertTrue(posts.valid_channel_id("UCYO_jab_esuFRV4b17AJtAw"))
        for bad in ("", "UCshort", "../../etc", "PLnotachannel", "UC" + "x" * 40):
            self.assertFalse(posts.valid_channel_id(bad), bad)

    def test_post_ids_are_bounded(self) -> None:
        self.assertTrue(posts.valid_post_id("Ugkx9CZiJmWDWAgW-pB-DGHDB3kz2_znQNVV"))
        for bad in ("", "short", "a" * 200, "has spaces"):
            self.assertFalse(posts.valid_post_id(bad), bad)


class NormalizeTests(unittest.TestCase):
    RAW = {
        "commentId": "Ugkx9CZiJmWDWAgW-pB-DGHDB3kz2_znQNVV",
        "authorId": "UCYO_jab_esuFRV4b17AJtAw",
        "author": "3Blue1Brown",
        "authorThumbnails": [{"url": "//yt3.example/a.jpg", "width": 88, "height": 88}],
        "content": "Hello",
        "contentHtml": "Hello",
        "published": 1750000000,
        "publishedText": "3 weeks ago",
        "likeCount": 42,
        "replyCount": 7,
    }

    def test_the_post_id_comes_from_comment_id(self) -> None:
        # Invidious returns postId as null and puts the id in commentId.
        self.assertEqual(posts.normalize_post(self.RAW)["post_id"], self.RAW["commentId"])

    def test_a_post_without_an_id_is_dropped(self) -> None:
        self.assertIsNone(posts.normalize_post({"author": "x"}))

    def test_publish_time_becomes_a_datetime(self) -> None:
        self.assertIsInstance(posts.normalize_post(self.RAW)["published_at"], datetime)

    def test_protocol_relative_avatars_get_a_scheme(self) -> None:
        self.assertTrue(posts.normalize_post(self.RAW)["author_thumbnail"].startswith("https://"))

    def test_a_non_dict_is_not_a_post(self) -> None:
        for bad in ([], "text", None, 7):
            self.assertIsNone(posts.normalize_post(bad))


class AttachmentTests(unittest.TestCase):
    def test_a_poll_keeps_its_choices_and_total(self) -> None:
        attachment = posts.normalize_attachment(
            {"type": "poll", "totalVotes": 84000, "choices": [{"text": "Yes"}, {"text": "No"}]})
        self.assertEqual(attachment["choices"], ["Yes", "No"])
        self.assertEqual(attachment["total_votes"], 84000)

    def test_a_poll_carries_no_per_option_counts(self) -> None:
        # YouTube does not publish the split. Anything claiming to show it would be made
        # up, which is the whole reason voting here is simulated.
        attachment = posts.normalize_attachment(
            {"type": "poll", "totalVotes": 100, "choices": [{"text": "A"}, {"text": "B"}]})
        self.assertNotIn("votes", str(attachment["choices"]))

    def test_a_video_attachment_keeps_what_a_card_needs(self) -> None:
        attachment = posts.normalize_attachment(
            {"type": "video", "videoId": "Rtkac4WHC1o", "title": "T", "lengthSeconds": 90, "viewCount": 5})
        self.assertEqual(attachment["youtube_video_id"], "Rtkac4WHC1o")
        self.assertEqual(attachment["duration_seconds"], 90)

    def test_an_image_picks_a_usable_url(self) -> None:
        attachment = posts.normalize_attachment(
            {"type": "image", "imageThumbnails": [{"url": "https://x/a.jpg", "width": 640}]})
        self.assertEqual(attachment["images"], ["https://x/a.jpg"])

    def test_an_unknown_type_is_reported_not_dropped(self) -> None:
        # The text of a post is still worth reading when its attachment is unrenderable.
        attachment = posts.normalize_attachment({"type": "quiz", "foo": 1})
        self.assertEqual(attachment["type"], "unsupported")
        self.assertEqual(attachment["raw_type"], "quiz")

    def test_a_supported_type_with_no_usable_payload_degrades(self) -> None:
        self.assertEqual(posts.normalize_attachment({"type": "video"})["type"], "unsupported")
        self.assertEqual(posts.normalize_attachment({"type": "poll", "choices": []})["type"], "unsupported")

    def test_no_attachment_is_none(self) -> None:
        self.assertIsNone(posts.normalize_attachment(None))
        self.assertIsNone(posts.normalize_attachment({}))


class ChannelPostsTests(unittest.TestCase):
    def setUp(self) -> None:
        posts.clear_cache()

    tearDown = setUp

    def test_posts_are_read_from_the_comments_key(self) -> None:
        payload = {"comments": [dict(NormalizeTests.RAW)], "continuation": "tok"}
        with patch.object(posts, "_get", return_value=payload):
            page = posts.channel_posts("UCYO_jab_esuFRV4b17AJtAw")
        self.assertEqual(len(page["posts"]), 1)
        self.assertEqual(page["continuation"], "tok")

    def test_a_channel_with_posts_disabled_is_empty_not_an_error(self) -> None:
        with patch.object(posts, "_get", return_value=None):
            page = posts.channel_posts("UCYO_jab_esuFRV4b17AJtAw")
        self.assertTrue(page["ok"])
        self.assertEqual(page["posts"], [])

    def test_a_bad_channel_id_never_reaches_the_network(self) -> None:
        with patch.object(posts, "_get", side_effect=AssertionError("should not fetch")):
            self.assertFalse(posts.channel_posts("nonsense")["ok"])

    def test_results_are_cached(self) -> None:
        calls = {"n": 0}

        def _get(path, params=None):
            calls["n"] += 1
            return {"comments": [], "continuation": ""}

        with patch.object(posts, "_get", _get):
            posts.channel_posts("UCYO_jab_esuFRV4b17AJtAw")
            posts.channel_posts("UCYO_jab_esuFRV4b17AJtAw")
        self.assertEqual(calls["n"], 1)

    def test_subscription_posts_are_newest_first_and_bounded(self) -> None:
        def _page(channel_id, continuation=""):
            return {"ok": True, "continuation": "", "posts": [
                {"post_id": f"{channel_id}-a", "published_at": datetime(2026, 1, 2)},
                {"post_id": f"{channel_id}-b", "published_at": datetime(2026, 3, 4)},
                {"post_id": f"{channel_id}-c", "published_at": datetime(2025, 1, 1)},
            ]}

        with patch.object(posts, "channel_posts", _page):
            found = posts.subscription_posts(["UC" + "a" * 22, "UC" + "b" * 22], per_channel=2)
        # Two per channel, newest first overall.
        self.assertEqual(len(found), 4)
        dates = [p["published_at"] for p in found]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_one_failing_channel_does_not_break_the_shelf(self) -> None:
        def _page(channel_id, continuation=""):
            if channel_id.endswith("a" * 22):
                raise posts.PostsError("boom")
            return {"ok": True, "continuation": "", "posts": [{"post_id": "x", "published_at": datetime(2026, 1, 1)}]}

        with patch.object(posts, "channel_posts", _page):
            found = posts.subscription_posts(["UC" + "a" * 22, "UC" + "b" * 22])
        self.assertEqual(len(found), 1)
