from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import db, main


def _video(**fields: object) -> dict[str, object]:
    return {
        "youtube_video_id": "abc123def45",
        "title": "A video",
        "channel_title": "A channel",
        "youtube_channel_id": "UCabc",
        "duration_seconds": 1200,
        **fields,
    }


class ProgressTests(unittest.TestCase):
    def test_a_stored_fraction_draws_its_real_width(self) -> None:
        self.assertIn("width:50%", main._progress_rail(_video(percent_completed=0.5)))

    def test_a_percentage_scale_row_draws_the_same(self) -> None:
        self.assertIn("width:33%", main._progress_rail(_video(percent_completed=33.3)))

    def test_no_progress_draws_nothing(self) -> None:
        self.assertEqual(main._progress_rail(_video(percent_completed=0)), "")

    def test_the_watched_threshold_matches_playback(self) -> None:
        self.assertIn("watched-mark", main._progress_rail(_video(percent_completed=0.85)))
        self.assertIn("progress-rail", main._progress_rail(_video(percent_completed=0.84)))

    def test_a_manual_mark_counts_at_any_progress(self) -> None:
        self.assertTrue(main._is_watched(_video(percent_completed=0.1, manually_marked_watched=True)))

    def test_time_left_uses_the_resume_point_over_the_furthest_point(self) -> None:
        video = _video(percent_completed=0.9, resume_seconds=600)
        self.assertEqual(main._time_left_text(_video(percent_completed=0.5, resume_seconds=600)), "10m left")
        self.assertEqual(main._time_left_text(video), "", "0.9 is past the watched threshold")

    def test_time_left_falls_back_to_the_percentage(self) -> None:
        self.assertEqual(main._time_left_text(_video(percent_completed=0.25)), "15m left")

    def test_time_left_formats(self) -> None:
        self.assertEqual(main._time_left_text(_video(percent_completed=0.5, duration_seconds=7800)), "1h 5m left")
        self.assertEqual(main._time_left_text(_video(percent_completed=0.5, duration_seconds=7200)), "1h left")
        self.assertEqual(main._time_left_text(_video(percent_completed=0.5, duration_seconds=40)), "Under 1m left")

    def test_no_duration_means_no_claim(self) -> None:
        self.assertEqual(main._time_left_text(_video(percent_completed=0.5, duration_seconds=0)), "")


class CardTests(unittest.TestCase):
    settings = SimpleNamespace(invidious_public_url="")

    def test_a_card_in_progress_shows_time_left_instead_of_views(self) -> None:
        html = main._home_video_card(_video(percent_completed=0.5, view_count=1000), 1, self.settings)
        self.assertIn("10m left", html)
        self.assertNotIn(" views", html)

    def test_a_watched_card_drops_the_mark_watched_shortcut_but_keeps_the_menu_row(self) -> None:
        html = main._home_video_card(_video(progress_completed=True), 1, self.settings)
        self.assertIn("is-watched", html)
        self.assertNotIn('card-quick-button" type="button" data-feedback="mark_watched"', html)
        self.assertIn('data-save-to-playlist="abc123def45"', html)

    def test_quick_actions_are_backed_by_real_handlers(self) -> None:
        html = main._card_quick_actions("abc123def45", "Title", False)
        self.assertIn('data-feedback="mark_watched"', html)
        self.assertIn("mark_watched", main.ALLOWED_FEEDBACK_EVENTS)
        self.assertIn("data-save-to-playlist", html)

    def test_a_title_is_escaped_in_the_quick_actions(self) -> None:
        html = main._card_quick_actions("abc123def45", '"><script>', False)
        self.assertNotIn("<script>", html)

    def test_the_card_keeps_the_marker_the_download_badge_is_injected_at(self) -> None:
        self.assertIn('<div class="video-info">', main._home_video_card(_video(), 1, self.settings))


class SectionTests(unittest.TestCase):
    settings = SimpleNamespace(invidious_public_url="")

    def test_only_catch_up_keeps_its_eyebrow(self) -> None:
        videos = [_video()]
        plain = main._feed_section_html("recommended", "For you", "Recommended for you", videos, self.settings, kind="recommended")
        catch_up = main._feed_section_html("week", "This week", "New this week", videos, self.settings, kind="catch-up")
        self.assertNotIn("section-eyebrow", plain)
        self.assertIn("section-eyebrow", catch_up)

    def test_view_all_needs_a_destination(self) -> None:
        self.assertEqual(main._view_all_link("", "Anything"), "")
        without = main._shelf_html("a", "A", [_video()], self.settings)
        with_link = main._shelf_html("a", "A", [_video()], self.settings, view_all="/subscriptions")
        self.assertNotIn("view-all", without)
        self.assertIn('href="/subscriptions"', with_link)

    def test_every_catch_up_destination_is_a_real_route(self) -> None:
        routes = {route.path for route in main.app.routes}
        for shelf in main._CATCH_UP_SHELVES:
            if shelf.get("view_all"):
                self.assertIn(shelf["view_all"], routes)


class FilterKeyTests(unittest.TestCase):
    def test_aliases_and_chips(self) -> None:
        self.assertEqual(main._home_filter_key("subscriptions"), "subscription")
        self.assertEqual(main._home_filter_key("continue"), "continue")
        self.assertEqual(main._home_filter_key("topic:rust"), "topic:rust")

    def test_anything_else_is_plain_home(self) -> None:
        self.assertEqual(main._home_filter_key(""), "all")
        self.assertEqual(main._home_filter_key("nonsense"), "all")
        self.assertEqual(main._home_filter_key("topic:"), "all")

    def test_a_linked_topic_gets_a_selected_chip_and_stays_escaped(self) -> None:
        with patch.object(db, "list_popular_tags", return_value=[]):
            html = main._topic_chips([], 'topic:"><img')
        self.assertNotIn("<img", html)
        self.assertEqual(html.count("topic-chip active"), 1)


class NavTests(unittest.TestCase):
    def test_channels_opens_the_channel_list_not_a_second_timeline(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[]):
            html = main._home_nav("home")
        self.assertIn('href="/channels?view=channels"', html)
        self.assertNotIn("All channels", html)

    def test_no_destination_was_dropped_from_the_sidebar(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[]):
            html = main._home_nav("home")
        for href in ("/", "/shorts", "/subscriptions", "/history", "/playlists", "/liked", "/downloads", "/channels?tab=collections"):
            self.assertIn(f'href="{href}"', html)

    def test_subscription_rows_are_grouped_once_each(self) -> None:
        recent = main.datetime.now(main.timezone.utc)
        rows = [
            {"title": "New one", "youtube_channel_id": "UCa", "latest_video_at": recent},
            {"title": "Old one", "youtube_channel_id": "UCb", "latest_video_at": None},
            {"title": "Old two", "youtube_channel_id": "UCc", "latest_video_at": None},
        ]
        with patch.object(db, "list_subscriptions", return_value=rows):
            html = main._sidebar_subscription_group(user_key="local")
        self.assertEqual(html.count(">New</p>"), 1)
        self.assertEqual(html.count(">Earlier</p>"), 1)

    def test_a_hidden_row_hides_its_label_too(self) -> None:
        rows = [{"title": f"C{i}", "youtube_channel_id": f"UC{i}", "latest_video_at": None} for i in range(9)]
        rows[8]["latest_video_at"] = main.datetime.now(main.timezone.utc)
        with patch.object(db, "list_subscriptions", return_value=rows):
            html = main._sidebar_subscription_group(limit=250, user_key="local")
        self.assertIn('<p class="nav-label nav-channel-extra" hidden>New</p>', html)

    def test_every_tab_is_reachable_and_there_are_five(self) -> None:
        html = main._mobile_nav("home")
        self.assertEqual(html.count("mobile-nav-item"), 5)
        self.assertIn("data-search-open", html)
        self.assertIn("data-open-account", html)

    def test_you_stays_lit_on_the_pages_it_leads_to(self) -> None:
        for active in ("history", "playlists", "liked", "downloads", "channels", "collections"):
            self.assertIn('data-open-account aria-current="true"', main._mobile_nav(active))


class AccountMenuTests(unittest.TestCase):
    def _html(self, *, authenticated: bool, auth_mode: str = "disabled") -> str:
        user = {"authenticated": authenticated, "display_name": "Ada", "email": "", "sub": "sub-1"}
        settings = SimpleNamespace(auth_mode=auth_mode, require_login=False)
        with (
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "_notification_items", return_value=([], 0)),
        ):
            return main._header_actions(user, "sub-1")

    def test_auth_off_has_no_account_rows(self) -> None:
        html = self._html(authenticated=False)
        self.assertNotIn("Sign in", html)
        self.assertNotIn("User key", html)

    def test_auth_on_offers_sign_in(self) -> None:
        self.assertIn("Sign in with Authentik", self._html(authenticated=False, auth_mode="authentik_oidc"))

    def test_signed_in_shows_sign_out_and_key(self) -> None:
        html = self._html(authenticated=True, auth_mode="authentik_oidc")
        self.assertIn("Sign out", html)
        self.assertIn("User key", html)

    def test_every_destination_without_a_tab_is_in_the_menu(self) -> None:
        html = self._html(authenticated=False)
        for href in ("/history", "/playlists", "/liked", "/downloads", "/channels?view=channels", "/channels?tab=collections", "/admin/server"):
            self.assertIn(f'href="{href}"', html)


if __name__ == "__main__":
    unittest.main()
