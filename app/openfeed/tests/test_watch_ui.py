from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import db, main, playback

_feed = main._subscription_feed_for_video


class ThumbIconTests(unittest.TestCase):
    """The like and dislike buttons must be the same thumb, not two drawings."""

    def test_dislike_is_the_like_thumb_rotated(self) -> None:
        like = main.ICON_PATHS["like"]
        dislike = main.ICON_PATHS["dislike"]
        self.assertEqual(dislike, f'<g transform="rotate(180 12 12)">{like}</g>')

    def test_the_outline_is_closed_so_filling_it_renders_a_solid_thumb(self) -> None:
        # A stroke-only icon would need a second "filled" asset; a closed subpath does not.
        self.assertIn("Z", main.ICON_PATHS["like"])

    def test_the_watch_page_reaction_buttons_share_one_class(self) -> None:
        css = main._home_styles()
        self.assertIn(".reaction-action.is-selected .icon { fill: currentColor; }", css)
        self.assertIn("@keyframes thumb-pop", css)
        self.assertIn("@keyframes thumb-pop-down", css)

    def test_shorts_reactions_fill_the_same_way(self) -> None:
        self.assertIn(".shorts-action.is-selected .icon { fill: currentColor; }", main._home_styles())


class TheaterLayoutTests(unittest.TestCase):
    """Theater mode keeps the two-column split below a full-bleed player."""

    def test_normal_mode_stacks_the_player_above_the_description(self) -> None:
        self.assertIn('grid-template-areas: "stage panel" "content panel"', main._home_styles())

    def test_theater_spans_the_player_and_keeps_up_next_beside_the_description(self) -> None:
        css = main._home_styles()
        self.assertIn('body.theater .watch-layout { grid-template-areas: "stage stage" "content panel"; }', css)
        # The old behaviour dropped to `display: block`, which pushed up-next below everything.
        self.assertNotIn("body.theater .watch-layout { display: block; }", css)

    def test_one_column_breakpoints_collapse_the_named_areas_too(self) -> None:
        # Columns and areas have to change together or the grid silently breaks.
        self.assertIn('grid-template-columns: 1fr; grid-template-areas: "stage" "content" "panel";', main._home_styles())


class MetaRowSpacingTests(unittest.TestCase):
    """The merged row has to carry the gap the old actions row used to provide."""

    def test_the_row_reserves_space_below_itself(self) -> None:
        # Without this the description box butts against the subscriber count and clips it.
        self.assertIn("margin: 4px 0 12px; min-height: 40px;", main._home_styles())

    def test_the_actions_no_longer_carry_that_margin(self) -> None:
        # They are right-aligned on the shared row now, so a vertical margin there would
        # only reintroduce the two-row spacing.
        self.assertIn(".watch-actions { margin: 0 0 0 auto; }", main._home_styles())


class TheaterSizingTests(unittest.TestCase):
    """The player keeps its width and letterboxes, rather than shrinking off the gutter."""

    def test_the_height_is_capped_against_the_viewport(self) -> None:
        css = main._home_styles()
        self.assertIn("height: min(calc(100cqw * 9 / 16), max(280px, calc(100vh - var(--header) - var(--theater-reserve, 196px))))", css)

    def test_the_reserve_is_a_tunable_variable(self) -> None:
        self.assertIn("--theater-reserve: 196px;", main._home_styles())

    def test_the_stage_is_a_size_container(self) -> None:
        # 100cqw only resolves if the stage declares itself a container.
        self.assertIn("body.theater .watch-stage { container-type: inline-size;", main._home_styles())

    def test_the_player_meets_the_header_with_no_gap(self) -> None:
        # The page's usual 20px breathing room below the header would read as a seam.
        self.assertIn("body.theater .watch-main { padding-top: var(--header); }", main._home_styles())

    def test_the_title_does_not_stack_its_margin_on_the_stage(self) -> None:
        # Left as-is it both doubled the gap under the player and pushed the title 16px
        # below the up-next heading.
        self.assertIn("body.theater .watch-title { margin-top: 0; }", main._home_styles())

    def test_the_stage_bleeds_past_the_page_gutter(self) -> None:
        # Edge to edge means cancelling the gutter the rest of the page pads by.
        self.assertIn("margin: 0 calc(var(--watch-gutter) * -1) 14px;", main._home_styles())

    def test_the_player_width_is_pinned_so_the_cap_cannot_shrink_it(self) -> None:
        css = main._home_styles()
        self.assertIn("body.theater .watch-player-shell { width: 100%; max-width: 100%; margin-inline: 0;", css)
        # aspect-ratio + max-height would make the browser re-derive width from the clamped
        # height, pulling the box off the gutter it is supposed to line up with.
        self.assertNotIn("aspect-ratio: 16 / 9;\n        max-height:", css)

    def test_the_iframe_fills_the_capped_box(self) -> None:
        self.assertIn("body.theater .player-frame { height: 100%; aspect-ratio: auto; }", main._home_styles())

    def test_vertical_shorts_keep_their_own_ratio_instead_of_letterboxing(self) -> None:
        css = main._home_styles()
        self.assertIn("body.theater .watch-player-shell.is-short { height: auto; aspect-ratio: 9 / 16;", css)


class NotificationLevelTests(unittest.TestCase):
    """Per-channel bell settings decide what reaches the notification panel."""

    def test_the_default_lets_everything_through(self) -> None:
        self.assertTrue(main._notification_allows({"tags": []}, "all", {"music"}))

    def test_none_mutes_the_channel(self) -> None:
        self.assertFalse(main._notification_allows({"tags": ["music"]}, "none", {"music"}))

    def test_personalized_keeps_uploads_matching_a_followed_topic(self) -> None:
        self.assertTrue(main._notification_allows({"tags": ["Music", "live"]}, "personalized", {"music"}))

    def test_personalized_drops_uploads_that_match_nothing(self) -> None:
        self.assertFalse(main._notification_allows({"tags": ["cooking"]}, "personalized", {"music"}))

    def test_personalized_without_followed_topics_behaves_as_all(self) -> None:
        # With nothing to narrow by, narrowing would silently mute the channel.
        self.assertTrue(main._notification_allows({"tags": ["cooking"]}, "personalized", set()))

    def test_an_unknown_level_is_treated_as_all(self) -> None:
        self.assertTrue(main._notification_allows({"tags": []}, "wat", {"music"}))


class NotificationBellMarkupTests(unittest.TestCase):
    def test_the_trigger_shows_the_current_level(self) -> None:
        html = main._notification_bell("UC123", "none")
        self.assertIn("Notifications: None", html)
        self.assertIn("is-muted", html)

    def test_every_level_is_offered(self) -> None:
        html = main._notification_bell("UC123", "all")
        for level in ("all", "personalized", "none"):
            self.assertIn(f'data-notify-level="{level}"', html)

    def test_only_the_current_level_is_ticked(self) -> None:
        html = main._notification_bell("UC123", "personalized")
        self.assertEqual(html.count("✓"), 1)
        # The tick trails the row it belongs to, so it must fall after that row's attribute
        # and before the next one starts.
        after_current = html.split('data-notify-level="personalized"', 1)[1]
        self.assertIn("✓", after_current.split('data-notify-level="none"', 1)[0])

    def test_every_row_has_a_trailing_slot_to_move_the_tick_into(self) -> None:
        # Without a slot on unticked rows the client could clear the tick but never set it.
        html = main._notification_bell("UC123", "all")
        self.assertEqual(html.count('class="menu-trailing"'), 3)

    def test_no_channel_id_means_no_bell(self) -> None:
        self.assertEqual(main._notification_bell("", "all"), "")


class SubscribeControlTests(unittest.TestCase):
    def test_subscribing_offers_the_bell(self) -> None:
        html = main._subscribe_control("UC1", "https://feed", "Some Channel", True, "all")
        self.assertIn("is-subscribed", html)
        self.assertIn("data-notify-menu", html)
        self.assertIn("data-unsubscribe-feed", html)

    def test_the_bell_is_rendered_but_hidden_before_subscribing(self) -> None:
        # It ships hidden so subscribing in place can reveal it without a reload.
        html = main._subscribe_control("UC1", "https://feed", "Some Channel", False)
        self.assertNotIn("is-subscribed", html)
        self.assertIn("data-notify-menu", html)
        self.assertIn("data-subscribe-feed", html)

    def test_a_channel_with_no_feed_gets_no_control(self) -> None:
        self.assertEqual(main._subscribe_control("UC1", "", "Some Channel", False), "")

    def test_the_channel_title_is_escaped(self) -> None:
        html = main._subscribe_control("UC1", "https://feed", '<script>"x"', False)
        self.assertNotIn("<script>", html)


class SubscriptionStateTests(unittest.TestCase):
    """Subscription belongs to the channel, not to how a video happened to arrive."""

    def test_a_recommended_video_on_a_subscribed_channel_reads_as_subscribed(self) -> None:
        # No freshrss_entries row, so only the channel-level column is populated. This is
        # the case that rendered "Subscribe" for an already-subscribed channel.
        video = {"channel_subscription_feed_url": "https://youtube.com/feeds/videos.xml?channel_id=UC1"}
        self.assertEqual(_feed(video), "https://youtube.com/feeds/videos.xml?channel_id=UC1")

    def test_a_feed_delivered_video_still_works(self) -> None:
        self.assertEqual(_feed({"subscription_feed_url": "https://feed"}), "https://feed")

    def test_the_channel_level_column_wins(self) -> None:
        video = {"channel_subscription_feed_url": "https://channel", "subscription_feed_url": "https://entry"}
        self.assertEqual(_feed(video), "https://channel")

    def test_an_unsubscribed_channel_reads_as_unsubscribed(self) -> None:
        self.assertEqual(_feed({"channel_subscription_feed_url": None, "subscription_feed_url": None}), "")

    def test_whitespace_only_is_not_a_subscription(self) -> None:
        self.assertEqual(_feed({"channel_subscription_feed_url": "   "}), "")

    def test_the_query_joins_subscriptions_on_the_channel(self) -> None:
        import inspect

        source = inspect.getsource(db.get_video_by_youtube_id)
        self.assertIn("LEFT JOIN freshrss_subscriptions cs ON cs.youtube_channel_id = c.youtube_channel_id", source)
        self.assertIn("cs.feed_url AS channel_subscription_feed_url", source)


class SubscribeIdempotencyTests(unittest.TestCase):
    """Subscribing to a feed FreshRSS already has is the goal state, not an error."""

    def _call(self, subscribe_error: Exception | None, existing_stream: str | None) -> dict[str, object]:
        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def subscribe_feed(self, feed_url: str, title: str | None = None) -> None:
                if subscribe_error:
                    raise subscribe_error

            def stream_id_for_feed_url(self, feed_url: str) -> str | None:
                return existing_stream

        payload = main.SubscribeRequest(youtube_channel_id="UC1", title="Chan", feed_url="https://feed")
        settings = SimpleNamespace(freshrss_configured=True)
        with patch.object(main, "FreshRSSClient", _Client), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(main.db, "upsert_subscription"), \
             patch.object(main.db, "upsert_channel"):
            return main.api_subscribe_freshrss(payload)

    def test_an_already_subscribed_feed_succeeds(self) -> None:
        result = self._call(RuntimeError("already subscribed"), "feed/7")
        self.assertTrue(result["ok"])

    def test_a_genuine_failure_still_raises(self) -> None:
        with self.assertRaises(main.HTTPException) as caught:
            self._call(RuntimeError("connection refused"), None)
        self.assertEqual(caught.exception.status_code, 502)

    def test_the_happy_path_is_unchanged(self) -> None:
        self.assertTrue(self._call(None, "feed/7")["ok"])


class WatchOptionsMenuTests(unittest.TestCase):
    def test_the_trigger_is_a_three_dot_button(self) -> None:
        html = main._watch_options_menu("vid1", "https://inv/watch?v=vid1", "https://yt/watch?v=vid1")
        self.assertIn(main._icon("more"), html)
        self.assertNotIn(main._icon("settings"), html)

    def test_watch_state_actions_moved_into_the_menu(self) -> None:
        html = main._watch_options_menu("vid1", "", "https://yt")
        self.assertIn('data-feedback="mark_watched"', html)
        self.assertIn('data-feedback="hide"', html)


class DownloadOptionTests(unittest.TestCase):
    """The picker offers only renditions that actually play as a single file."""

    VIDEO = {
        "title": "Example",
        "formatStreams": [
            {"url": "http://a/360", "itag": "18", "qualityLabel": "360p", "type": "video/mp4", "clen": "1048576"},
            {"url": "http://a/720", "itag": "22", "qualityLabel": "720p", "type": "video/mp4"},
        ],
        "adaptiveFormats": [
            {"url": "http://a/2160", "qualityLabel": "2160p", "type": "video/mp4; codecs=\"avc1\""},
            {"url": "http://a/audio", "type": "audio/mp4"},
        ],
    }

    def _options(self) -> dict[str, object]:
        with patch.object(playback, "fetch_invidious_video", return_value=self.VIDEO):
            return playback.download_options("vid1")

    def test_muxed_renditions_come_back_best_first(self) -> None:
        formats = self._options()["formats"]
        self.assertEqual([item["quality"] for item in formats], ["720p", "360p"])

    def test_adaptive_only_resolutions_are_reported_but_not_offered(self) -> None:
        options = self._options()
        self.assertEqual(options["max_adaptive_height"], 2160)
        self.assertNotIn("2160p", [item["quality"] for item in options["formats"]])

    def test_sizes_are_parsed_when_present_and_null_when_not(self) -> None:
        formats = {item["quality"]: item for item in self._options()["formats"]}
        self.assertEqual(formats["360p"]["size_bytes"], 1048576)
        self.assertIsNone(formats["720p"]["size_bytes"])

    def test_streams_without_a_url_are_skipped(self) -> None:
        video = {"title": "x", "formatStreams": [{"itag": "18", "qualityLabel": "360p"}], "adaptiveFormats": []}
        with patch.object(playback, "fetch_invidious_video", return_value=video):
            self.assertEqual(playback.download_options("vid1")["formats"], [])

    def test_an_itag_picks_that_exact_rendition(self) -> None:
        stream = playback.select_stream(self.VIDEO, "18")
        self.assertEqual(stream["qualityLabel"], "360p")

    def test_a_stale_itag_falls_back_to_the_best_rendition(self) -> None:
        # A picker left open across a re-fetch should still download something.
        stream = playback.select_stream(self.VIDEO, "999")
        self.assertEqual(stream["qualityLabel"], "720p")


class RelatedSuggestionTests(unittest.TestCase):
    """The side rail is related to the video on screen, and never shorts."""

    ANCHOR = {"youtube_video_id": "anchor", "title": "Modular synth patch walkthrough",
              "tags": ["synth", "modular"], "youtube_channel_id": "UC_anchor"}

    def _related(self, pool: list[dict[str, object]], **kwargs: object) -> list[str]:
        with patch.object(db, "get_video_by_youtube_id", return_value=self.ANCHOR), \
             patch.object(db, "list_recommended_videos", return_value=pool), \
             patch.object(db, "list_subscription_recommendations", return_value=[]):
            return [row["youtube_video_id"] for row in db.list_related_videos("anchor", **kwargs)]

    def test_shorts_are_left_out(self) -> None:
        pool = [
            {"youtube_video_id": "short1", "title": "quick clip", "duration_seconds": 30, "tags": []},
            {"youtube_video_id": "long1", "title": "a long video", "duration_seconds": 900, "tags": []},
        ]
        self.assertEqual(self._related(pool), ["long1"])

    def test_shorts_by_url_are_left_out_too(self) -> None:
        pool = [
            {"youtube_video_id": "short2", "title": "clip", "url": "https://youtube.com/shorts/short2", "duration_seconds": 400, "tags": []},
            {"youtube_video_id": "long2", "title": "video", "duration_seconds": 900, "tags": []},
        ]
        self.assertEqual(self._related(pool), ["long2"])

    def test_shared_tags_outrank_the_default_ordering(self) -> None:
        pool = [
            {"youtube_video_id": "unrelated", "title": "bread baking basics", "duration_seconds": 900, "tags": ["baking"]},
            {"youtube_video_id": "related", "title": "another video", "duration_seconds": 900, "tags": ["modular", "synth"]},
        ]
        self.assertEqual(self._related(pool)[0], "related")

    def test_the_same_channel_is_favoured(self) -> None:
        pool = [
            {"youtube_video_id": "other", "title": "bread baking basics", "duration_seconds": 900, "tags": ["baking"]},
            {"youtube_video_id": "same", "title": "bread baking basics too", "duration_seconds": 900,
             "tags": ["baking"], "youtube_channel_id": "UC_anchor"},
        ]
        self.assertEqual(self._related(pool)[0], "same")

    def test_the_anchor_never_recommends_itself(self) -> None:
        pool = [
            {"youtube_video_id": "anchor", "title": "Modular synth patch walkthrough", "duration_seconds": 900, "tags": ["synth"]},
            {"youtube_video_id": "other", "title": "video", "duration_seconds": 900, "tags": []},
        ]
        self.assertEqual(self._related(pool), ["other"])

    def test_duplicates_across_both_pools_appear_once(self) -> None:
        row = {"youtube_video_id": "dupe", "title": "video", "duration_seconds": 900, "tags": []}
        with patch.object(db, "get_video_by_youtube_id", return_value=self.ANCHOR), \
             patch.object(db, "list_recommended_videos", return_value=[row]), \
             patch.object(db, "list_subscription_recommendations", return_value=[row]):
            self.assertEqual([r["youtube_video_id"] for r in db.list_related_videos("anchor")], ["dupe"])

    def test_the_limit_is_respected(self) -> None:
        pool = [{"youtube_video_id": f"v{index}", "title": "video", "duration_seconds": 900, "tags": []} for index in range(30)]
        self.assertEqual(len(self._related(pool, limit=5)), 5)

    def test_ties_keep_the_pool_ordering(self) -> None:
        pool = [{"youtube_video_id": f"v{index}", "title": "video", "duration_seconds": 900, "tags": []} for index in range(4)]
        self.assertEqual(self._related(pool), ["v0", "v1", "v2", "v3"])


if __name__ == "__main__":
    unittest.main()


class EmbedMarkerTests(unittest.TestCase):
    def test_invidious_seek_bar_markers_are_hidden(self) -> None:
        # Internal segment bookkeeping, not something to put in front of a viewer.
        script = main._player_script()
        self.assertIn(".vjs-marker { display: none !important; }", script)

    def test_the_style_is_injected_once(self) -> None:
        script = main._player_script()
        self.assertIn("openfeed-embed-style", script)
        self.assertIn("doc.getElementById('openfeed-embed-style')", script)


class CommentVoteSpacingTests(unittest.TestCase):
    def test_the_thumbs_sit_together_as_one_control(self) -> None:
        css = main._home_styles()
        self.assertIn(".comment-reactions { display: inline-flex; align-items: center; gap: 0; margin-left: -5px; }", css)
        self.assertIn("min-height: 26px; padding: 0 5px;", css)


class CommentCountTests(unittest.TestCase):
    def test_a_zero_like_count_is_not_rendered(self) -> None:
        # It padded every uncounted comment and pushed the two thumbs apart for nothing.
        script = main._home_script()
        self.assertIn("if (item.like_count) {", script)

    def test_there_is_no_per_comment_dislike_count_to_show(self) -> None:
        # YouTube publishes none and Invidious returns none, so the dislike button is
        # deliberately icon-only rather than showing a fabricated zero.
        script = main._home_script()
        self.assertNotIn("item.dislike_count", script)


class DownloadCardRouteTests(unittest.TestCase):
    """A downloaded short must open the watch page, where the local copy is playable."""

    SHORT = {
        "youtube_video_id": "4h5BEALuh44",
        "title": "A short",
        "duration_seconds": 49,
        "channel_title": "Someone",
        "youtube_channel_id": "UC123",
        "quality": "1080p",
        "size_bytes": 2_518_150,
    }

    def _settings(self):
        return SimpleNamespace(invidious_public_url="http://localhost:8061")

    def test_a_plain_short_still_opens_the_shorts_feed(self) -> None:
        html = main._home_video_card(dict(self.SHORT), 0, self._settings())
        self.assertIn("/shorts?start=4h5BEALuh44", html)

    def test_a_downloaded_short_opens_the_watch_page_instead(self) -> None:
        # The Shorts feed streams from Invidious and would ignore the file on disk.
        html = main._download_card(dict(self.SHORT), self._settings())
        self.assertIn("/watch/4h5BEALuh44", html)
        self.assertNotIn("/shorts?start=4h5BEALuh44", html)

    def test_the_card_carries_the_quality_size_and_a_remove_control(self) -> None:
        html = main._download_card(dict(self.SHORT), self._settings())
        self.assertIn("1080p", html)
        self.assertIn("2.4 MB", html)
        self.assertIn('data-remove-download="4h5BEALuh44"', html)

    def test_it_is_still_an_ordinary_card_with_the_usual_menu(self) -> None:
        html = main._download_card(dict(self.SHORT), self._settings())
        self.assertIn("card-menu", html)
        self.assertIn("data-video-card", html)


class VideoDetailsPaneTests(unittest.TestCase):
    VIDEO = {
        "youtube_video_id": "abc12345678",
        "published_at": None,
        "view_count": 5_243_119,
        "like_count": 187_600,
        "dislike_count": 1_500,
        "dislike_source": "ryd",
        "duration_seconds": 5280,
        "captions_status": "available",
        "source_kind": "subscription",
        "metadata_source": "invidious",
        "tags": ["engineering", "restoration"],
    }

    def test_exact_figures_are_shown_since_the_header_rounds_them(self) -> None:
        html = main._video_details_pane(dict(self.VIDEO))
        self.assertIn("5,243,119", html)
        self.assertIn("187,600", html)

    def test_an_estimated_dislike_count_is_labelled(self) -> None:
        html = main._video_details_pane(dict(self.VIDEO))
        self.assertIn("estimated", html)

    def test_a_real_dislike_count_is_not_labelled_as_an_estimate(self) -> None:
        html = main._video_details_pane(dict(self.VIDEO, dislike_source="youtube"))
        self.assertNotIn("estimated", html)

    def test_tags_link_to_a_search(self) -> None:
        self.assertIn('href="/search?q=engineering"', main._video_details_pane(dict(self.VIDEO)))

    def test_a_downloaded_copy_is_reported(self) -> None:
        html = main._video_details_pane(dict(self.VIDEO), download={"quality": "1080p", "size_bytes": 2_518_150})
        self.assertIn("Downloaded", html)
        self.assertIn("1080p", html)

    def test_it_renders_nothing_when_there_is_nothing_to_say(self) -> None:
        # An empty pane would be a disclosure triangle that opens onto blank space.
        self.assertEqual(main._video_details_pane({"youtube_video_id": "x"}), "")

    def test_unknown_captions_are_omitted_rather_than_shown_as_unknown(self) -> None:
        html = main._video_details_pane(dict(self.VIDEO, captions_status="unknown"))
        self.assertNotIn("unknown", html)


class StatusPageTests(unittest.TestCase):
    def test_status_lives_in_settings_now(self) -> None:
        import inspect

        # It was a sidebar entry pointing at a standalone page; it is a Settings pane.
        from .support import offline_db

        self.assertIn("RedirectResponse", inspect.getsource(main.admin_status))
        with offline_db():
            self.assertIn('data-settings-pane="status"', main._settings_dialog(user={"authenticated": False}))

    def test_the_status_body_still_renders(self) -> None:
        from .support import offline_db

        with offline_db():
            self.assertIn("Service health", main._status_page_html("local"))

    def test_status_tables_scroll_in_their_own_box(self) -> None:
        # A long error message must not be able to widen the whole page.
        self.assertIn(".status-scroll { overflow-x: auto;", main._home_styles())
