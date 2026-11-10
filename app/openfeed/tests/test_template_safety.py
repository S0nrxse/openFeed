from __future__ import annotations

import re
import unittest
from html import escape
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import main
from openfeed.tests.support import offline_db


def _row_settings() -> SimpleNamespace:
    return SimpleNamespace(invidious_public_url="")


def _row_video(**overrides):
    video = {
        "youtube_video_id": "vid1",
        "youtube_channel_id": "UC123",
        "title": "Some Title",
        "channel_title": "Some Channel",
        "duration_seconds": 61,
        "view_count": None,
        "published_at": None,
    }
    video.update(overrides)
    return video


def _shell_settings() -> SimpleNamespace:
    return SimpleNamespace(
        youtube_fallback_enabled=False,
        invidious_public_url="",
        ryd_enabled=False,
        auth_configured=False,
        require_login=False,
        required_group="",
        admin_groups=set(),
        source_url="",
        sponsorblock_enabled=False,
    )


def _channel_row(index: int) -> dict[str, object]:
    return {
        "youtube_channel_id": f"UC{index:022d}",
        "title": f"Channel {index}",
        "channel_avatar_url": "",
        "avatar_url": "",
        "latest_video_at": None,
    }


class MediaRowExtensionTests(unittest.TestCase):
    """playlist_page extends _media_row through arguments, not str.replace."""

    def test_index_and_playlist_row_render_without_string_surgery(self) -> None:
        html = main._media_row(
            _row_video(), _row_settings(), index=3,
            playlist_row_id="vid1", trailing='<button type="button">x</button>',
        )
        self.assertIn('<span class="row-index">3</span>', html)
        self.assertIn('data-playlist-row="vid1"', html)
        self.assertIn('<button type="button">x</button>', html)

    def test_playlist_row_id_is_escaped_inside(self) -> None:
        evil = '"><script>alert(1)</script>'
        html = main._media_row(_row_video(), _row_settings(), playlist_row_id=evil)
        self.assertNotIn("<script>", html)
        self.assertNotIn(evil, html)
        self.assertIn(f'data-playlist-row="{escape(evil, quote=True)}"', html)

    def test_no_row_attrs_parameter_remains(self) -> None:
        import inspect

        params = inspect.signature(main._media_row).parameters
        self.assertNotIn("row_attrs", params)
        self.assertIn("playlist_row_id", params)

    def test_no_hidden_row_label_span(self) -> None:
        html = main._media_row(_row_video(), _row_settings())
        self.assertNotIn("row-label", html)
        # The source label still reaches the visible metadata as the fallback.
        self.assertIn("Library", html)

    def test_thumb_fallback_shows_title_initials(self) -> None:
        html = main._media_row(_row_video(), _row_settings())
        self.assertIn('<span class="thumb-fallback">ST</span>', html)
        self.assertNotIn('<span class="thumb-fallback">OpenFeed</span>', html)

    def test_thumbnail_image_stays_decorative(self) -> None:
        html = main._media_row(_row_video(), _row_settings())
        self.assertIn('src="/vi/vid1/hqdefault.jpg" alt=""', html)


class SubscriptionCardExtensionTests(unittest.TestCase):
    """collection_page extends _subscription_card through arguments, not str.replace."""

    def _member(self, **overrides):
        member = {
            "id": "m1",
            "title": "Chan",
            "youtube_channel_id": "UC123",
            "feed_url": "https://example.com/feed",
            "html_url": "",
            "video_count": 3,
            "latest_video_at": None,
            "channel_avatar_url": "",
        }
        member.update(overrides)
        return member

    def test_member_id_and_trailing_render_without_replace(self) -> None:
        html = main._subscription_card(
            self._member(), member_id="m1", trailing='<button type="button">rm</button>',
        )
        self.assertIn('data-member-card="m1"', html)
        self.assertIn('<button type="button">rm</button>', html)
        self.assertEqual(html.count("<article"), 1)

    def test_member_id_is_escaped_inside(self) -> None:
        evil = '"><script>alert(1)</script>'
        html = main._subscription_card(self._member(), member_id=evil)
        self.assertNotIn("<script>", html)
        self.assertIn(f'data-member-card="{escape(evil, quote=True)}"', html)

    def test_plain_cards_carry_no_member_attribute(self) -> None:
        html = main._subscription_card(self._member())
        self.assertNotIn("data-member-card", html)
        self.assertIn('data-subscription-card="m1"', html)


class CollectionPageSmokeTests(unittest.TestCase):
    """collection_page renders end to end with the database stubbed out."""

    COLLECTION_ID = "12345678-1234-5678-1234-567812345678"

    def _render(self, members, videos=(), view="channels"):
        collection = {
            "id": self.COLLECTION_ID,
            "name": "Test Collection",
            "description": "A description",
            "latest_member_at": None,
        }
        # Settings stay real: get_settings() only reads the environment, so the
        # render exercises the same shell every page ships. Auth and the database
        # reads are the parts that get stubbed.
        with offline_db(
            list_subscriptions=[],
            get_subscription_collection=collection,
            list_collection_subscriptions=members,
            list_collection_videos=list(videos),
        ), patch.object(main, "_login_required_redirect", return_value=None), \
            patch.object(main, "_user_key", return_value="local"):
            return main.collection_page(None, self.COLLECTION_ID, view=view)

    def _member(self, **overrides):
        member = {
            "id": "m1",
            "title": "Chan",
            "youtube_channel_id": "UC123",
            "feed_url": "https://example.com/feed",
            "html_url": "",
            "video_count": 3,
            "latest_video_at": None,
            "channel_avatar_url": "",
        }
        member.update(overrides)
        return member

    def test_the_feed_view_has_no_member_rows_and_an_empty_collection_explains_itself(self) -> None:
        self.assertNotIn("data-member-card", self._render([self._member()], view=""))
        empty = self._render([], view="")
        self.assertIn("empty-state", empty)
        self.assertIn("data-add-channels", empty)

    def test_renders_with_member_cards_and_remove_controls(self) -> None:
        html = self._render([self._member()])
        self.assertIsInstance(html, str)
        self.assertIn("Test Collection", html)
        self.assertIn('data-member-card="m1"', html)
        self.assertIn("collection-remove", html)

    def test_member_titles_are_escaped(self) -> None:
        evil = '"><script>alert(1)</script>'
        html = self._render([self._member(title=evil)])
        # The page legitimately ships <script> blocks, so assert on the payload:
        # the raw string must not appear, its escaped form must.
        self.assertNotIn(evil, html)
        self.assertIn(escape(evil, quote=True), html)

    def test_invalid_collection_id_is_a_404(self) -> None:
        with patch.object(main, "_login_required_redirect", return_value=None), \
            patch.object(main, "_user_key", return_value="local"):
            with self.assertRaises(main.HTTPException) as caught:
                main.collection_page(None, "not-a-uuid")
            self.assertEqual(caught.exception.status_code, 404)


class ChannelsPageSmokeTests(unittest.TestCase):
    """channels_page: one switch, rendered state, and an honest empty list."""

    COLLECTION = "12345678-1234-5678-1234-567812345678"
    SUB = {"id": "s1", "title": "Chan", "youtube_channel_id": "UC1", "feed_url": "https://f", "video_count": 2}

    def _render(self, subscriptions=None, collections=(), **query):
        subscriptions = [self.SUB] if subscriptions is None else subscriptions
        with offline_db(
            list_subscriptions=subscriptions,
            list_subscription_collections=list(collections),
            list_collection_ids_for_subscriptions={"s1": [self.COLLECTION]} if collections else {},
            list_subscription_recommendations=[],
            channel_notification_levels={"UC1": "none"},
        ), patch.object(main, "_login_required_redirect", return_value=None), \
            patch.object(main, "_user_key", return_value="local"):
            return main.channels_page(None, **query)

    def test_it_opens_on_the_channel_list_with_one_visible_selection(self) -> None:
        html = self._render()
        self.assertIn('class="channel-row"', html)
        self.assertEqual(html.count('aria-current="page">Channels<'), 1)
        self.assertNotIn("View as", html)
        self.assertNotIn("data-page-tab", html)

    def test_the_bell_shows_the_stored_level(self) -> None:
        self.assertIn('aria-label="Notifications: None"', self._render())

    def test_timeline_still_renders_with_an_empty_state(self) -> None:
        html = self._render(view="timeline")
        self.assertIn('aria-current="page">Timeline<', html)
        self.assertIn("No videos from your subscriptions yet", html)

    def test_collections_view_and_its_chips(self) -> None:
        collection = {"id": self.COLLECTION, "name": "Group", "subscription_count": 1, "member_titles": ["Chan"], "member_avatars": [""]}
        self.assertIn("Open feed", self._render(collections=[collection], tab="collections"))
        self.assertIn(f'href="/collection/{self.COLLECTION}"', self._render(collections=[collection]))

    def test_groups_filter_on_the_server(self) -> None:
        collection = {"id": self.COLLECTION, "name": "Group"}
        html = self._render(collections=[collection], group="none")
        self.assertNotIn('class="channel-row"', html)
        self.assertIn("No channels in this group", html)

    def test_no_subscriptions_says_how_to_get_some(self) -> None:
        html = self._render(subscriptions=[])
        self.assertIn("No subscriptions yet", html)
        self.assertIn("/?settings=import", html)


class PostCardSafetyTests(unittest.TestCase):
    """content_html arrives verbatim from YouTube with no sanitization anywhere."""

    def _post(self, **overrides):
        post = {
            "post_id": "p1",
            "youtube_channel_id": "UC123",
            "author": "Some Channel",
            "content": "Plain text body",
            "content_html": "",
            "attachment": None,
        }
        post.update(overrides)
        return post

    def test_content_html_is_never_rendered_as_markup(self) -> None:
        evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        html = main._post_card(self._post(content=evil))
        # The page's <script> blocks are its own; the payload string must appear
        # only in escaped form.
        self.assertNotIn("<script>alert", html)
        self.assertIn(escape(evil, quote=True), html)

    def test_html_fallback_renders_escaped_not_raw(self) -> None:
        evil = '<script>alert(1)</script>'
        # When the extracted plain text is empty the raw HTML string is shown, but
        # only through escape() — never as markup.
        html = main._post_card(self._post(content="", content_html=evil))
        self.assertNotIn("<script>alert", html)
        self.assertIn(escape(evil, quote=True), html)

    def test_plain_text_takes_precedence_over_content_html(self) -> None:
        html = main._post_card(self._post(content="Plain text body", content_html="<script>x</script>"))
        self.assertIn("Plain text body", html)
        self.assertNotIn("<script>", html)

    def test_post_id_and_channel_id_are_escaped(self) -> None:
        evil = '"><script>alert(1)</script>'
        html = main._post_card(self._post(post_id=evil, youtube_channel_id=evil))
        self.assertNotIn("<script>alert", html)
        self.assertIn(f'data-post-id="{escape(evil, quote=True)}"', html)


class SafeExternalHrefTests(unittest.TestCase):
    """Feed-supplied entry links could otherwise ship javascript: hrefs."""

    def test_http_and_https_pass_through(self) -> None:
        self.assertEqual(main._safe_external_href("https://example.com/watch?v=1"), "https://example.com/watch?v=1")
        self.assertEqual(main._safe_external_href("http://example.com/feed.xml"), "http://example.com/feed.xml")

    def test_everything_dangerous_drops_out(self) -> None:
        for evil in (
            "javascript:alert(1)",
            "JAVASCRIPT:alert(1)",
            " data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox",
            "\u0000javascript:alert(1)",
            "//cdn.example.com/x",
            "/relative/path",
            "chrome://settings",
        ):
            self.assertEqual(main._safe_external_href(evil), "", f"{evil!r} must be dropped")

    def test_media_row_original_url_is_browsable_or_relative(self) -> None:
        evil = "javascript:alert(1)"
        html = main._media_row(_row_video(url=evil), _row_settings())
        self.assertNotIn("javascript:", html)
        html2 = main._media_row(_row_video(url="https://example.com/x"), _row_settings())
        self.assertIn("https://example.com/x", html2)


class SidebarProbeTests(unittest.TestCase):
    """The shell fetches a VISIBLE + 1 probe; the API serves the full list."""

    def test_shell_fetches_only_visible_plus_one(self) -> None:
        with patch.object(main.db, "list_subscriptions", return_value=[]) as listed:
            main._sidebar_subscription_group(user_key="u1")
        listed.assert_called_once_with(main.SIDEBAR_VISIBLE_CHANNELS + 1, recent_first=True, user_key="u1")

    def test_channels_are_not_over_fetched_threefold(self) -> None:
        with patch.object(main.db, "list_subscriptions", return_value=[]) as listed:
            main._sidebar_channels(250, "u1")
        listed.assert_called_once_with(250, recent_first=True, user_key="u1")

    def test_without_a_user_the_sidebar_lists_nobodys_channels(self) -> None:
        with patch.object(main.db, "list_subscriptions", return_value=[_channel_row(0)]) as listed:
            self.assertEqual(main._sidebar_subscription_group(), "")
        listed.assert_not_called()

    def test_capped_probe_shows_a_generic_more_label(self) -> None:
        rows = [_channel_row(index) for index in range(8)]
        with patch.object(main.db, "list_subscriptions", return_value=rows):
            html = main._sidebar_subscription_group(user_key="u1")
        self.assertIn(">Show more<", html)
        self.assertNotIn("Show 1 more", html)

    def test_full_list_keeps_the_exact_remaining_count(self) -> None:
        rows = [_channel_row(index) for index in range(10)]
        with patch.object(main.db, "list_subscriptions", return_value=rows):
            html = main._sidebar_subscription_group(limit=250, user_key="u1")
        self.assertIn("Show 3 more", html)

    def test_sidebar_api_returns_the_full_list(self) -> None:
        rows = [_channel_row(index) for index in range(10)]
        with patch.object(main, "_login_required_redirect", return_value=None), \
            patch.object(main, "_user_key", return_value="u1"), \
            patch.object(main.db, "list_subscriptions", return_value=rows) as listed:
            html = main.api_sidebar_subscriptions(None)
        listed.assert_called_once_with(250, recent_first=True, user_key="u1")
        self.assertIn("Show 3 more", html)


class MobileNavLabelTests(unittest.TestCase):
    """Each mobile nav entry names a different destination."""

    def test_no_duplicate_mobile_nav_labels(self) -> None:
        labels = re.findall(r"<span>([^<>]+)</span>", main._mobile_nav())
        self.assertGreater(len(labels), 0)
        self.assertEqual(len(labels), len(set(labels)), f"duplicate labels: {labels}")


if __name__ == "__main__":
    unittest.main()
